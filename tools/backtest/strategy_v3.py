"""Software-only Strategy V3 relative-strength breakout primitives.

V3 is deliberately independent of the V1 reference model, V2 research code,
and FPGA RTL.  All state is causal and integer/fixed-point oriented so a
future implementation can use bounded histories, comparators, and counters.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import asdict, dataclass

from .research_cache import CacheRecord
from .strategy_v2 import bps_x100, trunc_div_signed


HORIZON_NAMES = {
    1_000_000_000: "1s",
    5_000_000_000: "5s",
    15_000_000_000: "15s",
    30_000_000_000: "30s",
    60_000_000_000: "60s",
    120_000_000_000: "120s",
    300_000_000_000: "300s",
}
FEATURE_HORIZONS_NS = tuple(h for h in HORIZON_NAMES if h != 1_000_000_000)
RANGE_HORIZONS_NS = (15_000_000_000, 30_000_000_000, 60_000_000_000, 120_000_000_000)
ACTIVITY_HORIZONS_NS = (5_000_000_000, 15_000_000_000, 30_000_000_000, 35_000_000_000, 60_000_000_000)
REGIME_HORIZONS_NS = (15_000_000_000, 60_000_000_000, 120_000_000_000)

# 0=SPY, 1=QQQ, 2=NVDA, 3=AMD.  SPY uses QQQ as a secondary context
# benchmark; no SPY-vs-SPY relative-strength feature is used.
BENCHMARK_BY_SYMBOL = {0: 1, 1: 0, 2: 1, 3: 1}


def _asof_return(history: tuple[list[int], list[int]], timestamp_ns: int, horizon_ns: int) -> int | None:
    timestamps, values = history
    if not timestamps:
        return None
    index = bisect_right(timestamps, timestamp_ns - horizon_ns) - 1
    if index < 0 or values[index] <= 0:
        return None
    return bps_x100(values[-1] - values[index], values[index])


def _asof_value(history: tuple[list[int], list[int]], timestamp_ns: int) -> int | None:
    timestamps, values = history
    index = bisect_right(timestamps, timestamp_ns) - 1
    return values[index] if index >= 0 else None


class _MonotonicRange:
    def __init__(self, horizon_ns: int):
        self.horizon_ns = horizon_ns
        self.maximum: deque[tuple[int, int]] = deque()
        self.minimum: deque[tuple[int, int]] = deque()

    def reset(self) -> None:
        self.maximum.clear()
        self.minimum.clear()

    def prior(self, timestamp_ns: int) -> tuple[int | None, int | None]:
        cutoff = timestamp_ns - self.horizon_ns
        while self.maximum and self.maximum[0][0] < cutoff:
            self.maximum.popleft()
        while self.minimum and self.minimum[0][0] < cutoff:
            self.minimum.popleft()
        return (self.maximum[0][1] if self.maximum else None, self.minimum[0][1] if self.minimum else None)

    def add(self, timestamp_ns: int, value: int) -> None:
        while self.maximum and self.maximum[-1][1] <= value:
            self.maximum.pop()
        while self.minimum and self.minimum[-1][1] >= value:
            self.minimum.pop()
        self.maximum.append((timestamp_ns, value))
        self.minimum.append((timestamp_ns, value))


class _ActivityWindow:
    def __init__(self, horizon_ns: int):
        self.horizon_ns = horizon_ns
        self.events: deque[tuple[int, int]] = deque()
        self.quantity = 0
        self.count = 0

    def reset(self) -> None:
        self.events.clear()
        self.quantity = 0
        self.count = 0

    def add(self, timestamp_ns: int, quantity: int) -> None:
        self.events.append((timestamp_ns, quantity))
        self.quantity += quantity
        self.count += 1

    def prune(self, timestamp_ns: int) -> None:
        cutoff = timestamp_ns - self.horizon_ns
        while self.events and self.events[0][0] < cutoff:
            _, quantity = self.events.popleft()
            self.quantity -= quantity
            self.count -= 1


@dataclass(frozen=True)
class V3Feature:
    record: CacheRecord
    stock_returns_bps_x100: dict[int, int | None]
    benchmark_returns_bps_x100: dict[int, int | None]
    relative_strength_bps_x100: dict[int, int | None]
    prior_high: dict[int, int | None]
    prior_low: dict[int, int | None]
    range_width_bps_x100: dict[int, int | None]
    volatility_bps_x100: int | None
    recent_volume: dict[int, int]
    recent_trade_count: dict[int, int]
    prior_30s_volume: int
    prior_30s_trade_count: int
    activity_ratio_q8: int
    regime: dict[int, str]
    benchmark_symbol_id: int | None

    @property
    def measured(self) -> bool:
        return self.record.measured

    @property
    def quote_valid(self) -> bool:
        record = self.record
        return bool(record.event_type == 1 and record.midpoint_valid and record.bid_price > 0 and record.ask_price > record.bid_price and record.spread_bps_x100 >= 0)


class CausalV3FeatureEngine:
    """Maintain per-symbol stock, benchmark, range, and activity histories."""

    def __init__(self, num_symbols: int = 4, regime_threshold_bps_x100: int = 50):
        self.num_symbols = num_symbols
        self.regime_threshold_bps_x100 = regime_threshold_bps_x100
        self.reset()

    def reset(self) -> None:
        self.segment_index = None
        self.midpoints = [([], []) for _ in range(self.num_symbols)]
        self.ranges = [[_MonotonicRange(horizon) for horizon in RANGE_HORIZONS_NS] for _ in range(self.num_symbols)]
        self.activities = [[_ActivityWindow(horizon) for horizon in ACTIVITY_HORIZONS_NS] for _ in range(self.num_symbols)]

    def _reset_if_segment_changed(self, record: CacheRecord) -> None:
        if self.segment_index != record.segment_index:
            self.reset()
            self.segment_index = record.segment_index

    def process(self, record: CacheRecord) -> V3Feature:
        self._reset_if_segment_changed(record)
        sid = record.symbol_id
        now = record.timestamp_ns
        activities = self.activities[sid]
        if record.event_type == 2 and record.quantity > 0:
            for activity in activities:
                activity.add(now, record.quantity)
        for activity in activities:
            activity.prune(now)

        stock_history = self.midpoints[sid]
        benchmark_sid = BENCHMARK_BY_SYMBOL.get(sid)
        benchmark_history = self.midpoints[benchmark_sid] if benchmark_sid is not None else ([], [])

        prior_high: dict[int, int | None] = {}
        prior_low: dict[int, int | None] = {}
        widths: dict[int, int | None] = {}
        for horizon, range_state in zip(RANGE_HORIZONS_NS, self.ranges[sid]):
            high, low = range_state.prior(now)
            prior_high[horizon] = high
            prior_low[horizon] = low
            widths[horizon] = bps_x100(high - low, low) if high is not None and low is not None and low > 0 else None

        # The current quote is causal information for the stock return, while
        # the range above intentionally remains prior-only.  Append before
        # calculating returns so a quote exactly at the horizon endpoint is
        # measured against the prior as-of quote, without exposing future data.
        current_midpoint_appended = False
        if record.midpoint_valid and record.midpoint > 0:
            stock_history[0].append(now)
            stock_history[1].append(record.midpoint)
            current_midpoint_appended = True

        volatility = widths.get(60_000_000_000)
        stock_returns = {horizon: _asof_return(stock_history, now, horizon) for horizon in FEATURE_HORIZONS_NS}
        benchmark_returns = {horizon: _asof_return(benchmark_history, now, horizon) for horizon in FEATURE_HORIZONS_NS}
        relative = {
            horizon: stock_returns[horizon] - benchmark_returns[horizon]
            if stock_returns[horizon] is not None and benchmark_returns[horizon] is not None else None
            for horizon in FEATURE_HORIZONS_NS
        }
        recent_volume = {horizon: activities[index].quantity for index, horizon in enumerate(ACTIVITY_HORIZONS_NS[:4])}
        recent_count = {horizon: activities[index].count for index, horizon in enumerate(ACTIVITY_HORIZONS_NS[:4])}
        prior_volume = max(0, activities[4].quantity - activities[0].quantity)
        prior_count = max(0, activities[4].count - activities[0].count)
        activity_ratio = min(65_535, (recent_volume[5_000_000_000] * 6 * 256) // max(1, prior_volume))
        regime = {}
        for horizon in REGIME_HORIZONS_NS:
            value = benchmark_returns.get(horizon)
            if value is None or abs(value) < self.regime_threshold_bps_x100:
                regime[horizon] = "NEUTRAL"
            else:
                regime[horizon] = "UP" if value > 0 else "DOWN"

        feature = V3Feature(
            record=record,
            stock_returns_bps_x100=stock_returns,
            benchmark_returns_bps_x100=benchmark_returns,
            relative_strength_bps_x100=relative,
            prior_high=prior_high,
            prior_low=prior_low,
            range_width_bps_x100=widths,
            volatility_bps_x100=volatility,
            recent_volume=recent_volume,
            recent_trade_count=recent_count,
            prior_30s_volume=prior_volume,
            prior_30s_trade_count=prior_count,
            activity_ratio_q8=activity_ratio,
            regime=regime,
            benchmark_symbol_id=benchmark_sid,
        )

        if current_midpoint_appended:
            for range_state in self.ranges[sid]:
                range_state.add(now, record.midpoint)
        return feature


@dataclass(frozen=True)
class V3Config:
    name: str
    family: str
    range_horizon_ns: int
    breakout_buffer_bps_x100: int = 0
    spread_limit_bps_x100: int = 500
    regime_horizon_ns: int = 60_000_000_000
    regime_threshold_bps_x100: int = 50
    regime_mode: str = "none"
    relative_horizon_ns: int = 30_000_000_000
    relative_threshold_bps_x100: int = 0
    volatility_horizon_ns: int = 60_000_000_000
    normalized_threshold: int = 0
    activity_threshold_q8: int = 0
    imbalance_threshold_q15: int | None = None
    persistence_ns: int = 0
    cooldown_ns: int = 0
    score_threshold: int = 0
    direction: str = "both"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class V3Signal:
    action: int
    direction: str
    score: int
    reason_bits: int


class V3Strategy:
    """Interpret one V3 configuration with causal persistence/cooldown state."""

    def __init__(self, config: V3Config, num_symbols: int = 4):
        self.config = config
        self.num_symbols = num_symbols
        self.reset()

    def reset(self) -> None:
        self.segment_index = None
        self.condition_since = [None] * self.num_symbols
        self.condition_direction = [0] * self.num_symbols
        self.last_emit = [None] * self.num_symbols

    def _breakout(self, feature: V3Feature) -> tuple[int, int, int]:
        record = feature.record
        high = feature.prior_high.get(self.config.range_horizon_ns)
        low = feature.prior_low.get(self.config.range_horizon_ns)
        if not feature.quote_valid or high is None or low is None:
            return 0, 0, 0
        buffer_up = high * self.config.breakout_buffer_bps_x100 // 1_000_000
        buffer_down = low * self.config.breakout_buffer_bps_x100 // 1_000_000
        long_break = record.midpoint > high + buffer_up
        short_break = record.midpoint < low - buffer_down
        direction = 1 if long_break and not short_break else -1 if short_break and not long_break else 0
        if direction == 1:
            distance = record.midpoint - high
        elif direction == -1:
            distance = low - record.midpoint
        else:
            distance = 0
        return direction, distance, high if direction == 1 else low

    def _factor_gate(self, feature: V3Feature, direction: int, distance: int, reference: int) -> tuple[bool, int, int]:
        config = self.config
        relative = feature.relative_strength_bps_x100.get(config.relative_horizon_ns)
        if config.family in {"V3-C", "V3-D", "V3-E", "V3-F", "V3-G"}:
            if relative is None or (relative < config.relative_threshold_bps_x100 if direction == 1 else relative > -config.relative_threshold_bps_x100):
                return False, 0, 0
        if config.family in {"V3-B", "V3-C", "V3-D", "V3-E", "V3-F", "V3-G"} and config.regime_mode != "none":
            regime = feature.regime.get(config.regime_horizon_ns, "NEUTRAL")
            if config.regime_mode == "permissive" and ((direction == 1 and regime == "DOWN") or (direction == -1 and regime == "UP")):
                return False, 0, 0
            if config.regime_mode == "confirming" and ((direction == 1 and regime != "UP") or (direction == -1 and regime != "DOWN")):
                return False, 0, 0
        volatility = feature.range_width_bps_x100.get(config.volatility_horizon_ns) or 0
        if config.family in {"V3-D", "V3-E", "V3-F", "V3-G"}:
            if relative is None or volatility <= 0 or abs(relative) * 100 < volatility * config.normalized_threshold:
                return False, 0, 0
        if config.family in {"V3-E", "V3-F", "V3-G"} and feature.activity_ratio_q8 < config.activity_threshold_q8:
            return False, 0, 0
        if config.imbalance_threshold_q15 is not None:
            imbalance = feature.record.imbalance_q15
            if direction == 1 and imbalance < config.imbalance_threshold_q15:
                return False, 0, 0
            if direction == -1 and imbalance > -config.imbalance_threshold_q15:
                return False, 0, 0

        spread_score = int(feature.record.spread_bps_x100 <= config.spread_limit_bps_x100)
        breakout_score = 2 if volatility > 0 and distance * 100 >= volatility else 1
        relative_score = 2 if relative is not None and abs(relative) >= max(1, config.relative_threshold_bps_x100 * 2) else int(relative is not None)
        regime_score = int(feature.regime.get(config.regime_horizon_ns) == ("UP" if direction == 1 else "DOWN"))
        volatility_score = 2 if volatility > 0 and distance * 100 >= volatility * 2 else int(volatility > 0)
        activity_score = int(feature.activity_ratio_q8 >= 256)
        score = breakout_score + relative_score + regime_score + volatility_score + activity_score + spread_score
        if config.family == "V3-G" and score < config.score_threshold:
            return False, score, 0
        reason_bits = (1 if breakout_score else 0) | (2 if relative_score else 0) | (4 if regime_score else 0) | (8 if volatility_score else 0) | (16 if activity_score else 0) | (32 if spread_score else 0)
        return True, score, reason_bits

    def evaluate(self, feature: V3Feature) -> V3Signal:
        if self.segment_index != feature.record.segment_index:
            self.reset()
            self.segment_index = feature.record.segment_index
        sid = feature.record.symbol_id
        if feature.record.event_type != 1:
            return V3Signal(0, "none", 0, 0)
        direction, distance, reference = self._breakout(feature)
        if direction == 0 or (self.config.direction == "long" and direction != 1) or (self.config.direction == "short" and direction != -1):
            self.condition_since[sid] = None
            self.condition_direction[sid] = 0
            return V3Signal(0, "none", 0, 0)
        allowed, score, reason_bits = self._factor_gate(feature, direction, distance, reference)
        if not allowed:
            self.condition_since[sid] = None
            self.condition_direction[sid] = 0
            return V3Signal(0, "none", score, reason_bits)
        now = feature.record.timestamp_ns
        if self.condition_direction[sid] != direction:
            self.condition_direction[sid] = direction
            self.condition_since[sid] = now
            self.last_emit[sid] = None
        since = self.condition_since[sid] if self.condition_since[sid] is not None else now
        if now - since < self.config.persistence_ns:
            return V3Signal(0, "long" if direction == 1 else "short", score, reason_bits)
        if not feature.measured:
            return V3Signal(0, "long" if direction == 1 else "short", score, reason_bits)
        if self.last_emit[sid] is not None:
            if self.config.cooldown_ns == 0:
                return V3Signal(0, "long" if direction == 1 else "short", score, reason_bits)
            if now - self.last_emit[sid] < self.config.cooldown_ns:
                return V3Signal(0, "long" if direction == 1 else "short", score, reason_bits)
        self.last_emit[sid] = now
        return V3Signal(direction, "long" if direction == 1 else "short", score, reason_bits)


def normalize_strength(relative_strength_bps_x100: int | None, volatility_bps_x100: int | None) -> int | None:
    if relative_strength_bps_x100 is None or volatility_bps_x100 is None or volatility_bps_x100 <= 0:
        return None
    return trunc_div_signed(relative_strength_bps_x100 * 100, volatility_bps_x100)
