"""Validation and diagnostics for imported normalized quote/trade events."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from .canonical import MSG_MARKET_QUOTE, MSG_MARKET_TRADE, NormalizedEvent, event_type_name


@dataclass(frozen=True)
class QualityIssue:
    severity: str
    code: str
    source_index: int
    symbol_id: int | None
    timestamp_ns: int | None
    detail: str


@dataclass
class DataQualityReport:
    records: int = 0
    issues: list[QualityIssue] | None = None

    def __post_init__(self) -> None:
        if self.issues is None:
            self.issues = []

    @property
    def errors(self) -> int:
        return sum(issue.severity == "error" for issue in self.issues or [])

    @property
    def warnings(self) -> int:
        return sum(issue.severity == "warning" for issue in self.issues or [])

    @property
    def by_code(self) -> dict[str, int]:
        return dict(Counter(issue.code for issue in self.issues or []))

    @property
    def has_errors(self) -> bool:
        return self.errors != 0

    def add(
        self,
        severity: str,
        code: str,
        event: NormalizedEvent | None,
        detail: str,
    ) -> None:
        self.issues.append(
            QualityIssue(
                severity=severity,
                code=code,
                source_index=event.source_index if event else 0,
                symbol_id=event.symbol_id if event else None,
                timestamp_ns=event.timestamp_ns if event else None,
                detail=detail,
            )
        )

    def to_dict(self) -> dict:
        return {
            "records": self.records,
            "errors": self.errors,
            "warnings": self.warnings,
            "by_code": self.by_code,
            "issues": [asdict(issue) for issue in self.issues or []],
        }


class DataQualityValidator:
    """Detect quality problems without silently changing replay semantics."""

    def __init__(
        self,
        *,
        symbol_mapping: Mapping[int, str] | None = None,
        gap_threshold_ns: int | None = 5 * 60 * 1_000_000_000,
        duplicate_severity: str = "warning",
        cross_severity: str = "warning",
    ) -> None:
        self.symbol_mapping = symbol_mapping
        self.gap_threshold_ns = gap_threshold_ns
        self.duplicate_severity = duplicate_severity
        self.cross_severity = cross_severity

    def validate(self, events: Iterable[NormalizedEvent]) -> DataQualityReport:
        report = DataQualityReport()
        seen_records: set[tuple] = set()
        seen_sequences: dict[int, set[int]] = {}
        last_timestamp: int | None = None
        books: dict[int, dict[str, int | None]] = {}

        for event in events:
            report.records += 1
            if last_timestamp is not None:
                if event.timestamp_ns < last_timestamp:
                    report.add(
                        "error",
                        "timestamp_out_of_order",
                        event,
                        f"{event.timestamp_ns} follows {last_timestamp}",
                    )
                if (
                    self.gap_threshold_ns is not None
                    and event.timestamp_ns - last_timestamp > self.gap_threshold_ns
                ):
                    report.add(
                        "warning",
                        "large_timestamp_gap",
                        event,
                        f"gap={event.timestamp_ns - last_timestamp} ns",
                    )
            last_timestamp = event.timestamp_ns

            fingerprint = (
                event.timestamp_ns,
                event.event_type,
                event.symbol_id,
                event.price,
                event.quantity,
                event.side,
                event.sequence,
                event.flags,
            )
            if fingerprint in seen_records:
                report.add(self.duplicate_severity, "duplicate_record", event, "identical event repeated")
            seen_records.add(fingerprint)

            if event.timestamp_ns < 0:
                report.add("error", "negative_timestamp", event, "timestamp must be non-negative")
            if event.symbol_id < 0:
                report.add("error", "negative_symbol", event, "symbol ID must be non-negative")
            if event.event_type not in (MSG_MARKET_QUOTE, MSG_MARKET_TRADE):
                report.add("error", "unsupported_event_type", event, event_type_name(event.event_type))
            if event.price < 0:
                report.add("error", "negative_price", event, "price must be non-negative")
            if event.quantity < 0:
                report.add("error", "negative_quantity", event, "quantity must be non-negative")
            if event.event_type in (MSG_MARKET_QUOTE, MSG_MARKET_TRADE) and event.price == 0:
                report.add("error", "zero_price", event, "market price must be positive")
            if event.side not in (0, 1):
                report.add("error", "invalid_side", event, f"side={event.side}")
            if self.symbol_mapping is not None and event.symbol_id not in self.symbol_mapping:
                report.add("error", "missing_symbol_mapping", event, "symbol ID has no metadata mapping")

            if event.sequence is not None:
                sequence_set = seen_sequences.setdefault(event.symbol_id, set())
                if event.sequence in sequence_set:
                    report.add(
                        self.duplicate_severity,
                        "duplicate_sequence",
                        event,
                        f"sequence={event.sequence} repeated for symbol",
                    )
                sequence_set.add(event.sequence)

            if event.event_type == MSG_MARKET_QUOTE and event.side in (0, 1):
                book = books.setdefault(event.symbol_id, {"bid": None, "ask": None})
                book["bid" if event.side == 0 else "ask"] = event.price
                bid, ask = book["bid"], book["ask"]
                if bid is not None and ask is not None and ask < bid:
                    report.add(
                        self.cross_severity,
                        "quote_cross",
                        event,
                        f"bid={bid} ask={ask}; retained for FPGA-validity semantics",
                    )

        return report
