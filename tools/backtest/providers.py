"""Historical-data provider adapters.

The adapter boundary keeps provider-specific JSON out of the exact FPGA
replay. Alpaca support is parser/downloader support only; no credentials are
stored and no network request is made by tests or by importing this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .canonical import MSG_MARKET_QUOTE, MSG_MARKET_TRADE, NormalizedEvent


class HistoricalProviderAdapter(Protocol):
    name: str

    def parse(self, payload: Any, symbol_id: int) -> list[NormalizedEvent]:
        """Convert one provider payload into normalized events."""


@dataclass(frozen=True)
class ProviderMetadata:
    provider: str
    symbol_mapping: dict[int, str]
    timezone: str = "UTC"
    dataset_id: str = ""


def timestamp_to_ns(value: str | int | float) -> int:
    if isinstance(value, float) and not value.is_integer():
        numeric_decimal = Decimal(str(value))
        return int(numeric_decimal * Decimal(1_000_000_000))
    if isinstance(value, (int, float)):
        # Provider numeric timestamps are interpreted as nanoseconds only when
        # sufficiently large; this avoids turning Unix seconds into 1970 data.
        numeric = int(value)
        if numeric < 10_000_000_000:
            return numeric * 1_000_000_000
        if numeric < 10_000_000_000_000:
            return numeric * 1_000_000
        if numeric < 10_000_000_000_000_000:
            return numeric * 1_000
        return numeric
    text = str(value).strip()
    if text.isdigit():
        return timestamp_to_ns(int(text))
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    return delta.days * 86_400_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1_000


def price_to_micro(value: str | int | float | Decimal) -> int:
    """Convert a dollar price to integer micro-dollars without float math."""

    decimal_value = Decimal(str(value))
    micro = decimal_value * Decimal(1_000_000)
    if micro != micro.to_integral_value():
        raise ValueError(f"price has more than six decimal places: {value!r}")
    return int(micro)


class AlpacaHistoricalAdapter:
    """Parse Alpaca stock quote/trade JSON and optionally download it.

    Both single-symbol arrays and the multi-symbol ``{"SYM": [...]}`` shape
    are accepted. A quote snapshot becomes two canonical side updates in
    deterministic bid-then-ask order because the FPGA protocol updates one
    side per event.
    """

    name = "alpaca"

    def __init__(self, *, symbol: str | None = None, sequence_start: int = 1) -> None:
        self.symbol = symbol
        self.sequence_start = sequence_start

    def parse(self, payload: Any, symbol_id: int) -> list[NormalizedEvent]:
        if isinstance(payload, Mapping):
            if self.symbol and self.symbol in payload:
                rows = payload[self.symbol]
            elif "quotes" in payload:
                rows = payload["quotes"]
            elif "trades" in payload:
                rows = payload["trades"]
            elif "data" in payload and isinstance(payload["data"], list):
                rows = payload["data"]
            else:
                # A map of symbols is supported by selecting the first list;
                # callers combining symbols should call parse per symbol.
                lists = [value for value in payload.values() if isinstance(value, list)]
                rows = lists[0] if lists else []
        else:
            rows = payload
        if not isinstance(rows, list):
            raise ValueError("Alpaca payload must contain a list of quote/trade rows")

        events: list[NormalizedEvent] = []
        sequence = self.sequence_start
        for row_index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise ValueError(f"Alpaca row {row_index} is not an object")
            timestamp = row.get("t", row.get("timestamp", row.get("time")))
            if timestamp is None:
                raise ValueError(f"Alpaca row {row_index} has no timestamp")
            if self._is_trade(row):
                price = price_to_micro(row.get("p", row.get("price")))
                quantity = int(row.get("s", row.get("size", row.get("quantity"))))
                side = self._side(row.get("side", row.get("taker_side", 0)))
                events.append(
                    NormalizedEvent(
                        timestamp_ns=timestamp_to_ns(timestamp),
                        event_type=MSG_MARKET_TRADE,
                        symbol_id=symbol_id,
                        price=price,
                        quantity=quantity,
                        side=side,
                        sequence=sequence,
                        source_index=row_index,
                        source="alpaca",
                    )
                )
                sequence += 1
                continue

            bid_price = row.get("bp", row.get("bid_price", row.get("bid")))
            bid_size = row.get("bs", row.get("bid_size", row.get("bid_quantity")))
            ask_price = row.get("ap", row.get("ask_price", row.get("ask")))
            ask_size = row.get("as", row.get("ask_size", row.get("ask_quantity")))
            if bid_price is None or bid_size is None or ask_price is None or ask_size is None:
                raise ValueError(f"Alpaca quote row {row_index} is missing bid/ask fields")
            timestamp_ns = timestamp_to_ns(timestamp)
            events.append(
                NormalizedEvent(
                    timestamp_ns=timestamp_ns,
                    event_type=MSG_MARKET_QUOTE,
                    symbol_id=symbol_id,
                    price=price_to_micro(bid_price),
                    quantity=int(bid_size),
                    side=0,
                    sequence=sequence,
                    source_index=row_index * 2,
                    source="alpaca",
                )
            )
            events.append(
                NormalizedEvent(
                    timestamp_ns=timestamp_ns,
                    event_type=MSG_MARKET_QUOTE,
                    symbol_id=symbol_id,
                    price=price_to_micro(ask_price),
                    quantity=int(ask_size),
                    side=1,
                    sequence=sequence + 1,
                    source_index=row_index * 2 + 1,
                    source="alpaca",
                )
            )
            sequence += 2
        return events

    @staticmethod
    def _is_trade(row: Mapping[str, Any]) -> bool:
        return any(key in row for key in ("p", "price")) and any(
            key in row for key in ("s", "size", "quantity")
        ) and not any(key in row for key in ("bp", "bid_price", "bid"))

    @staticmethod
    def _side(value: Any) -> int:
        if value in (0, 1):
            return int(value)
        text = str(value).strip().lower()
        if text in {"buy", "b", "ask", "1"}:
            return 1
        if text in {"sell", "s", "bid", "0", ""}:
            return 0
        raise ValueError(f"unsupported trade side: {value!r}")

    def download(
        self,
        output_path: Path,
        *,
        symbol: str,
        start: str,
        end: str,
        feed: str = "sip",
        kind: str = "trades",
        endpoint: str = "https://data.alpaca.markets/v2/stocks",
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> Path:
        """Download raw quote/trade JSON to an explicit external path.

        The caller owns the output location. Credentials default to the
        documented environment variables and are never written to disk.
        """

        key = api_key or os.environ.get("ALPACA_API_KEY")
        secret = api_secret or os.environ.get("ALPACA_API_SECRET")
        if not key or not secret:
            raise RuntimeError("set ALPACA_API_KEY and ALPACA_API_SECRET to download data")
        if kind not in {"quotes", "trades"}:
            raise ValueError("kind must be quotes or trades")
        query = urlencode({"symbols": symbol, "start": start, "end": end, "feed": feed})
        request = Request(
            f"{endpoint.rstrip('/')}/{kind}?{query}",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        )
        with urlopen(request, timeout=60) as response:  # nosec B310 - explicit provider URL
            raw = response.read()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(raw)
        return output_path


def load_provider_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)
