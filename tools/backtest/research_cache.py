"""Deterministic contiguous research-cache extraction from canonical events.

The cache is intentionally separate from the FPGA/V1 wire format.  It stores
the original event fields plus the exact V1 market-state feature snapshot
produced while replaying each contiguous slice.  A five-minute warm-up is
stored and marked, so software research can use causal state without replaying
the 55.7M-event source for every strategy iteration.

This module never accesses a provider.  The only source accepted by
``build_research_caches`` is the already validated canonical binary.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time as time_module
from typing import Callable, Iterable, Iterator
from zoneinfo import ZoneInfo

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_model"
if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from market_model import MSG_MARKET_QUOTE, MarketModel  # noqa: E402

from .canonical import NormalizedEvent, iter_binary_field_tuples


CACHE_VERSION = "strategy-v2-feature-cache-v1"
# 108 bytes.  Every field is integer/fixed width; no pickle or text rows are
# required for normal research scans.
CACHE_RECORD = struct.Struct(">QBBBBBB2xQIIQQIIQQQQiiiiH2x")
CACHE_RECORD_SIZE = CACHE_RECORD.size

VALID_FEATURE = 1 << 0
VALID_SPREAD = 1 << 1
VALID_MIDPOINT = 1 << 2
VALID_MOMENTUM = 1 << 3
VALID_VWAP = 1 << 4
VALID_VWAP_QUOTIENT = 1 << 5
VALID_IMBALANCE = 1 << 6
VALID_SPREAD_BPS = 1 << 7
VALID_MOMENTUM_BPS = 1 << 8
VALID_VWAP_DELTA_BPS = 1 << 9


@dataclass(frozen=True)
class ResearchWindow:
    """One measured window and its preceding warm-up interval."""

    name: str
    day: str
    warmup_start: str
    measured_start: str
    measured_end: str
    warmup_start_ns: int
    measured_start_ns: int
    measured_end_ns: int

    def contains(self, timestamp_ns: int) -> bool:
        return self.warmup_start_ns <= timestamp_ns < self.measured_end_ns

    def is_measured(self, timestamp_ns: int) -> bool:
        return self.measured_start_ns <= timestamp_ns < self.measured_end_ns

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "day": self.day,
            "warmup_start": self.warmup_start,
            "measured_start": self.measured_start,
            "measured_end": self.measured_end,
            "warmup_start_ns": self.warmup_start_ns,
            "measured_start_ns": self.measured_start_ns,
            "measured_end_ns": self.measured_end_ns,
            "warmup_semantics": "warm-up updates causal market state and strategy state but is excluded from research metrics and trades",
        }


@dataclass(frozen=True)
class CacheRecord:
    timestamp_ns: int
    event_type: int
    symbol_id: int
    measured: bool
    segment_index: int
    session_index: int
    side: int
    price: int
    quantity: int
    sequence: int
    bid_price: int
    ask_price: int
    bid_quantity: int
    ask_quantity: int
    midpoint: int
    spread: int
    vwap: int
    rolling_volume: int
    momentum_bps_x100: int
    vwap_delta_bps_x100: int
    imbalance_q15: int
    spread_bps_x100: int
    validity: int

    @property
    def feature_valid(self) -> bool:
        return bool(self.validity & VALID_FEATURE)

    @property
    def midpoint_valid(self) -> bool:
        return bool(self.validity & VALID_MIDPOINT)

    @property
    def momentum_valid(self) -> bool:
        return bool(self.validity & VALID_MOMENTUM_BPS)

    @property
    def vwap_valid(self) -> bool:
        return bool(self.validity & VALID_VWAP_QUOTIENT)


def _local_ns(day_text: str, value: str, zone: ZoneInfo) -> int:
    local_date = datetime.strptime(day_text, "%Y-%m-%d").date()
    hours, minutes = (int(part) for part in value.split(":", 1))
    return int(datetime.combine(local_date, time(hours, minutes), tzinfo=zone).timestamp() * 1_000_000_000)


def make_windows(
    days: Iterable[str],
    measured_windows: Iterable[tuple[str, str]],
    *,
    warmup_minutes: int = 5,
    timezone_name: str = "America/New_York",
    prefix: str,
) -> tuple[ResearchWindow, ...]:
    zone = ZoneInfo(timezone_name)
    result: list[ResearchWindow] = []
    for day_text in days:
        local_date = datetime.strptime(day_text, "%Y-%m-%d").date()
        for measured_start, measured_end in measured_windows:
            start_hour, start_minute = (int(part) for part in measured_start.split(":", 1))
            start_dt = datetime.combine(local_date, time(start_hour, start_minute), tzinfo=zone)
            warmup_dt = start_dt.replace(second=0, microsecond=0)
            warmup_dt = warmup_dt.fromtimestamp(warmup_dt.timestamp() - warmup_minutes * 60, tz=zone)
            warmup_text = warmup_dt.strftime("%H:%M")
            name = f"{prefix}_{day_text}_{measured_start.replace(':', '')}_{measured_end.replace(':', '')}"
            result.append(
                ResearchWindow(
                    name=name,
                    day=day_text,
                    warmup_start=warmup_text,
                    measured_start=measured_start,
                    measured_end=measured_end,
                    warmup_start_ns=_local_ns(day_text, warmup_text, zone),
                    measured_start_ns=_local_ns(day_text, measured_start, zone),
                    measured_end_ns=_local_ns(day_text, measured_end, zone),
                )
            )
    return tuple(sorted(result, key=lambda item: item.warmup_start_ns))


FAST_WINDOWS = make_windows(
    ("2026-09-01", "2026-09-08"),
    (("09:35", "09:45"), ("12:00", "12:10"), ("15:45", "15:55")),
    prefix="fast",
)
MEDIUM_WINDOWS = make_windows(
    ("2026-09-01", "2026-09-03", "2026-09-08"),
    (("09:35", "09:50"), ("10:45", "11:00"), ("12:45", "13:00"), ("15:35", "15:50")),
    prefix="medium",
)


def cache_record_from_event(event: NormalizedEvent, feature, *, measured: bool, segment_index: int, session_index: int) -> CacheRecord:
    validity = VALID_FEATURE
    if feature is not None:
        validity |= VALID_SPREAD if feature.spread_valid else 0
        validity |= VALID_MIDPOINT if feature.midpoint_valid else 0
        validity |= VALID_MOMENTUM if feature.momentum_valid else 0
        validity |= VALID_VWAP if feature.vwap_valid else 0
        validity |= VALID_VWAP_QUOTIENT if feature.vwap_quotient_valid else 0
        validity |= VALID_IMBALANCE if feature.imbalance_normalized_valid else 0
        validity |= VALID_SPREAD_BPS if feature.spread_bps_x100_valid else 0
        validity |= VALID_MOMENTUM_BPS if feature.momentum_bps_x100_valid else 0
        validity |= VALID_VWAP_DELTA_BPS if feature.midpoint_minus_vwap_bps_x100_valid else 0
        values = (
            feature.bid_price,
            feature.ask_price,
            feature.bid_quantity,
            feature.ask_quantity,
            feature.midpoint,
            feature.spread,
            feature.vwap,
            feature.rolling_volume,
            feature.momentum_bps_x100,
            feature.midpoint_minus_vwap_bps_x100,
            feature.imbalance_normalized,
            feature.spread_bps_x100,
        )
    else:
        validity = 0
        values = (0,) * 12
    return CacheRecord(
        timestamp_ns=event.timestamp_ns,
        event_type=event.event_type,
        symbol_id=event.symbol_id,
        measured=measured,
        segment_index=segment_index,
        session_index=session_index,
        side=event.side,
        price=event.price,
        quantity=event.quantity,
        sequence=event.sequence or 0,
        bid_price=values[0],
        ask_price=values[1],
        bid_quantity=values[2],
        ask_quantity=values[3],
        midpoint=values[4],
        spread=values[5],
        vwap=values[6],
        rolling_volume=values[7],
        momentum_bps_x100=values[8],
        vwap_delta_bps_x100=values[9],
        imbalance_q15=values[10],
        spread_bps_x100=values[11],
        validity=validity,
    )


def pack_cache_record(record: CacheRecord) -> bytes:
    return CACHE_RECORD.pack(
        record.timestamp_ns,
        record.event_type,
        record.symbol_id,
        int(record.measured),
        record.segment_index,
        record.session_index,
        record.side,
        record.price,
        record.quantity,
        record.sequence,
        record.bid_price,
        record.ask_price,
        record.bid_quantity,
        record.ask_quantity,
        record.midpoint,
        record.spread,
        record.vwap,
        record.rolling_volume,
        record.momentum_bps_x100,
        record.vwap_delta_bps_x100,
        record.imbalance_q15,
        record.spread_bps_x100,
        record.validity,
    )


def unpack_cache_record(payload: bytes) -> CacheRecord:
    values = CACHE_RECORD.unpack(payload)
    return CacheRecord(
        timestamp_ns=values[0],
        event_type=values[1],
        symbol_id=values[2],
        measured=bool(values[3]),
        segment_index=values[4],
        session_index=values[5],
        side=values[6],
        price=values[7],
        quantity=values[8],
        sequence=values[9],
        bid_price=values[10],
        ask_price=values[11],
        bid_quantity=values[12],
        ask_quantity=values[13],
        midpoint=values[14],
        spread=values[15],
        vwap=values[16],
        rolling_volume=values[17],
        momentum_bps_x100=values[18],
        vwap_delta_bps_x100=values[19],
        imbalance_q15=values[20],
        spread_bps_x100=values[21],
        validity=values[22],
    )


def iter_cache_records(path: Path) -> Iterator[CacheRecord]:
    with path.open("rb") as source:
        while True:
            payload = source.read(CACHE_RECORD_SIZE)
            if not payload:
                return
            if len(payload) != CACHE_RECORD_SIZE:
                raise ValueError(f"truncated research cache record in {path}")
            yield unpack_cache_record(payload)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_canonical_identity(path: Path, *, expected_size: int, expected_events: int, expected_sha256: str) -> dict:
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(f"canonical size mismatch: expected {expected_size}, got {actual_size}")
    if actual_size != expected_events * 32:
        raise RuntimeError("canonical size/event-count relationship mismatch")
    actual_hash = sha256_file(path)
    if actual_hash.lower() != expected_sha256.lower():
        raise RuntimeError(f"canonical SHA-256 mismatch: expected {expected_sha256}, got {actual_hash}")
    return {"size_bytes": actual_size, "event_count": expected_events, "sha256": actual_hash}


def _feature_dict(feature) -> tuple[int, ...]:
    return (
        feature.bid_price,
        feature.ask_price,
        feature.bid_quantity,
        feature.ask_quantity,
        feature.midpoint,
        feature.spread,
        feature.vwap,
        feature.rolling_volume,
        feature.momentum_bps_x100,
        feature.midpoint_minus_vwap_bps_x100,
        feature.imbalance_normalized,
        feature.spread_bps_x100,
    )


def _valid_manifest(path: Path, data_path: Path, source_identity: dict, windows: tuple[ResearchWindow, ...]) -> dict | None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("cache_version") != CACHE_VERSION:
            return None
        if manifest.get("source_dataset_hash") != source_identity["sha256"] or manifest.get("source_event_count") != source_identity["event_count"]:
            return None
        if manifest.get("record_size") != CACHE_RECORD_SIZE or data_path.stat().st_size != manifest.get("file_size_bytes"):
            return None
        if manifest.get("windows") != [window.to_dict() for window in windows]:
            return None
        if sha256_file(data_path) != manifest.get("cache_sha256"):
            return None
        return manifest
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _manifest(cache_path: Path, source_identity: dict, windows: tuple[ResearchWindow, ...], counts: dict, elapsed: float, digest: str) -> dict:
    return {
        "cache_version": CACHE_VERSION,
        "source_dataset_hash": source_identity["sha256"],
        "source_event_count": source_identity["event_count"],
        "record_format": "timestamp_ns,event_type,symbol_id,measured,segment_index,session_index,side,price,quantity,sequence,bid_price,ask_price,bid_quantity,ask_quantity,midpoint,spread,vwap,rolling_volume,momentum_bps_x100,vwap_delta_bps_x100,imbalance_q15,spread_bps_x100,validity",
        "record_size": CACHE_RECORD_SIZE,
        "record_count": counts["records"],
        "measured_record_count": counts["measured_records"],
        "warmup_record_count": counts["warmup_records"],
        "event_counts": counts["event_counts"],
        "per_segment_counts": counts["per_segment_counts"],
        "file_size_bytes": cache_path.stat().st_size,
        "cache_sha256": digest,
        "generation_elapsed_seconds": elapsed,
        "generation_semantics": "One chronological pass over the validated canonical binary; each segment resets MarketModel at warm-up start, processes every event in the contiguous warm-up+measured interval, and marks only measured timestamps for research metrics.",
        "windows": [window.to_dict() for window in windows],
    }


def build_research_caches(
    canonical_path: Path,
    source_identity: dict,
    cache_specs: dict[str, tuple[Path, tuple[ResearchWindow, ...]]],
    *,
    progress_callback: Callable[[dict], None] | None = None,
    total_events: int | None = None,
    progress_event_interval: int = 1_000_000,
    progress_time_seconds: float = 30.0,
) -> dict[str, dict]:
    """Build any missing caches from the validated canonical source.

    ``cache_specs`` maps a logical name to ``(data_path, windows)``. Existing
    manifests are validated and reused. If more than one cache is missing,
    each cache is generated in its own source pass so each MarketModel state
    remains isolated; no provider download or normalization is involved.
    """

    results: dict[str, dict] = {}
    missing: dict[str, tuple[Path, tuple[ResearchWindow, ...], Path]] = {}
    for name, (data_path, windows) in cache_specs.items():
        manifest_path = data_path.with_suffix(".json")
        valid = _valid_manifest(manifest_path, data_path, source_identity, windows) if data_path.exists() and manifest_path.exists() else None
        if valid is not None:
            results[name] = valid | {"cache_reused": True}
        else:
            data_path.parent.mkdir(parents=True, exist_ok=True)
            missing[name] = (data_path, windows, manifest_path)
    if not missing:
        return results
    # Separate missing datasets keep each hot path to one MarketModel state.
    # This is still efficient extraction (and avoids provider/raw work), while
    # preventing overlapping FAST/MEDIUM windows from doubling the expensive
    # feature-model work for every source event.
    if len(missing) > 1:
        for name, (data_path, windows, manifest_path) in missing.items():
            results.update(build_research_caches(
                canonical_path,
                source_identity,
                {name: (data_path, windows)},
                progress_callback=progress_callback,
                total_events=total_events,
                progress_event_interval=progress_event_interval,
                progress_time_seconds=progress_time_seconds,
            ))
        return results

    partials: dict[str, tuple[Path, object, hashlib._Hash]] = {}
    for name, (data_path, windows, manifest_path) in missing.items():
        partial = data_path.with_name(data_path.name + ".part")
        handle = partial.open("wb")
        partials[name] = (partial, handle, hashlib.sha256())

    states = {
        name: {"pointer": 0, "model": None, "records": 0, "measured_records": 0, "warmup_records": 0, "event_counts": {"quote": 0, "trade": 0}, "per_segment_counts": {}}
        for name in missing
    }
    processed = 0
    started = time_module.perf_counter()
    last_progress_time = started
    last_progress_events = 0

    def report(force: bool = False):
        nonlocal last_progress_time, last_progress_events
        if progress_callback is None:
            return
        now = time_module.perf_counter()
        if not force and processed - last_progress_events < progress_event_interval and now - last_progress_time < progress_time_seconds:
            return
        elapsed = now - started
        rate = processed / elapsed if elapsed else 0.0
        remaining = (total_events - processed) / rate if total_events and rate > 0 else None
        progress_callback({
            "processed_events": processed,
            "total_events": total_events,
            "percent_complete": processed / total_events * 100.0 if total_events else None,
            "elapsed_time": elapsed,
            "processing_rate_events_per_second": rate,
            "estimated_remaining_time": remaining,
        })
        last_progress_time = now
        last_progress_events = processed

    try:
        for source_index, fields in iter_binary_field_tuples(canonical_path, chunk_packets=65_536):
            processed += 1
            report()
            _, event_type, symbol_id, timestamp_ns, price, quantity, side, sequence, flags = fields
            event = NormalizedEvent(
                timestamp_ns=timestamp_ns,
                event_type=event_type,
                symbol_id=symbol_id,
                price=price,
                quantity=quantity,
                side=side,
                sequence=sequence,
                flags=flags,
                source_index=source_index,
                source=str(canonical_path),
            )
            for name, (data_path, windows, manifest_path) in missing.items():
                state = states[name]
                while state["pointer"] < len(windows) and event.timestamp_ns >= windows[state["pointer"]].measured_end_ns:
                    state["pointer"] += 1
                    state["model"] = None
                if state["pointer"] >= len(windows):
                    continue
                window = windows[state["pointer"]]
                if event.timestamp_ns < window.warmup_start_ns:
                    continue
                if event.timestamp_ns >= window.measured_end_ns:
                    continue
                if state["model"] is None:
                    state["model"] = MarketModel(num_symbols=4, trade_window=32, momentum_window=16, vwap_window=32)
                outcome = state["model"].process_event(event.to_market_event())
                feature = outcome.feature if outcome.kind == "feature" else None
                record = cache_record_from_event(
                    event,
                    feature,
                    measured=window.is_measured(event.timestamp_ns),
                    segment_index=state["pointer"],
                    session_index=sum(1 for prior in windows if prior.day < window.day),
                )
                payload = pack_cache_record(record)
                partial, handle, digest = partials[name]
                handle.write(payload)
                digest.update(payload)
                state["records"] += 1
                if record.measured:
                    state["measured_records"] += 1
                else:
                    state["warmup_records"] += 1
                event_name = "quote" if event.event_type == MSG_MARKET_QUOTE else "trade"
                state["event_counts"][event_name] += 1
                segment_counts = state["per_segment_counts"].setdefault(str(state["pointer"]), {"records": 0, "measured_records": 0, "warmup_records": 0})
                segment_counts["records"] += 1
                segment_counts["measured_records" if record.measured else "warmup_records"] += 1
        report(force=True)
        elapsed = time_module.perf_counter() - started
        for name, (data_path, windows, manifest_path) in missing.items():
            partial, handle, digest = partials[name]
            handle.close()
            os.replace(partial, data_path)
            state = states[name]
            counts = {
                "records": state["records"],
                "measured_records": state["measured_records"],
                "warmup_records": state["warmup_records"],
                "event_counts": state["event_counts"],
                "per_segment_counts": state["per_segment_counts"],
            }
            manifest = _manifest(data_path, source_identity, windows, counts, elapsed, digest.hexdigest())
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            results[name] = manifest | {"cache_reused": False}
    except BaseException:
        for partial, handle, _ in partials.values():
            try:
                handle.close()
            except OSError:
                pass
        raise
    return results


def cache_feature_object(record: CacheRecord):
    """Return the FeatureSnapshot-compatible view needed by V1 execution."""

    from types import SimpleNamespace

    valid = record.validity
    return SimpleNamespace(
        symbol_id=record.symbol_id,
        sequence=record.sequence,
        bid_price=record.bid_price,
        bid_quantity=record.bid_quantity,
        ask_price=record.ask_price,
        ask_quantity=record.ask_quantity,
        spread=record.spread,
        spread_valid=bool(valid & VALID_SPREAD),
        midpoint=record.midpoint,
        midpoint_valid=bool(valid & VALID_MIDPOINT),
        momentum=0,
        momentum_valid=bool(valid & VALID_MOMENTUM),
        rolling_volume=record.rolling_volume,
        imbalance_numerator=record.imbalance_q15,
        imbalance_denominator=1,
        imbalance_valid=bool(valid & VALID_IMBALANCE),
        vwap_sum_price_quantity=0,
        vwap_sum_quantity=0,
        vwap_valid=bool(valid & VALID_VWAP),
        vwap=record.vwap,
        vwap_quotient_valid=bool(valid & VALID_VWAP_QUOTIENT),
        imbalance_normalized=record.imbalance_q15,
        imbalance_normalized_valid=bool(valid & VALID_IMBALANCE),
        spread_bps_x100=record.spread_bps_x100,
        spread_bps_x100_valid=bool(valid & VALID_SPREAD_BPS),
        momentum_bps_x100=record.momentum_bps_x100,
        momentum_bps_x100_valid=bool(valid & VALID_MOMENTUM_BPS),
        midpoint_minus_vwap=record.midpoint - record.vwap if record.vwap else 0,
        midpoint_minus_vwap_valid=bool(valid & VALID_VWAP),
        midpoint_minus_vwap_bps_x100=record.vwap_delta_bps_x100,
        midpoint_minus_vwap_bps_x100_valid=bool(valid & VALID_VWAP_DELTA_BPS),
    )
