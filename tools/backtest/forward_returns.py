"""No-look-ahead forward-return and candidate calibration analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from statistics import mean, median

HORIZONS_NS = {
    "100ms": 100_000_000,
    "500ms": 500_000_000,
    "1s": 1_000_000_000,
    "5s": 5_000_000_000,
    "15s": 15_000_000_000,
    "30s": 30_000_000_000,
    "60s": 60_000_000_000,
    "5min": 300_000_000_000,
}


@dataclass(frozen=True)
class QuotePoint:
    timestamp_ns: int
    symbol_id: int
    bid_price: int
    ask_price: int


@dataclass(frozen=True)
class ForwardReturnRow:
    candidate_timestamp_ns: int
    symbol_id: int
    direction: str
    score: int
    reason_bits: int
    horizon: str
    horizon_ns: int
    return_bps: float | None


class ForwardReturnAnalyzer:
    def __init__(self, horizons: dict[str, int] | None = None) -> None:
        self.horizons = horizons or HORIZONS_NS

    def analyze(self, candidates, quote_points: list[QuotePoint]) -> tuple[list[ForwardReturnRow], dict]:
        by_symbol: dict[int, list[QuotePoint]] = defaultdict(list)
        for point in sorted(quote_points, key=lambda p: (p.symbol_id, p.timestamp_ns)):
            by_symbol[point.symbol_id].append(point)
        rows: list[ForwardReturnRow] = []
        for candidate in candidates:
            if candidate.action == 0:
                continue
            direction = "long" if candidate.action == 1 else "short"
            current = self._current_point(candidate, by_symbol.get(candidate.symbol_id, []), direction)
            entry_price = self._entry_price(current, direction) if current else None
            for label, horizon_ns in self.horizons.items():
                result = None
                if entry_price:
                    target = candidate.timestamp_ns + horizon_ns
                    for future in by_symbol.get(candidate.symbol_id, []):
                        if future.timestamp_ns < target:
                            continue
                        exit_price = future.bid_price if direction == "long" else future.ask_price
                        if exit_price > 0:
                            result = ((exit_price - entry_price) if direction == "long" else (entry_price - exit_price)) / entry_price * 10_000.0
                            break
                rows.append(ForwardReturnRow(
                    candidate_timestamp_ns=candidate.timestamp_ns,
                    symbol_id=candidate.symbol_id,
                    direction=direction,
                    score=candidate.score,
                    reason_bits=candidate.reason_bits,
                    horizon=label,
                    horizon_ns=horizon_ns,
                    return_bps=result,
                ))
        return rows, self._summarize(rows)

    @staticmethod
    def _current_point(candidate, points, direction):
        for point in reversed(points):
            if point.timestamp_ns > candidate.timestamp_ns:
                continue
            if ForwardReturnAnalyzer._entry_price(point, direction) is not None:
                return point
        return None

    @staticmethod
    def _entry_price(point, direction):
        if point is None:
            return None
        price = point.ask_price if direction == "long" else point.bid_price
        return price if price > 0 else None

    @staticmethod
    def _quantile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        index = min(len(values) - 1, int(round((len(values) - 1) * fraction)))
        return values[index]

    def _summarize(self, rows: list[ForwardReturnRow]) -> dict:
        groups: dict[tuple, list[float]] = defaultdict(list)
        for row in rows:
            if row.return_bps is not None:
                groups[(row.direction, row.symbol_id, row.score, row.reason_bits, row.horizon)].append(row.return_bps)
        summary = []
        for (direction, symbol_id, score, reason_bits, horizon), values in sorted(groups.items(), key=str):
            summary.append({
                "direction": direction,
                "symbol_id": symbol_id,
                "score": score,
                "reason_bits": reason_bits,
                "horizon": horizon,
                "count": len(values),
                "mean_bps": mean(values),
                "median_bps": median(values),
                "win_fraction": sum(value > 0 for value in values) / len(values),
                "quantiles_bps": {str(q): self._quantile(values, q) for q in (0.1, 0.25, 0.5, 0.75, 0.9)},
            })
        return {"rows": len(rows), "groups": summary}

    @staticmethod
    def row_to_dict(row: ForwardReturnRow) -> dict:
        return asdict(row)
