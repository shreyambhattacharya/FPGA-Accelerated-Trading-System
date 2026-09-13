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
import hashlib
import re
import time
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
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
    match = re.match(r"^(.*?)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", text)
    if not match:
        raise ValueError(f"invalid RFC-3339 timestamp: {value!r}")
    base_text, fraction_text, zone_text = match.groups()
    parsed = datetime.fromisoformat(base_text + ("+00:00" if zone_text == "Z" else zone_text))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = parsed.astimezone(timezone.utc) - epoch
    fraction_ns = int(((fraction_text or "") + "000000000")[:9])
    return delta.days * 86_400_000_000_000 + delta.seconds * 1_000_000_000 + delta.microseconds * 1_000 + fraction_ns


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

    def __init__(
        self,
        *,
        symbol: str | None = None,
        sequence_start: int = 1,
        quote_size_multiplier: int = 100,
        trade_size_multiplier: int = 1,
    ) -> None:
        self.symbol = symbol
        self.sequence_start = sequence_start
        self.quote_size_multiplier = quote_size_multiplier
        self.trade_size_multiplier = trade_size_multiplier
        if quote_size_multiplier < 1 or trade_size_multiplier < 1:
            raise ValueError("size multipliers must be positive")

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
                quantity = int(row.get("s", row.get("size", row.get("quantity")))) * self.trade_size_multiplier
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
                    quantity=int(bid_size) * self.quote_size_multiplier,
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
                    quantity=int(ask_size) * self.quote_size_multiplier,
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


@dataclass
class DownloadSummary:
    provider: str
    feed: str
    symbol: str
    kind: str
    requested_start: str
    requested_end: str
    pages_requested: int = 0
    pages_completed: int = 0
    records_downloaded: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    retry_count: int = 0
    status_summary: dict[str, int] | None = None
    warnings: list[str] | None = None
    raw_sha256: str | None = None
    raw_file_size: int | None = None
    raw_path: str | None = None

    def __post_init__(self) -> None:
        if self.status_summary is None:
            self.status_summary = {}
        if self.warnings is None:
            self.warnings = []

    def to_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


class AlpacaDownloadError(RuntimeError):
    """Safe provider error containing status/class only, never credentials."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class AlpacaHistoricalDownloader:
    """Paginated, atomic raw downloader for Alpaca stock IEX history."""

    TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
    PERMANENT_STATUSES = {400, 401, 403, 404}

    def __init__(
        self,
        *,
        feed: str = "iex",
        api_key: str | None = None,
        api_secret: str | None = None,
        endpoint: str = "https://data.alpaca.markets/v2/stocks",
        page_limit: int = 10_000,
        max_retries: int = 3,
        backoff_seconds: float = 0.5,
        opener=None,
        sleep_fn=None,
    ) -> None:
        if feed not in {"iex", "sip", "boats", "otc"}:
            raise ValueError("unsupported Alpaca feed")
        if not 1 <= page_limit <= 10_000 or max_retries < 0:
            raise ValueError("invalid pagination/retry settings")
        self.feed = feed
        self.api_key = api_key if api_key is not None else os.environ.get("ALPACA_API_KEY")
        self.api_secret = api_secret if api_secret is not None else os.environ.get("ALPACA_API_SECRET")
        self.endpoint = endpoint.rstrip("/")
        self.page_limit = page_limit
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.opener = opener or urlopen
        self.sleep_fn = sleep_fn or time.sleep

    def fetch_pages(self, *, symbol: str, kind: str, start: str, end: str) -> tuple[list[dict], DownloadSummary]:
        if not self.api_key or not self.api_secret:
            raise AlpacaDownloadError("Alpaca credentials are unavailable", retryable=False)
        if kind not in {"quotes", "trades"}:
            raise ValueError("kind must be quotes or trades")
        summary = DownloadSummary("alpaca", self.feed, symbol, kind, start, end)
        records: list[dict] = []
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            if page_token is not None:
                if page_token in seen_tokens:
                    summary.warnings.append("repeated_page_token")
                    raise AlpacaDownloadError("repeated Alpaca page token")
                seen_tokens.add(page_token)
            params = {"start": start, "end": end, "feed": self.feed, "limit": str(self.page_limit), "sort": "asc"}
            if page_token is not None:
                params["page_token"] = page_token
            url = f"{self.endpoint}/{symbol}/{kind}?{urlencode(params)}"
            summary.pages_requested += 1
            payload, status, retries = self._get_json(url)
            summary.retry_count += retries
            summary.status_summary[str(status)] = summary.status_summary.get(str(status), 0) + 1
            key = kind
            if not isinstance(payload, Mapping) or not isinstance(payload.get(key), list):
                summary.warnings.append("malformed_json")
                raise AlpacaDownloadError("Alpaca response lacks the expected records array")
            page_records = payload[key]
            if not page_records and page_token is not None:
                summary.warnings.append("empty_unexpected_page")
                raise AlpacaDownloadError("empty Alpaca page followed a page token")
            if any(not isinstance(record, Mapping) for record in page_records):
                summary.warnings.append("malformed_record")
                raise AlpacaDownloadError("Alpaca response contains a non-object record")
            records.extend(dict(record) for record in page_records)
            summary.pages_completed += 1
            next_token = payload.get("next_page_token")
            if len(page_records) >= self.page_limit and not next_token:
                summary.warnings.append("missing_expected_page_token")
            if not next_token:
                break
            if str(next_token) in seen_tokens:
                summary.warnings.append("repeated_page_token")
                raise AlpacaDownloadError("repeated Alpaca page token")
            page_token = str(next_token)
        summary.records_downloaded = len(records)
        if records:
            summary.first_timestamp = str(records[0].get("t", records[0].get("timestamp", "")))
            summary.last_timestamp = str(records[-1].get("t", records[-1].get("timestamp", "")))
        return records, summary

    def download_to(self, output_path: Path, *, symbol: str, kind: str, start: str, end: str) -> DownloadSummary:
        if not self.api_key or not self.api_secret:
            raise AlpacaDownloadError("Alpaca credentials are unavailable", retryable=False)
        if kind not in {"quotes", "trades"}:
            raise ValueError("kind must be quotes or trades")
        summary = DownloadSummary("alpaca", self.feed, symbol, kind, start, end)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        partial = output_path.with_name(output_path.name + ".part")
        digest = hashlib.sha256()
        first_record = True
        first_timestamp_record = True
        try:
            with partial.open("wb") as destination:
                destination.write(b'{"' + kind.encode("ascii") + b'":[\n')
                page_token: str | None = None
                seen_tokens: set[str] = set()
                while True:
                    if page_token is not None:
                        if page_token in seen_tokens:
                            summary.warnings.append("repeated_page_token")
                            raise AlpacaDownloadError("repeated Alpaca page token")
                        seen_tokens.add(page_token)
                    params = {"start": start, "end": end, "feed": self.feed, "limit": str(self.page_limit), "sort": "asc"}
                    if page_token is not None:
                        params["page_token"] = page_token
                    url = f"{self.endpoint}/{symbol}/{kind}?{urlencode(params)}"
                    summary.pages_requested += 1
                    payload, status, retries = self._get_json(url)
                    summary.retry_count += retries
                    summary.status_summary[str(status)] = summary.status_summary.get(str(status), 0) + 1
                    if not isinstance(payload, Mapping) or not isinstance(payload.get(kind), list):
                        summary.warnings.append("malformed_json")
                        raise AlpacaDownloadError("Alpaca response lacks the expected records array")
                    page_records = payload[kind]
                    if not page_records and page_token is not None:
                        summary.warnings.append("empty_unexpected_page")
                        raise AlpacaDownloadError("empty Alpaca page followed a page token")
                    for record in page_records:
                        if not isinstance(record, Mapping):
                            summary.warnings.append("malformed_record")
                            raise AlpacaDownloadError("Alpaca response contains a non-object record")
                        encoded = json.dumps(dict(record), separators=(",", ":"), sort_keys=True).encode("utf-8")
                        if not first_record:
                            destination.write(b",\n")
                        destination.write(encoded)
                        digest.update(encoded)
                        first_record = False
                        summary.records_downloaded += 1
                    summary.pages_completed += 1
                    if first_timestamp_record and page_records:
                        summary.first_timestamp = str(page_records[0].get("t", page_records[0].get("timestamp", "")))
                        first_timestamp_record = False
                    if page_records:
                        summary.last_timestamp = str(page_records[-1].get("t", page_records[-1].get("timestamp", "")))
                    next_token = payload.get("next_page_token")
                    if len(page_records) >= self.page_limit and not next_token:
                        summary.warnings.append("missing_expected_page_token")
                    if not next_token:
                        break
                    if str(next_token) in seen_tokens:
                        summary.warnings.append("repeated_page_token")
                        raise AlpacaDownloadError("repeated Alpaca page token")
                    page_token = str(next_token)
                destination.write(b"\n]}\n")
            os.replace(partial, output_path)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        summary.raw_sha256 = _sha256_file(output_path)
        summary.raw_file_size = output_path.stat().st_size
        summary.raw_path = str(output_path)
        return summary

    def _get_json(self, url: str) -> tuple[Any, int, int]:
        retries = 0
        while True:
            request = Request(
                url,
                headers={"APCA-API-KEY-ID": self.api_key, "APCA-API-SECRET-KEY": self.api_secret},
            )
            try:
                with self.opener(request, timeout=60) as response:
                    response_status = getattr(response, "status", None)
                    status = int(response_status if response_status is not None else response.getcode())
                    body = response.read()
                if status != 200:
                    raise AlpacaDownloadError(f"Alpaca HTTP status {status}", status=status, retryable=status in self.TRANSIENT_STATUSES)
                try:
                    return json.loads(body.decode("utf-8")), status, retries
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AlpacaDownloadError("Alpaca returned malformed JSON") from exc
            except HTTPError as exc:
                status = int(exc.code)
                if status in self.PERMANENT_STATUSES:
                    raise AlpacaDownloadError(f"Alpaca HTTP status {status}", status=status, retryable=False) from exc
                if status not in self.TRANSIENT_STATUSES:
                    raise AlpacaDownloadError(f"Alpaca HTTP status {status}", status=status, retryable=False) from exc
                error = AlpacaDownloadError(f"Alpaca HTTP status {status}", status=status, retryable=True)
            except (TimeoutError, URLError, OSError) as exc:
                error = AlpacaDownloadError("Alpaca network timeout or transport error", retryable=True)
            except AlpacaDownloadError as error:
                if not error.retryable:
                    raise
            if retries >= self.max_retries:
                raise AlpacaDownloadError(
                    f"Alpaca request failed after {retries + 1} attempts" + (f"; status {error.status}" if error.status else ""),
                    status=error.status,
                    retryable=False,
                ) from error
            self.sleep_fn(self.backoff_seconds * (2**retries))
            retries += 1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
