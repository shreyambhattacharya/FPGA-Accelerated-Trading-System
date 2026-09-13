"""Software-only, integer-friendly Strategy V2 research primitives.

The classes in this module are deliberately outside ``tools/reference_model``
and the RTL comparison path.  They operate on the fixed-width research cache,
use only causal timestamp lookups, and expose simple predicates that can be
translated to eventual FPGA state machines after the research milestone.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from statistics import mean, median
from typing import Iterable

from .research_cache import CacheRecord, cache_feature_object


def trunc_div_signed(numerator: int, denominator: int) -> int:
    magnitude = abs(numerator) // abs(denominator)
    return -magnitude if (numerator < 0) != (denominator < 0) else magnitude


def bps_x100(delta: int, reference: int) -> int:
    """Return integer basis points x100 using V1's toward-zero arithmetic."""

    if reference == 0:
        raise ZeroDivisionError("reference price cannot be zero")
    return trunc_div_signed(delta * 10_000 * 100, reference)


HORIZON_NAMES = {
    100_000_000: "100ms",
    250_000_000: "250ms",
    500_000_000: "500ms",
    1_000_000_000: "1s",
    2_000_000_000: "2s",
    5_000_000_000: "5s",
    15_000_000_000: "15s",
    30_000_000_000: "30s",
    60_000_000_000: "60s",
}


@dataclass(frozen=True)
class TimeFeature:
    record: CacheRecord
    momentum_bps_x100: dict[int, int | None]
    vwap_bps_x100: dict[int, int | None]
    time_vwap: dict[int, int | None]

    @property
    def feature(self):
        return cache_feature_object(self.record)


class CausalTimeFeatureEngine:
    """Compute timestamp-based features with no future lookup."""

    MOMENTUM_HORIZONS = (100_000_000, 250_000_000, 500_000_000, 1_000_000_000, 2_000_000_000, 5_000_000_000, 15_000_000_000)
    VWAP_HORIZONS = (500_000_000, 1_000_000_000, 2_000_000_000, 5_000_000_000, 15_000_000_000, 30_000_000_000, 60_000_000_000)

    def __init__(self, num_symbols: int = 4):
        self.num_symbols = num_symbols
        self.reset()

    def reset(self) -> None:
        self.midpoint_timestamps = [[] for _ in range(self.num_symbols)]
        self.midpoint_values = [[] for _ in range(self.num_symbols)]
        self.trade_windows = [[deque() for _ in self.VWAP_HORIZONS] for _ in range(self.num_symbols)]
        self.trade_sum_pq = [[0] * len(self.VWAP_HORIZONS) for _ in range(self.num_symbols)]
        self.trade_sum_qty = [[0] * len(self.VWAP_HORIZONS) for _ in range(self.num_symbols)]
        self.segment_index = None

    def process(self, record: CacheRecord, *, compute: bool = True) -> TimeFeature:
        if self.segment_index != record.segment_index:
            self.reset()
            self.segment_index = record.segment_index
        sid = record.symbol_id
        timestamp_ns = record.timestamp_ns
        if record.midpoint_valid and record.midpoint > 0:
            self.midpoint_timestamps[sid].append(timestamp_ns)
            self.midpoint_values[sid].append(record.midpoint)
        if record.event_type == 2 and record.price > 0 and record.quantity > 0:
            for index, horizon in enumerate(self.VWAP_HORIZONS):
                trades = self.trade_windows[sid][index]
                trades.append((timestamp_ns, record.price, record.quantity))
                self.trade_sum_pq[sid][index] += record.price * record.quantity
                self.trade_sum_qty[sid][index] += record.quantity
        for index, horizon in enumerate(self.VWAP_HORIZONS):
            trades = self.trade_windows[sid][index]
            cutoff = timestamp_ns - horizon
            while trades and trades[0][0] < cutoff:
                _, price, quantity = trades.popleft()
                self.trade_sum_pq[sid][index] -= price * quantity
                self.trade_sum_qty[sid][index] -= quantity
        if not compute:
            return TimeFeature(record, {}, {}, {})
        momentum: dict[int, int | None] = {}
        if record.midpoint_valid and record.midpoint > 0:
            timestamps = self.midpoint_timestamps[sid]
            values = self.midpoint_values[sid]
            for horizon in self.MOMENTUM_HORIZONS:
                index = bisect_right(timestamps, timestamp_ns - horizon) - 1
                reference = values[index] if index >= 0 else 0
                momentum[horizon] = bps_x100(record.midpoint - reference, reference) if reference else None
        else:
            momentum = {horizon: None for horizon in self.MOMENTUM_HORIZONS}
        time_vwap: dict[int, int | None] = {}
        vwap_delta: dict[int, int | None] = {}
        for index, horizon in enumerate(self.VWAP_HORIZONS):
            sum_pq = self.trade_sum_pq[sid][index]
            sum_qty = self.trade_sum_qty[sid][index]
            vwap = sum_pq // sum_qty if sum_qty else None
            time_vwap[horizon] = vwap
            vwap_delta[horizon] = bps_x100(record.midpoint - vwap, vwap) if vwap and record.midpoint_valid else None
        return TimeFeature(record, momentum, vwap_delta, time_vwap)


@dataclass(frozen=True)
class V2Config:
    name: str
    family: str
    momentum_horizon_ns: int = 1_000_000_000
    momentum_threshold_bps_x100: int = 0
    vwap_horizon_ns: int = 1_000_000_000
    vwap_threshold_bps_x100: int = 0
    imbalance_threshold_q15: int = 0
    max_spread_bps_x100: int | None = None
    min_rolling_volume: int | None = None
    cooldown_ns: int = 0
    persistence_ns: int = 0
    score_threshold: int = 0
    momentum_strong_threshold_bps_x100: int | None = None
    vwap_strong_threshold_bps_x100: int | None = None
    imbalance_strong_threshold_q15: int | None = None
    direction: str = "long"
    mean_reversion: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class V2Signal:
    action: int
    score: int
    reason_bits: int
    condition: bool
    momentum_value: int | None
    vwap_value: int | None


class V2Strategy:
    """Small stateful V2 policy; action 1 is long and 2 is short."""

    def __init__(self, config: V2Config, num_symbols: int = 4):
        self.config = config
        self.num_symbols = num_symbols
        self.reset()

    def reset(self) -> None:
        self.previous_condition = [False] * self.num_symbols
        self.condition_started = [None] * self.num_symbols
        self.last_emitted = [None] * self.num_symbols
        self.condition_emitted = [False] * self.num_symbols

    def evaluate(self, time_feature: TimeFeature) -> V2Signal:
        cfg = self.config
        record = time_feature.record
        sid = record.symbol_id
        momentum = time_feature.momentum_bps_x100.get(cfg.momentum_horizon_ns)
        vwap = time_feature.vwap_bps_x100.get(cfg.vwap_horizon_ns)
        imbalance = record.imbalance_q15 if record.validity & (1 << 6) else None
        spread = record.spread_bps_x100 if record.validity & (1 << 7) else None
        volume = record.rolling_volume
        if cfg.mean_reversion:
            momentum_pass = momentum is not None and momentum <= -cfg.momentum_threshold_bps_x100
            vwap_pass = vwap is not None and vwap <= -cfg.vwap_threshold_bps_x100
            imbalance_pass = imbalance is not None and imbalance <= -cfg.imbalance_threshold_q15
        else:
            momentum_pass = momentum is not None and momentum >= cfg.momentum_threshold_bps_x100
            vwap_pass = vwap is not None and vwap >= cfg.vwap_threshold_bps_x100
            imbalance_pass = imbalance is not None and imbalance >= cfg.imbalance_threshold_q15
        spread_pass = cfg.max_spread_bps_x100 is None or (spread is not None and spread <= cfg.max_spread_bps_x100)
        volume_pass = cfg.min_rolling_volume is None or volume >= cfg.min_rolling_volume
        persistence_pass = True
        directional = momentum_pass
        if cfg.family in {"V2-B", "V2-C", "V2-D", "V2-E", "V2-MR"}:
            directional = directional and vwap_pass and imbalance_pass
        if cfg.family in {"V2-C", "V2-D", "V2-E", "V2-MR"}:
            directional = directional and spread_pass and volume_pass
        if directional:
            if self.condition_started[sid] is None:
                self.condition_started[sid] = record.timestamp_ns
            if cfg.persistence_ns:
                persistence_pass = record.timestamp_ns - self.condition_started[sid] >= cfg.persistence_ns
        else:
            self.condition_started[sid] = None
            self.condition_emitted[sid] = False
        quality_score = int(momentum_pass) + int(vwap_pass) + int(imbalance_pass) + int(spread_pass) + int(volume_pass) + int(persistence_pass)
        if cfg.momentum_strong_threshold_bps_x100 is not None and momentum is not None:
            quality_score += int((momentum <= -cfg.momentum_strong_threshold_bps_x100 if cfg.mean_reversion else momentum >= cfg.momentum_strong_threshold_bps_x100))
        if cfg.vwap_strong_threshold_bps_x100 is not None and vwap is not None:
            quality_score += int((vwap <= -cfg.vwap_strong_threshold_bps_x100 if cfg.mean_reversion else vwap >= cfg.vwap_strong_threshold_bps_x100))
        if cfg.imbalance_strong_threshold_q15 is not None and imbalance is not None:
            quality_score += int((imbalance <= -cfg.imbalance_strong_threshold_q15 if cfg.mean_reversion else imbalance >= cfg.imbalance_strong_threshold_q15))
        if cfg.family == "V2-E":
            directional = directional and quality_score >= cfg.score_threshold
        cooldown_pass = cfg.cooldown_ns == 0 or self.last_emitted[sid] is None or record.timestamp_ns - self.last_emitted[sid] >= cfg.cooldown_ns
        action = 0
        if directional and persistence_pass and cooldown_pass and not self.condition_emitted[sid]:
            action = 2 if cfg.direction == "short" else 1
            self.last_emitted[sid] = record.timestamp_ns
            self.condition_emitted[sid] = True
        self.previous_condition[sid] = directional
        reason = 0
        reason |= 0x01 if momentum_pass else 0
        reason |= 0x02 if vwap_pass else 0
        reason |= 0x04 if imbalance_pass else 0
        reason |= 0x08 if spread_pass else 0
        reason |= 0x10 if volume_pass else 0
        reason |= 0x20 if persistence_pass else 0
        reason |= 0x40 if cfg.family == "V2-E" and quality_score >= cfg.score_threshold else 0
        return V2Signal(action, quality_score, reason, directional, momentum, vwap)


def quantile(values: Iterable[int | float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))]


def summarize_values(values: list[int | float], *, approximate: bool = True) -> dict:
    if not values:
        return {"count": 0, "mean": None, "median": None, "p90": None, "p95": None, "p99": None, "quantiles_approximate": approximate}
    return {
        "count": len(values),
        "mean": mean(values),
        "median": quantile(values, 0.5),
        "p90": quantile(values, 0.9),
        "p95": quantile(values, 0.95),
        "p99": quantile(values, 0.99),
        "min": min(values),
        "max": max(values),
        "quantiles_approximate": approximate,
    }
