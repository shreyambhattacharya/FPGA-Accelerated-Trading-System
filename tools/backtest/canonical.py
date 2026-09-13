"""Canonical normalized historical-event format.

CSV is the inspection format. The compact binary format is a concatenation of
the existing 32-byte CRC-protected FPGA event packets, so a binary replay is
not a second semantic encoding.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from pathlib import Path
import struct
from typing import Iterable, Iterator, Sequence

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_model"
import sys

if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from market_model import (  # noqa: E402
    MSG_MARKET_QUOTE,
    MSG_MARKET_TRADE,
    MarketEvent,
    PACKET_BYTES,
    SYNC_VERSION,
    crc8,
)


EVENT_TYPES = {
    "quote": MSG_MARKET_QUOTE,
    "market_quote": MSG_MARKET_QUOTE,
    "trade": MSG_MARKET_TRADE,
    "market_trade": MSG_MARKET_TRADE,
}
EVENT_NAMES = {
    MSG_MARKET_QUOTE: "quote",
    MSG_MARKET_TRADE: "trade",
}
CANONICAL_FIELDS = (
    "timestamp_ns",
    "event_type",
    "symbol_id",
    "price",
    "quantity",
    "side",
    "sequence",
    "flags",
)


class CanonicalFormatError(ValueError):
    """Raised when an input file cannot be decoded as canonical events."""


@dataclass(frozen=True)
class NormalizedEvent:
    """One normalized event in FPGA wire units.

    ``sequence`` may be ``None`` only before deterministic replay sequencing is
    applied. ``source_index`` preserves input order for equal-timestamp ties.
    """

    timestamp_ns: int
    event_type: int
    symbol_id: int
    price: int
    quantity: int
    side: int
    sequence: int | None
    flags: int = 0
    source_index: int = 0
    source: str = ""

    @property
    def message_type(self) -> int:
        """Compatibility alias for the reference model's packet field."""

        return self.event_type

    def to_market_event(self, sequence: int | None = None) -> MarketEvent:
        resolved = self.sequence if sequence is None else sequence
        if resolved is None:
            raise CanonicalFormatError("event sequence is not assigned")
        return MarketEvent(
            message_type=self.event_type,
            symbol_id=self.symbol_id,
            timestamp_ns=self.timestamp_ns,
            price=self.price,
            quantity=self.quantity,
            side=self.side,
            sequence=resolved,
            flags=self.flags,
        )

    def with_sequence(self, sequence: int) -> "NormalizedEvent":
        return replace(self, sequence=sequence)


def parse_int(value: str | int | None, *, field: str, row_number: int | None = None) -> int:
    if value is None or str(value).strip() == "":
        location = f" on row {row_number}" if row_number is not None else ""
        raise CanonicalFormatError(f"missing {field}{location}")
    try:
        return int(str(value).strip(), 0)
    except ValueError as exc:
        location = f" on row {row_number}" if row_number is not None else ""
        raise CanonicalFormatError(f"invalid {field}{location}: {value!r}") from exc


def parse_event_type(value: str | int | None, *, row_number: int | None = None) -> int:
    if value is None or str(value).strip() == "":
        raise CanonicalFormatError(f"missing event_type on row {row_number}")
    text = str(value).strip().lower()
    if text in EVENT_TYPES:
        return EVENT_TYPES[text]
    return parse_int(text, field="event_type", row_number=row_number)


def event_type_name(event_type: int) -> str:
    return EVENT_NAMES.get(event_type, f"0x{event_type:02x}")


def event_from_mapping(mapping: dict[str, str], *, row_number: int = 0, source: str = "") -> NormalizedEvent:
    timestamp_key = "timestamp_ns" if "timestamp_ns" in mapping else "timestamp"
    required = (timestamp_key, "event_type", "symbol_id", "price", "quantity", "side")
    for field in required:
        if field not in mapping:
            raise CanonicalFormatError(f"missing {field} on row {row_number}")
    sequence_text = mapping.get("sequence", "")
    sequence = None if str(sequence_text).strip() == "" else parse_int(
        sequence_text, field="sequence", row_number=row_number
    )
    return NormalizedEvent(
        timestamp_ns=parse_int(mapping[timestamp_key], field=timestamp_key, row_number=row_number),
        event_type=parse_event_type(mapping["event_type"], row_number=row_number),
        symbol_id=parse_int(mapping["symbol_id"], field="symbol_id", row_number=row_number),
        price=parse_int(mapping["price"], field="price", row_number=row_number),
        quantity=parse_int(mapping["quantity"], field="quantity", row_number=row_number),
        side=parse_int(mapping["side"], field="side", row_number=row_number),
        sequence=sequence,
        flags=0 if str(mapping.get("flags", "")).strip() == "" else parse_int(
            mapping["flags"], field="flags", row_number=row_number
        ),
        source_index=max(row_number - 2, 0),
        source=source,
    )


def read_csv_events(path: Path) -> list[NormalizedEvent]:
    """Read canonical CSV, allowing comments and legacy ``timestamp``."""

    events: list[NormalizedEvent] = []
    with path.open(newline="", encoding="utf-8") as source:
        rows = (line for line in source if line.strip() and not line.lstrip().startswith("#"))
        reader = csv.DictReader(rows)
        if not reader.fieldnames:
            raise CanonicalFormatError(f"{path} has no CSV header")
        required = {"event_type", "symbol_id", "price", "quantity", "side"}
        if not ({"timestamp_ns", "timestamp"} & set(reader.fieldnames)):
            required.add("timestamp_ns")
        missing = sorted(required.difference(reader.fieldnames))
        if missing:
            raise CanonicalFormatError(f"{path} is missing columns: {', '.join(missing)}")
        for row_number, row in enumerate(reader, start=2):
            events.append(event_from_mapping(row, row_number=row_number, source=str(path)))
    return events


def read_binary_events(path: Path, *, validate_crc: bool = True) -> list[NormalizedEvent]:
    """Read concatenated exact 32-byte FPGA packets and validate their CRC."""

    return list(iter_binary_events(path, validate_crc=validate_crc))


def iter_binary_events(path: Path, *, validate_crc: bool = True, start_index: int = 0, step: int = 1) -> Iterator[NormalizedEvent]:
    """Stream exact 32-byte FPGA packets, optionally validating each CRC.

    CRC validation is enabled by default for ingestion and quality checks. A
    normalized file is already written from CRC-protected packets, so large
    replay passes may disable the per-record CRC loop after one validation
    scan; this keeps the replay semantics identical while avoiding redundant
    packet-integrity work on every experiment variant. ``step`` selects
    deterministic packet positions without decoding skipped packets, making
    bounded exploratory probes sparse reads rather than hidden full decoding
    passes.
    """

    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if step < 1:
        raise ValueError("step must be positive")
    with path.open("rb") as source:
        source.seek(start_index * PACKET_BYTES)
        index = start_index
        source_label = str(path)
        while True:
            packet = source.read(PACKET_BYTES)
            if not packet:
                return
            if len(packet) != PACKET_BYTES:
                raise CanonicalFormatError(f"{path} length is not a multiple of {PACKET_BYTES}")
            if validate_crc and (packet[0] != SYNC_VERSION or packet[-1] != crc8(packet[:-1])):
                raise CanonicalFormatError(f"invalid sync/CRC in binary record {index}")
            message_type, symbol_id, timestamp_ns, price, quantity, side, sequence, flags = _decode_packet_fields(packet)
            yield NormalizedEvent(
                timestamp_ns=timestamp_ns,
                event_type=message_type,
                symbol_id=symbol_id,
                price=price,
                quantity=quantity,
                side=side,
                sequence=sequence,
                flags=flags,
                source_index=index,
                source=source_label,
            )
            index += step
            if step > 1:
                source.seek((step - 1) * PACKET_BYTES, 1)


def iter_binary_field_tuples(path: Path, *, start_index: int = 0, step: int = 1, chunk_packets: int = 65_536):
    """Yield packet fields through a large buffered ``struct.iter_unpack`` loop.

    This is the no-CRC, extraction hot path.  It avoids constructing a
    ``NormalizedEvent`` for packets that fall outside the requested research
    windows; callers create the dataclass only for selected records.
    """

    if start_index < 0:
        raise ValueError("start_index must be non-negative")
    if step < 1:
        raise ValueError("step must be positive")
    if chunk_packets < 1:
        raise ValueError("chunk_packets must be positive")
    fields = struct.Struct(">BBHQQIBIHx")
    chunk_bytes = chunk_packets * PACKET_BYTES
    with path.open("rb", buffering=chunk_bytes) as source:
        source.seek(start_index * PACKET_BYTES)
        index = start_index
        while True:
            payload = source.read(chunk_bytes)
            if not payload:
                return
            if len(payload) % PACKET_BYTES:
                raise CanonicalFormatError(f"{path} length is not a multiple of {PACKET_BYTES}")
            for offset, values in enumerate(struct.iter_unpack(fields.format, payload)):
                if offset % step == 0:
                    yield index + offset, values
            index += len(payload) // PACKET_BYTES


def _decode_packet_fields(packet: bytes) -> tuple[int, int, int, int, int, int, int, int]:
    sync, message_type, symbol, timestamp, price, quantity, side, sequence, flags = struct.unpack(
        ">BBHQQIBIH", packet[:31]
    )
    return message_type, symbol, timestamp, price, quantity, side, sequence, flags


def read_events(path: Path, fmt: str = "auto") -> list[NormalizedEvent]:
    selected = fmt
    if selected == "auto":
        selected = "binary" if path.suffix.lower() in {".bin", ".pkt", ".packets"} else "csv"
    if selected == "csv":
        return read_csv_events(path)
    if selected == "binary":
        return read_binary_events(path)
    raise CanonicalFormatError(f"unsupported input format: {fmt}")


def write_csv_events(path: Path, events: Iterable[NormalizedEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=CANONICAL_FIELDS)
        writer.writeheader()
        for event in events:
            writer.writerow(
                {
                    "timestamp_ns": event.timestamp_ns,
                    "event_type": event_type_name(event.event_type),
                    "symbol_id": event.symbol_id,
                    "price": event.price,
                    "quantity": event.quantity,
                    "side": event.side,
                    "sequence": "" if event.sequence is None else event.sequence,
                    "flags": event.flags,
                }
            )


def write_binary_events(path: Path, events: Iterable[NormalizedEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as destination:
        for event in events:
            destination.write(event.to_market_event().packet())


def sort_events(events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
    """Globally time-order events with deterministic symbol/source/side ties."""

    return sorted(events, key=lambda event: (event.timestamp_ns, event.symbol_id, event.source_index, event.event_type, event.side))


def assign_replay_sequences(
    events: Sequence[NormalizedEvent], *, reset_per_session: bool = False, timezone=None
) -> list[NormalizedEvent]:
    """Assign 1-based deterministic per-symbol sequences to missing values."""

    counters: dict[tuple[object, int], int] = {}
    resolved: list[NormalizedEvent] = []
    for event in events:
        session_key: object = None
        if reset_per_session and timezone is not None:
            from datetime import datetime, timezone as dt_timezone

            session_key = datetime.fromtimestamp(
                event.timestamp_ns / 1_000_000_000, tz=dt_timezone.utc
            ).astimezone(timezone).date()
        key = (session_key, event.symbol_id)
        if event.sequence is None:
            counters[key] = counters.get(key, 0) + 1
            resolved.append(event.with_sequence(counters[key]))
        else:
            counters[key] = max(counters.get(key, 0), event.sequence)
            resolved.append(event)
    return resolved


def iter_session_dates(events: Iterable[NormalizedEvent], timezone) -> Iterator[object]:
    from datetime import datetime, timezone as dt_timezone

    seen = set()
    for event in events:
        date = datetime.fromtimestamp(event.timestamp_ns / 1_000_000_000, tz=dt_timezone.utc).astimezone(timezone).date()
        if date not in seen:
            seen.add(date)
            yield date
