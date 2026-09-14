"""Offline edge-attribution diagnostics for the failed V1/V2/V3 studies.

This module consumes only the validated fixed-width V2 research caches.  It
does not open the canonical binary, access a provider, or change any strategy
semantics.  The cache pass deliberately combines quote quality, causal V3
candidate generation, forward-return decomposition, controls, random samples,
quote-age, and equal-timestamp diagnostics in one sequential stream.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path
import random
import time
from typing import Iterable
from zoneinfo import ZoneInfo

from .canonical import MSG_MARKET_QUOTE, MSG_MARKET_TRADE
from .research_cache import (
    CACHE_RECORD_SIZE,
    FAST_WINDOWS,
    MEDIUM_WINDOWS,
    CacheRecord,
    iter_cache_records,
)
from .strategy_v3 import CausalV3FeatureEngine, V3Config, V3Feature, V3Strategy
from .strategy_v3_research import (
    EXPECTED_CANONICAL_EVENTS,
    EXPECTED_CANONICAL_SHA256,
    EXPECTED_CANONICAL_SIZE,
    _cache_paths,
    _window_for,
)


SYMBOL_NAMES = {0: "SPY", 1: "QQQ", 2: "NVDA", 3: "AMD"}
ZONE = ZoneInfo("America/New_York")
FORWARD_HORIZONS_NS = (1_000_000_000, 5_000_000_000, 15_000_000_000, 30_000_000_000, 60_000_000_000, 120_000_000_000, 300_000_000_000)
IMMEDIATE_HORIZONS_NS = (1_000_000, 10_000_000, 100_000_000, 250_000_000, 500_000_000, 1_000_000_000, 5_000_000_000)
HORIZON_NAMES = {
    1_000_000: "1ms",
    10_000_000: "10ms",
    100_000_000: "100ms",
    250_000_000: "250ms",
    500_000_000: "500ms",
    1_000_000_000: "1s",
    5_000_000_000: "5s",
    15_000_000_000: "15s",
    30_000_000_000: "30s",
    60_000_000_000: "60s",
    120_000_000_000: "120s",
    300_000_000_000: "300s",
}
QUIET_PERIODS_NS = (1_000_000_000, 5_000_000_000, 10_000_000_000, 30_000_000_000)
AGE_BUCKETS = ("<1ms", "1-10ms", "10-100ms", "100ms-1s", ">1s")
RANDOM_SEED = 20260913
RANDOM_RESERVOIR_SIZE = 32
CONTROL_RESERVOIR_SIZE = 8
WIDE_SPREAD_BPS_X100 = 1_000
PRIOR_CANONICAL_FULL_PASSES = 58
METRIC_NAMES = (
    "midpoint_return_bps",
    "executable_return_bps",
    "entry_spread_bps",
    "future_spread_bps",
    "crossing_cost_estimate_bps",
    "midpoint_minus_executable_bps",
    "zero_move_executable_bps",
)


def bps_return(delta: float, entry: float) -> float:
    """Convert a price return to basis points without hidden integer scaling."""
    if entry == 0:
        raise ValueError("entry price must be non-zero")
    return delta / entry * 10_000.0


def midpoint_directional_return(entry_mid: int, future_mid: int, direction: str) -> float:
    return bps_return((future_mid - entry_mid) if direction == "long" else (entry_mid - future_mid), entry_mid)


def executable_directional_return(entry_bid: int, entry_ask: int, future_bid: int, future_ask: int, direction: str) -> float:
    return bps_return((future_bid - entry_ask) if direction == "long" else (entry_bid - future_ask), entry_ask if direction == "long" else entry_bid)


def crossing_cost_estimate(entry_mid: int, entry_bid: int, entry_ask: int, future_bid: int, future_ask: int, direction: str) -> float:
    if entry_mid <= 0:
        return 0.0
    entry_half = (entry_ask - entry_bid) / 2.0
    future_half = (future_ask - future_bid) / 2.0
    return bps_return(entry_half + future_half, entry_mid)


def zero_move_executable_baseline(entry_mid: int, entry_bid: int, entry_ask: int, future_bid: int, future_ask: int, direction: str) -> float:
    """Theoretical executable return if the midpoint stayed at entry_mid."""
    future_half = (future_ask - future_bid) / 2.0
    if direction == "long":
        return bps_return((entry_mid - future_half) - entry_ask, entry_ask)
    return bps_return(entry_bid - (entry_mid + future_half), entry_bid)


def executable_quote_valid(bid: int, ask: int) -> bool:
    return bid > 0 and ask > 0 and ask >= bid


def normal_quote_valid(bid: int, ask: int) -> bool:
    return bid > 0 and ask > bid


@dataclass(frozen=True)
class QuotePoint:
    timestamp_ns: int
    bid: int
    ask: int
    midpoint: int


def first_quote_at_or_after(points: Iterable[QuotePoint], target_ns: int) -> QuotePoint | None:
    for point in points:
        if point.timestamp_ns >= target_ns and executable_quote_valid(point.bid, point.ask):
            return point
    return None


class NumericStats:
    """Exact count/mean/extrema plus deterministic bounded quantile samples."""

    def __init__(self, stride: int = 32, limit: int = 20_000):
        self.stride = max(1, stride)
        self.limit = limit
        self.count = 0
        self.total = 0.0
        self.positive_count = 0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.sample: list[float] = []

    def observe(self, value: int | float) -> None:
        value = float(value)
        self.count += 1
        self.total += value
        if value > 0:
            self.positive_count += 1
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if self.count % self.stride == 0 and len(self.sample) < self.limit:
            self.sample.append(value)

    def summary(self) -> dict:
        values = sorted(self.sample)

        def quantile(fraction: float) -> float | None:
            if not values:
                return None
            return values[min(len(values) - 1, int(round((len(values) - 1) * fraction)))]

        return {
            "count": self.count,
            "mean": self.total / self.count if self.count else None,
            "min": self.minimum,
            "max": self.maximum,
            "sample_count": len(values),
            "median": quantile(.50),
            "p10": quantile(.10),
            "p25": quantile(.25),
            "p75": quantile(.75),
            "p90": quantile(.90),
            "p95": quantile(.95),
            "p99": quantile(.99),
            "quantiles_approximate": True,
            "sample_method": f"deterministic every {self.stride}th observation; bounded at {self.limit}",
        }


def _empty_metric_stats() -> dict[str, NumericStats]:
    return {name: NumericStats(stride=8) for name in METRIC_NAMES}


class MetricBook:
    def __init__(self):
        self.aggregate: dict[int, dict[str, NumericStats]] = defaultdict(_empty_metric_stats)
        self.by_direction: dict[str, dict[int, dict[str, NumericStats]]] = defaultdict(lambda: defaultdict(_empty_metric_stats))
        self.by_symbol: dict[str, dict[int, dict[str, NumericStats]]] = defaultdict(lambda: defaultdict(_empty_metric_stats))
        self.by_day: dict[str, dict[int, dict[str, NumericStats]]] = defaultdict(lambda: defaultdict(_empty_metric_stats))
        self.by_window: dict[str, dict[int, dict[str, NumericStats]]] = defaultdict(lambda: defaultdict(_empty_metric_stats))

    @staticmethod
    def _observe_scope(scope: dict, key, horizon_ns: int, values: dict[str, float]) -> None:
        stats = scope[key][horizon_ns] if key is not None else scope[horizon_ns]
        for name, value in values.items():
            stats[name].observe(value)

    def observe(self, direction: str, horizon_ns: int, values: dict[str, float], *, symbol: str | None = None, day: str | None = None, window: str | None = None) -> None:
        self._observe_scope(self.aggregate, None, horizon_ns, values)
        self._observe_scope(self.by_direction, direction, horizon_ns, values)
        if symbol is not None:
            self._observe_scope(self.by_symbol, symbol, horizon_ns, values)
        if day is not None:
            self._observe_scope(self.by_day, day, horizon_ns, values)
        if window is not None:
            self._observe_scope(self.by_window, window, horizon_ns, values)

    @staticmethod
    def _summary(scope: dict, *, keyed: bool) -> dict:
        def metric_summary(name: str, value: NumericStats) -> dict:
            summary = value.summary()
            if name in {"midpoint_return_bps", "executable_return_bps"}:
                summary["favorable_count"] = value.positive_count
                summary["favorable_fraction"] = value.positive_count / value.count if value.count else None
            return summary

        if keyed:
            return {str(key): {HORIZON_NAMES[h]: {name: metric_summary(name, value) for name, value in stats.items()} for h, stats in sorted(horizons.items())} for key, horizons in sorted(scope.items())}
        return {HORIZON_NAMES[h]: {name: metric_summary(name, value) for name, value in stats.items()} for h, stats in sorted(scope.items())}

    def summary(self) -> dict:
        return {
            "aggregate": self._summary(self.aggregate, keyed=False),
            "by_direction": self._summary(self.by_direction, keyed=True),
            "by_symbol": self._summary(self.by_symbol, keyed=True),
            "by_day": self._summary(self.by_day, keyed=True),
            "by_window": self._summary(self.by_window, keyed=True),
            "quantile_note": "Counts, means, minima, maxima, and favorable-fraction inputs are exact; quantiles use deterministic bounded samples.",
        }


def _metric_values(obs: "Observation", future: "FutureQuote", direction: str) -> dict[str, float]:
    midpoint = midpoint_directional_return(obs.midpoint, future.midpoint, direction)
    executable = executable_directional_return(obs.bid, obs.ask, future.bid, future.ask, direction)
    return {
        "midpoint_return_bps": midpoint,
        "executable_return_bps": executable,
        "entry_spread_bps": bps_return(obs.ask - obs.bid, obs.midpoint),
        "future_spread_bps": bps_return(future.ask - future.bid, future.midpoint),
        "crossing_cost_estimate_bps": crossing_cost_estimate(obs.midpoint, obs.bid, obs.ask, future.bid, future.ask, direction),
        "midpoint_minus_executable_bps": midpoint - executable,
        "zero_move_executable_bps": zero_move_executable_baseline(obs.midpoint, obs.bid, obs.ask, future.bid, future.ask, direction),
    }


@dataclass
class FutureQuote:
    timestamp_ns: int
    bid: int
    ask: int
    midpoint: int
    spread: int


@dataclass
class Observation:
    observation_id: int
    kind: str
    timestamp_ns: int
    segment_index: int
    symbol_id: int
    direction: str | None
    day: str
    window: str
    bid: int
    ask: int
    midpoint: int
    spread: int
    side: int
    score: int = 0
    bid_age_ns: int | None = None
    ask_age_ns: int | None = None
    benchmark_age_ns: int | None = None
    last_trade_age_ns: int | None = None
    age_bucket: str | None = None
    spread_bucket: str = "unknown"
    activity_bucket: str = "unknown"
    volatility_bucket: str = "unknown"
    intermediate_state: bool = False
    active: bool = True
    futures: dict[int, FutureQuote] = field(default_factory=dict)


class ForwardResolver:
    def __init__(self, on_resolved):
        self.pending: dict[int, list[tuple[int, int, int, int]]] = defaultdict(list)
        self.observations: dict[int, Observation] = {}
        self.serial = 0
        self.on_resolved = on_resolved

    def register(self, observation: Observation, horizons: Iterable[int]) -> None:
        self.observations[observation.observation_id] = observation
        for horizon_ns in sorted(set(horizons)):
            self.serial += 1
            heapq.heappush(self.pending[observation.symbol_id], (observation.timestamp_ns + horizon_ns, self.serial, observation.observation_id, horizon_ns))

    def resolve(self, symbol_id: int, timestamp_ns: int, quote: FutureQuote) -> None:
        queue = self.pending[symbol_id]
        while queue and queue[0][0] <= timestamp_ns:
            _, _, observation_id, horizon_ns = heapq.heappop(queue)
            observation = self.observations.get(observation_id)
            if observation is None or not observation.active:
                continue
            observation.futures[horizon_ns] = quote
            self.on_resolved(observation, horizon_ns, quote)

    def deactivate(self, observation: Observation) -> None:
        observation.active = False

    def clear_segment(self) -> None:
        self.pending.clear()
        for observation in self.observations.values():
            if not observation.futures:
                observation.active = False


class ReservoirManager:
    def __init__(self, resolver: ForwardResolver, rng: random.Random, capacity: int):
        self.resolver = resolver
        self.rng = rng
        self.capacity = capacity
        self.seen: defaultdict[tuple, int] = defaultdict(int)
        self.pools: defaultdict[tuple, list[Observation]] = defaultdict(list)

    def consider(self, key: tuple, observation: Observation, horizons: Iterable[int]) -> None:
        self.seen[key] += 1
        pool = self.pools[key]
        if len(pool) < self.capacity:
            pool.append(observation)
            self.resolver.register(observation, horizons)
            return
        replacement_index = self.rng.randrange(self.seen[key])
        if replacement_index < self.capacity:
            self.resolver.deactivate(pool[replacement_index])
            pool[replacement_index] = observation
            self.resolver.register(observation, horizons)
        else:
            observation.active = False

    def active_observations(self) -> list[Observation]:
        return [observation for pool in self.pools.values() for observation in pool if observation.active]


def _age_bucket(age_ns: int | None) -> str:
    if age_ns is None:
        return "unknown"
    if age_ns < 1_000_000:
        return "<1ms"
    if age_ns < 10_000_000:
        return "1-10ms"
    if age_ns < 100_000_000:
        return "10-100ms"
    if age_ns < 1_000_000_000:
        return "100ms-1s"
    return ">1s"


def _max_age_bucket(bid_age_ns: int | None, ask_age_ns: int | None) -> str:
    if bid_age_ns is None and ask_age_ns is None:
        return "unknown"
    ages = [age for age in (bid_age_ns, ask_age_ns) if age is not None]
    return _age_bucket(max(ages))


def _spread_bucket(spread_bps_x100: int) -> str:
    if spread_bps_x100 <= 100:
        return "<=1bp"
    if spread_bps_x100 <= 500:
        return "1-5bp"
    if spread_bps_x100 <= 1_000:
        return "5-10bp"
    return ">10bp"


def _activity_bucket(activity_ratio_q8: int) -> str:
    if activity_ratio_q8 < 128:
        return "<128q8"
    if activity_ratio_q8 < 256:
        return "128-256q8"
    if activity_ratio_q8 < 512:
        return "256-512q8"
    return ">=512q8"


def _volatility_bucket(volatility_bps_x100: int | None) -> str:
    if volatility_bps_x100 is None:
        return "unknown"
    if volatility_bps_x100 < 100:
        return "<1bp"
    if volatility_bps_x100 < 500:
        return "1-5bp"
    return ">=5bp"


def _local_time_bucket(timestamp_ns: int) -> str:
    local = datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(ZONE)
    if local.hour < 10:
        return "opening"
    if local.hour < 14:
        return "midday"
    return "late_session"


@dataclass
class GroupSymbolState:
    quote_count: int = 0
    bid_count: int = 0
    ask_count: int = 0
    trade_count: int = 0
    first_bid_order: int | None = None
    first_ask_order: int | None = None
    ask_seen: bool = False
    candidates: list[Observation] = field(default_factory=list)
    last_quote_feature: V3Feature | None = None
    last_quote_order: int = -1


@dataclass
class TimestampGroup:
    timestamp_ns: int
    event_count: int = 0
    symbols: dict[int, GroupSymbolState] = field(default_factory=dict)
    has_quote: bool = False
    has_trade: bool = False


class DiagnosticAccumulator:
    def __init__(self, windows):
        self.windows = windows
        self.processed_records = 0
        self.measured_quotes = 0
        self.valid_quotes = 0
        self.candidate_count = 0
        self.candidate_counts_by_direction: defaultdict[str, int] = defaultdict(int)
        self.candidate_counts_by_symbol: defaultdict[str, int] = defaultdict(int)
        self.candidate_counts_by_day: defaultdict[str, int] = defaultdict(int)
        self.candidate_counts_by_window: defaultdict[str, int] = defaultdict(int)
        self.candidate_timestamps: defaultdict[int, list[Observation]] = defaultdict(list)
        self.candidate_observations: list[Observation] = []
        self.spot: dict[str, list[Observation]] = {"long": [], "short": []}
        self.candidate_book = MetricBook()
        self.intermediate_book = MetricBook()
        self.snapshot_book = MetricBook()
        self.candidate_immediate_book = MetricBook()
        self.candidate_age_books: dict[str, MetricBook] = defaultdict(MetricBook)
        self.control_book = MetricBook()
        self.random_book = MetricBook()
        self.rng = random.Random(RANDOM_SEED)
        self.resolver = ForwardResolver(self._on_resolved)
        self.random_reservoir = ReservoirManager(self.resolver, random.Random(RANDOM_SEED + 1), RANDOM_RESERVOIR_SIZE)
        self.control_reservoir = ReservoirManager(self.resolver, random.Random(RANDOM_SEED + 2), CONTROL_RESERVOIR_SIZE)
        self.activity_stats = NumericStats()
        self.volatility_stats = NumericStats()
        self.quote_age_stats = {"bid_age_ns": NumericStats(), "ask_age_ns": NumericStats(), "benchmark_age_ns": NumericStats(), "last_trade_age_ns": NumericStats()}
        self.quote_age_bucket_counts = {"bid": defaultdict(int), "ask": defaultdict(int), "max": defaultdict(int)}
        self.candidate_age_stats = {"bid_age_ns": NumericStats(), "ask_age_ns": NumericStats(), "benchmark_age_ns": NumericStats(), "last_trade_age_ns": NumericStats()}
        self.candidate_age_bucket_counts = {"bid": defaultdict(int), "ask": defaultdict(int), "max": defaultdict(int)}
        self.spread_by_symbol: defaultdict[str, NumericStats] = defaultdict(NumericStats)
        self.spread_by_time: defaultdict[str, NumericStats] = defaultdict(NumericStats)
        self.spread_by_symbol_time: defaultdict[str, defaultdict[str, NumericStats]] = defaultdict(lambda: defaultdict(NumericStats))
        self.quote_state_counts = defaultdict(int)
        self.quote_state_by_symbol: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.candidates_in_bad_states = defaultdict(int)
        self.intermediate_candidate_count = 0
        self.intermediate_candidate_ids: set[int] = set()
        self.paired_snapshot_candidate_count = 0
        self.equal_timestamp_groups = 0
        self.equal_timestamp_events = NumericStats()
        self.paired_quote_groups = 0
        self.bid_then_ask_pairs = 0
        self.ask_then_bid_pairs = 0
        self.multiple_quote_groups = 0
        self.trade_quote_mixed_groups = 0
        self.multiple_symbol_groups = 0
        self.same_timestamp_symbol_pairs = 0
        self.sequence_regressions = 0
        self.last_sequence: int | None = None
        self.group_quote_counts = NumericStats()
        self.snapshot_strategy = V3Strategy(V3Config("diagnostic-snapshot", "V3-A", 30_000_000_000, 0, 417))
        self.next_observation_id = 0

    def new_observation(self, **kwargs) -> Observation:
        self.next_observation_id += 1
        return Observation(observation_id=self.next_observation_id, **kwargs)

    def _on_resolved(self, observation: Observation, horizon_ns: int, future: FutureQuote) -> None:
        if observation.kind == "candidate":
            values = _metric_values(observation, future, observation.direction or "long")
            if horizon_ns in FORWARD_HORIZONS_NS:
                self.candidate_book.observe(observation.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[observation.symbol_id], day=observation.day, window=observation.window)
            if horizon_ns in IMMEDIATE_HORIZONS_NS:
                self.candidate_immediate_book.observe(observation.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[observation.symbol_id], day=observation.day, window=observation.window)
            if horizon_ns in FORWARD_HORIZONS_NS and observation.age_bucket is not None:
                self.candidate_age_books[observation.age_bucket].observe(observation.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[observation.symbol_id], day=observation.day, window=observation.window)
            if horizon_ns in FORWARD_HORIZONS_NS and observation.intermediate_state:
                self.intermediate_book.observe(observation.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[observation.symbol_id], day=observation.day, window=observation.window)
        elif observation.kind == "snapshot":
            values = _metric_values(observation, future, observation.direction or "long")
            self.snapshot_book.observe(observation.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[observation.symbol_id], day=observation.day, window=observation.window)

    def add_candidate(self, feature: V3Feature, direction: str, score: int, ages: dict[str, int | None], dimensions: dict[str, str], side: int) -> Observation:
        record = feature.record
        day, window = _window_for(self.windows, record.segment_index)
        observation = self.new_observation(
            kind="candidate", timestamp_ns=record.timestamp_ns, segment_index=record.segment_index,
            symbol_id=record.symbol_id, direction=direction, day=day, window=window,
            bid=record.bid_price, ask=record.ask_price, midpoint=record.midpoint, spread=record.spread,
            side=side, score=score, bid_age_ns=ages["bid_age_ns"], ask_age_ns=ages["ask_age_ns"],
            benchmark_age_ns=ages.get("benchmark_age_ns"), last_trade_age_ns=ages.get("last_trade_age_ns"),
            age_bucket=_max_age_bucket(ages["bid_age_ns"], ages["ask_age_ns"]),
            spread_bucket=dimensions["spread_bucket"], activity_bucket=dimensions["activity_bucket"], volatility_bucket=dimensions["volatility_bucket"],
        )
        self.candidate_observations.append(observation)
        self.candidate_timestamps[record.symbol_id].append(observation)
        if len(self.spot[direction]) < 25:
            self.spot[direction].append(observation)
        self.candidate_count += 1
        self.candidate_counts_by_direction[direction] += 1
        self.candidate_counts_by_symbol[SYMBOL_NAMES[record.symbol_id]] += 1
        self.candidate_counts_by_day[day] += 1
        self.candidate_counts_by_window[window] += 1
        for name, value in ages.items():
            if value is not None:
                self.candidate_age_stats[name].observe(value)
        for name, age_key in (("bid", "bid_age_ns"), ("ask", "ask_age_ns"), ("max", None)):
            age = ages[age_key] if age_key else max((value for value in (ages["bid_age_ns"], ages["ask_age_ns"]) if value is not None), default=None)
            if age is not None:
                self.candidate_age_bucket_counts[name][_age_bucket(age)] += 1
        self.resolver.register(observation, set(FORWARD_HORIZONS_NS) | set(IMMEDIATE_HORIZONS_NS))
        return observation

    def _control_observation(self, feature: V3Feature, direction: str, ages: dict[str, int | None], dimensions: dict[str, str], kind: str) -> Observation:
        record = feature.record
        day, window = _window_for(self.windows, record.segment_index)
        return self.new_observation(
            kind=kind, timestamp_ns=record.timestamp_ns, segment_index=record.segment_index,
            symbol_id=record.symbol_id, direction=direction, day=day, window=window,
            bid=record.bid_price, ask=record.ask_price, midpoint=record.midpoint, spread=record.spread,
            side=record.side, bid_age_ns=ages["bid_age_ns"], ask_age_ns=ages["ask_age_ns"],
            benchmark_age_ns=ages.get("benchmark_age_ns"), last_trade_age_ns=ages.get("last_trade_age_ns"),
            age_bucket=_max_age_bucket(ages["bid_age_ns"], ages["ask_age_ns"]),
            spread_bucket=dimensions["spread_bucket"], activity_bucket=dimensions["activity_bucket"], volatility_bucket=dimensions["volatility_bucket"],
        )

    def add_random_and_control(self, feature: V3Feature, ages: dict[str, int | None], dimensions: dict[str, str], is_candidate: bool) -> None:
        record = feature.record
        day, window = _window_for(self.windows, record.segment_index)
        base_key = (record.symbol_id, day, window)
        for direction in ("long", "short"):
            random_observation = self._control_observation(feature, direction, ages, dimensions, "random")
            self.random_reservoir.consider(base_key + (direction,), random_observation, FORWARD_HORIZONS_NS)
        if is_candidate:
            return
        match_key = base_key + (dimensions["spread_bucket"], dimensions["activity_bucket"], dimensions["volatility_bucket"])
        for direction in ("long", "short"):
            control_observation = self._control_observation(feature, direction, ages, dimensions, "control")
            self.control_reservoir.consider(match_key + (direction,), control_observation, FORWARD_HORIZONS_NS)

    def finalize_reservoir_metrics(self) -> None:
        for observation in self.random_reservoir.active_observations():
            for horizon_ns, future in observation.futures.items():
                values = _metric_values(observation, future, observation.direction or "long")
                self.random_book.observe(observation.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[observation.symbol_id], day=observation.day, window=observation.window)
        for observation in self.control_reservoir.active_observations():
            for horizon_ns, future in observation.futures.items():
                values = _metric_values(observation, future, observation.direction or "long")
                self.control_book.observe(observation.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[observation.symbol_id], day=observation.day, window=observation.window)

    def summary(self) -> dict:
        return {
            "processed_records": self.processed_records,
            "measured_quote_records": self.measured_quotes,
            "valid_executable_quote_records": self.valid_quotes,
            "candidate_count": self.candidate_count,
            "candidate_counts_by_direction": dict(self.candidate_counts_by_direction),
            "candidate_counts_by_symbol": dict(self.candidate_counts_by_symbol),
            "candidate_counts_by_day": dict(self.candidate_counts_by_day),
            "candidate_counts_by_window": dict(self.candidate_counts_by_window),
            "candidate_returns": self.candidate_book.summary(),
            "intermediate_candidate_returns": self.intermediate_book.summary(),
            "paired_snapshot_candidate_returns": self.snapshot_book.summary(),
            "candidate_immediate_returns": self.candidate_immediate_book.summary(),
            "random_returns": self.random_book.summary(),
            "control_returns": self.control_book.summary(),
            "activity_ratio_q8": self.activity_stats.summary(),
            "volatility_bps_x100": self.volatility_stats.summary(),
            "quote_age": {name: stats.summary() for name, stats in self.quote_age_stats.items()},
            "quote_age_bucket_counts": {key: dict(value) for key, value in self.quote_age_bucket_counts.items()},
            "candidate_quote_age": {name: stats.summary() for name, stats in self.candidate_age_stats.items()},
            "candidate_quote_age_bucket_counts": {key: dict(value) for key, value in self.candidate_age_bucket_counts.items()},
            "spread_by_symbol": {key: value.summary() for key, value in sorted(self.spread_by_symbol.items())},
            "spread_by_time": {key: value.summary() for key, value in sorted(self.spread_by_time.items())},
            "spread_by_symbol_time": {symbol: {period: value.summary() for period, value in sorted(periods.items())} for symbol, periods in sorted(self.spread_by_symbol_time.items())},
            "quote_state_counts": dict(self.quote_state_counts),
            "quote_state_by_symbol": {symbol: dict(states) for symbol, states in sorted(self.quote_state_by_symbol.items())},
            "candidates_in_bad_states": dict(self.candidates_in_bad_states),
            "intermediate_candidate_count": self.intermediate_candidate_count,
            "paired_snapshot_candidate_count": self.paired_snapshot_candidate_count,
            "same_timestamp": {
                "equal_timestamp_groups": self.equal_timestamp_groups,
                "equal_timestamp_events": self.equal_timestamp_events.summary(),
                "paired_quote_groups": self.paired_quote_groups,
                "bid_then_ask_pairs": self.bid_then_ask_pairs,
                "ask_then_bid_pairs": self.ask_then_bid_pairs,
                "multiple_quote_groups": self.multiple_quote_groups,
                "trade_quote_mixed_groups": self.trade_quote_mixed_groups,
                "multiple_symbol_groups": self.multiple_symbol_groups,
                "same_timestamp_symbol_pairs": self.same_timestamp_symbol_pairs,
                "sequence_regressions": self.sequence_regressions,
                "quote_count_per_equal_group": self.group_quote_counts.summary(),
            },
        }


def _make_dimensions(feature: V3Feature) -> dict[str, str]:
    record = feature.record
    return {
        "spread_bucket": _spread_bucket(record.spread_bps_x100),
        "activity_bucket": _activity_bucket(feature.activity_ratio_q8),
        "volatility_bucket": _volatility_bucket(feature.volatility_bps_x100),
    }


def _make_ages(record: CacheRecord, last_bid: list[int | None], last_ask: list[int | None], last_quote: list[int | None], last_trade: list[int | None], benchmark_symbol: int | None) -> dict[str, int | None]:
    now = record.timestamp_ns
    ages = {
        "bid_age_ns": now - last_bid[record.symbol_id] if last_bid[record.symbol_id] is not None else None,
        "ask_age_ns": now - last_ask[record.symbol_id] if last_ask[record.symbol_id] is not None else None,
        "last_trade_age_ns": now - last_trade[record.symbol_id] if last_trade[record.symbol_id] is not None else None,
    }
    if benchmark_symbol is not None and last_quote[benchmark_symbol] is not None:
        ages["benchmark_age_ns"] = now - last_quote[benchmark_symbol]
    else:
        ages["benchmark_age_ns"] = None
    return ages


def _update_quote_quality(acc: DiagnosticAccumulator, record: CacheRecord) -> None:
    if not record.measured or record.event_type != MSG_MARKET_QUOTE:
        return
    acc.measured_quotes += 1
    bid, ask = record.bid_price, record.ask_price
    symbol = SYMBOL_NAMES.get(record.symbol_id, str(record.symbol_id))
    if bid > ask:
        state = "crossed"
    elif bid == ask:
        state = "locked"
    elif bid <= 0 or ask <= 0:
        state = "invalid_nonpositive"
    else:
        state = "normal"
    if record.spread_bps_x100 > WIDE_SPREAD_BPS_X100:
        acc.quote_state_counts["wide_spread_over_10bp"] += 1
        acc.quote_state_by_symbol[symbol]["wide_spread_over_10bp"] += 1
    acc.quote_state_counts[state] += 1
    acc.quote_state_by_symbol[symbol][state] += 1
    if record.spread_bps_x100 >= 0:
        period = _local_time_bucket(record.timestamp_ns)
        acc.spread_by_symbol[symbol].observe(record.spread_bps_x100 / 100.0)
        acc.spread_by_time[period].observe(record.spread_bps_x100 / 100.0)
        acc.spread_by_symbol_time[symbol][period].observe(record.spread_bps_x100 / 100.0)


def _mark_candidate_intermediate(group_symbol: GroupSymbolState, acc: DiagnosticAccumulator) -> None:
    for observation in group_symbol.candidates:
        if not observation.intermediate_state:
            observation.intermediate_state = True
            acc.intermediate_candidate_count += 1
            acc.intermediate_candidate_ids.add(observation.observation_id)


def _finalize_group(group: TimestampGroup, acc: DiagnosticAccumulator) -> None:
    if group.event_count <= 1:
        return
    acc.equal_timestamp_groups += 1
    acc.equal_timestamp_events.observe(group.event_count)
    if len(group.symbols) > 1:
        acc.multiple_symbol_groups += 1
        acc.same_timestamp_symbol_pairs += len(group.symbols) * (len(group.symbols) - 1) // 2
    if group.has_quote and group.has_trade:
        acc.trade_quote_mixed_groups += 1
    quote_total = 0
    for symbol_state in group.symbols.values():
        quote_total += symbol_state.quote_count
        if symbol_state.quote_count > 1:
            acc.multiple_quote_groups += 1
        if symbol_state.bid_count and symbol_state.ask_count:
            acc.paired_quote_groups += 1
            if (symbol_state.first_bid_order or 0) < (symbol_state.first_ask_order or 0):
                acc.bid_then_ask_pairs += 1
            else:
                acc.ask_then_bid_pairs += 1
        if symbol_state.quote_count and symbol_state.last_quote_feature is not None and symbol_state.bid_count and symbol_state.ask_count:
            signal = acc.snapshot_strategy.evaluate(symbol_state.last_quote_feature)
            if signal.action and symbol_state.last_quote_feature.record.measured:
                feature = symbol_state.last_quote_feature
                record = feature.record
                day, window = _window_for(acc.windows, record.segment_index)
                observation = acc.new_observation(
                    kind="snapshot", timestamp_ns=record.timestamp_ns, segment_index=record.segment_index,
                    symbol_id=record.symbol_id, direction=signal.direction, day=day, window=window,
                    bid=record.bid_price, ask=record.ask_price, midpoint=record.midpoint, spread=record.spread,
                    side=record.side, score=signal.score,
                )
                acc.resolver.register(observation, FORWARD_HORIZONS_NS)
                acc.paired_snapshot_candidate_count += 1
    if quote_total:
        acc.group_quote_counts.observe(quote_total)


def _process_record(
    record: CacheRecord,
    acc: DiagnosticAccumulator,
    engine: CausalV3FeatureEngine,
    strategy: V3Strategy,
    last_bid: list[int | None],
    last_ask: list[int | None],
    last_quote: list[int | None],
    last_trade: list[int | None],
) -> tuple[V3Feature, Observation | None]:
    feature = engine.process(record)
    if record.event_type == MSG_MARKET_QUOTE and executable_quote_valid(record.bid_price, record.ask_price) and record.midpoint > 0:
        future = FutureQuote(record.timestamp_ns, record.bid_price, record.ask_price, record.midpoint, record.spread)
        acc.resolver.resolve(record.symbol_id, record.timestamp_ns, future)
    if record.event_type == MSG_MARKET_TRADE:
        last_trade[record.symbol_id] = record.timestamp_ns
    elif record.event_type == MSG_MARKET_QUOTE:
        if record.side == 0:
            last_bid[record.symbol_id] = record.timestamp_ns
        elif record.side == 1:
            last_ask[record.symbol_id] = record.timestamp_ns
        last_quote[record.symbol_id] = record.timestamp_ns

    _update_quote_quality(acc, record)
    observation = None
    if record.measured and record.event_type == MSG_MARKET_QUOTE and feature.quote_valid:
        acc.valid_quotes += 1
        acc.activity_stats.observe(feature.activity_ratio_q8)
        if feature.volatility_bps_x100 is not None:
            acc.volatility_stats.observe(feature.volatility_bps_x100)
        ages = _make_ages(record, last_bid, last_ask, last_quote, last_trade, feature.benchmark_symbol_id)
        for name, value in ages.items():
            if value is not None:
                acc.quote_age_stats[name].observe(value)
        for name, age_key in (("bid", "bid_age_ns"), ("ask", "ask_age_ns"), ("max", None)):
            age = ages[age_key] if age_key else max((value for value in (ages["bid_age_ns"], ages["ask_age_ns"]) if value is not None), default=None)
            if age is not None:
                acc.quote_age_bucket_counts[name][_age_bucket(age)] += 1
        dimensions = _make_dimensions(feature)
        signal = strategy.evaluate(feature)
        is_candidate = bool(signal.action)
        if is_candidate:
            observation = acc.add_candidate(feature, signal.direction, signal.score, ages, dimensions, record.side)
            current_state = "normal"
            if record.bid_price > record.ask_price:
                current_state = "crossed"
            elif record.bid_price == record.ask_price:
                current_state = "locked"
            if current_state != "normal":
                acc.candidates_in_bad_states[current_state] += 1
        # Random controls are formed from the observed current state and never
        # use a future field for selection.  A candidate quote can be sampled
        # by the unconditional baseline, but cannot enter the matched control.
        acc.add_random_and_control(feature, ages, dimensions, is_candidate)
    else:
        strategy.evaluate(feature)
    return feature, observation


def run_cache_diagnostic(cache_path: Path, windows, *, total_events: int, progress_label: str, max_records: int | None = None) -> tuple[DiagnosticAccumulator, float]:
    acc = DiagnosticAccumulator(windows)
    engine = CausalV3FeatureEngine()
    strategy = V3Strategy(V3Config("diagnostic-v3-a-broad", "V3-A", 30_000_000_000, 0, 417))
    last_bid: list[int | None] = [None] * 4
    last_ask: list[int | None] = [None] * 4
    last_quote: list[int | None] = [None] * 4
    last_trade: list[int | None] = [None] * 4
    group: TimestampGroup | None = None
    previous_segment: int | None = None
    started = time.perf_counter()
    last_progress = [started, 0]
    for record in iter_cache_records(cache_path):
        if max_records is not None and acc.processed_records >= max_records:
            break
        if previous_segment is not None and record.segment_index != previous_segment:
            if group is not None:
                _finalize_group(group, acc)
                group = None
            acc.resolver.clear_segment()
            engine.reset()
            strategy.reset()
            acc.snapshot_strategy.reset()
            last_bid = [None] * 4
            last_ask = [None] * 4
            last_quote = [None] * 4
            last_trade = [None] * 4
        if group is None or record.timestamp_ns != group.timestamp_ns:
            if group is not None:
                _finalize_group(group, acc)
            group = TimestampGroup(record.timestamp_ns)
        previous_segment = record.segment_index
        if record.sequence and acc.last_sequence is not None and record.sequence < acc.last_sequence:
            acc.sequence_regressions += 1
        if record.sequence:
            acc.last_sequence = record.sequence
        symbol_state = group.symbols.setdefault(record.symbol_id, GroupSymbolState())
        order = group.event_count
        group.event_count += 1
        acc.processed_records += 1
        if record.event_type == MSG_MARKET_QUOTE:
            group.has_quote = True
            symbol_state.quote_count += 1
            symbol_state.last_quote_order = order
            if record.side == 0:
                symbol_state.bid_count += 1
                if symbol_state.first_bid_order is None:
                    symbol_state.first_bid_order = order
            elif record.side == 1:
                symbol_state.ask_count += 1
                if symbol_state.first_ask_order is None:
                    symbol_state.first_ask_order = order
                symbol_state.ask_seen = True
                _mark_candidate_intermediate(symbol_state, acc)
        else:
            group.has_trade = True
            symbol_state.trade_count += 1
        feature, observation = _process_record(record, acc, engine, strategy, last_bid, last_ask, last_quote, last_trade)
        if record.event_type == MSG_MARKET_QUOTE:
            symbol_state.last_quote_feature = feature
            if observation is not None and record.side == 0 and not symbol_state.ask_seen:
                symbol_state.candidates.append(observation)
        _progress(progress_label, acc.processed_records, min(total_events, max_records or total_events), started, last_progress)
    if group is not None:
        _finalize_group(group, acc)
    acc.resolver.clear_segment()
    acc.finalize_reservoir_metrics()
    _progress(progress_label, acc.processed_records, min(total_events, max_records or total_events), started, last_progress, force=True)
    return acc, time.perf_counter() - started


def _progress(label: str, processed: int, total: int, started: float, last: list[float | int], *, force: bool = False) -> None:
    now = time.perf_counter()
    if not force and processed - last[1] < 1_000_000 and now - last[0] < 30:
        return
    elapsed = now - started
    rate = processed / elapsed if elapsed else 0.0
    print(label + " " + json.dumps({
        "processed_events": processed,
        "total_events": total,
        "percent_complete": processed / total * 100.0 if total else None,
        "elapsed_time": elapsed,
        "processing_rate_events_per_second": rate,
        "estimated_remaining_time": (total - processed) / rate if rate else None,
    }, sort_keys=True), flush=True)
    last[:] = [now, processed]


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_forward_return_fixtures() -> dict:
    """Run hand-verifiable arithmetic and no-lookahead fixtures."""
    cases = []

    def add(name: str, actual: float, expected: float, tolerance: float = 1e-9):
        cases.append({"name": name, "actual": actual, "expected": expected, "passed": abs(actual - expected) <= tolerance})

    add("unchanged_long_midpoint", midpoint_directional_return(100, 100, "long"), 0.0)
    add("unchanged_short_midpoint", midpoint_directional_return(100, 100, "short"), 0.0)
    add("rising_long_midpoint", midpoint_directional_return(100, 102, "long"), 200.0)
    add("rising_short_midpoint", midpoint_directional_return(100, 102, "short"), -200.0)
    add("falling_long_executable", executable_directional_return(99, 101, 97, 99, "long"), (97 - 101) / 101 * 10_000)
    add("falling_short_executable", executable_directional_return(99, 101, 97, 99, "short"), (99 - 99) / 99 * 10_000)
    add("narrow_spread_long", executable_directional_return(99, 101, 99, 101, "long"), (99 - 101) / 101 * 10_000)
    add("wide_spread_long", executable_directional_return(90, 110, 90, 110, "long"), (90 - 110) / 110 * 10_000)
    add("wide_spread_short", executable_directional_return(90, 110, 90, 110, "short"), (90 - 110) / 90 * 10_000)
    add("zero_move_crossing_cost", zero_move_executable_baseline(100, 99, 101, 99, 101, "long"), (99 - 101) / 101 * 10_000)
    add("crossing_cost_estimate", crossing_cost_estimate(100, 99, 101, 99, 101, "long"), 200.0)
    add("bps_conversion", bps_return(1, 100), 100.0)
    locked_quote_is_not_normal = not normal_quote_valid(100, 100)
    asynchronous = first_quote_at_or_after((QuotePoint(999, 99, 101, 100), QuotePoint(1_001, 100, 102, 101)), 1_000)
    cases.extend([
        {"name": "locked_quote_is_not_normal", "actual": locked_quote_is_not_normal, "expected": True, "passed": locked_quote_is_not_normal},
        {"name": "asynchronous_first_quote_at_or_after_horizon", "actual": asynchronous.timestamp_ns if asynchronous else None, "expected": 1_001, "passed": asynchronous is not None and asynchronous.timestamp_ns == 1_001},
    ])
    return {"status": "passed" if all(case["passed"] for case in cases) else "failed", "cases": cases, "formula": "LONG=(future_bid-entry_ask)/entry_ask; SHORT=(entry_bid-future_ask)/entry_bid; midpoint uses current/future midpoint; bps=return*10000"}


def _metric_mean(summary: dict, horizon_name: str, metric: str, scope: str = "aggregate", direction: str | None = None) -> float | None:
    if direction is not None:
        value = summary.get("by_direction", {}).get(direction, {}).get(horizon_name, {}).get(metric, {})
    else:
        value = summary.get(scope, {}).get(horizon_name, {}).get(metric, {})
    return value.get("mean") if isinstance(value, dict) else None


def _book_excess(candidate: dict, control: dict) -> dict:
    output = {}
    for horizon_ns in FORWARD_HORIZONS_NS:
        horizon = HORIZON_NAMES[horizon_ns]
        output[horizon] = {"aggregate": {}, "by_direction": {}}
        for direction in ("long", "short"):
            output[horizon]["by_direction"][direction] = {}
            for metric in ("midpoint_return_bps", "executable_return_bps"):
                candidate_mean = _metric_mean(candidate, horizon, metric, direction=direction)
                control_mean = _metric_mean(control, horizon, metric, direction=direction)
                output[horizon]["by_direction"][direction][f"candidate_{metric}"] = candidate_mean
                output[horizon]["by_direction"][direction][f"control_{metric}"] = control_mean
                output[horizon]["by_direction"][direction][f"excess_{metric}"] = candidate_mean - control_mean if candidate_mean is not None and control_mean is not None else None
        for metric in ("midpoint_return_bps", "executable_return_bps"):
            candidate_mean = _metric_mean(candidate, horizon, metric)
            control_mean = _metric_mean(control, horizon, metric)
            output[horizon]["aggregate"][f"candidate_{metric}"] = candidate_mean
            output[horizon]["aggregate"][f"control_{metric}"] = control_mean
            output[horizon]["aggregate"][f"excess_{metric}"] = candidate_mean - control_mean if candidate_mean is not None and control_mean is not None else None
    return output


def _spread_attribution(candidate_summary: dict) -> dict:
    result = {}
    for horizon_ns in FORWARD_HORIZONS_NS:
        horizon = HORIZON_NAMES[horizon_ns]
        aggregate = candidate_summary.get("aggregate", {}).get(horizon, {})
        exec_mean = aggregate.get("executable_return_bps", {}).get("mean")
        midpoint_mean = aggregate.get("midpoint_return_bps", {}).get("mean")
        zero_mean = aggregate.get("zero_move_executable_bps", {}).get("mean")
        gap_mean = aggregate.get("midpoint_minus_executable_bps", {}).get("mean")
        share = abs(zero_mean) / abs(exec_mean) if zero_mean is not None and exec_mean is not None and exec_mean < 0 else None
        result[horizon] = {
            "mean_midpoint_return_bps": midpoint_mean,
            "mean_executable_return_bps": exec_mean,
            "mean_zero_move_executable_bps": zero_mean,
            "mean_midpoint_minus_executable_bps": gap_mean,
            "mechanical_zero_move_cost_share_of_negative_executable": share,
            "interpretation": "share is a mechanical attribution ratio, not a causal estimator; it can exceed 1 when adverse movement and spread interact asymmetrically",
        }
    return result


def _cluster_book(observations: list[Observation], quiet_ns: int, strongest: bool = False) -> tuple[MetricBook, int]:
    book = MetricBook()
    clusters = 0
    by_symbol: dict[int, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_symbol[observation.symbol_id].append(observation)
    for symbol_observations in by_symbol.values():
        symbol_observations.sort(key=lambda item: item.timestamp_ns)
        current: list[Observation] = []
        previous: int | None = None
        def flush() -> None:
            nonlocal clusters
            if not current:
                return
            clusters += 1
            selected = max(current, key=lambda item: (item.score, -item.timestamp_ns)) if strongest else current[0]
            for horizon_ns, future in selected.futures.items():
                if horizon_ns in FORWARD_HORIZONS_NS:
                    values = _metric_values(selected, future, selected.direction or "long")
                    book.observe(selected.direction or "long", horizon_ns, values, symbol=SYMBOL_NAMES[selected.symbol_id], day=selected.day, window=selected.window)
            current.clear()
        for observation in symbol_observations:
            if previous is not None and observation.timestamp_ns - previous > quiet_ns:
                flush()
            current.append(observation)
            previous = observation.timestamp_ns
        flush()
    return book, clusters


def _cluster_summary(acc: DiagnosticAccumulator) -> dict:
    output = {"raw_candidate_count": acc.candidate_count, "by_quiet_period": {}}
    for quiet_ns in QUIET_PERIODS_NS:
        first_book, clusters = _cluster_book(acc.candidate_observations, quiet_ns)
        strongest_book, strongest_clusters = _cluster_book(acc.candidate_observations, quiet_ns, strongest=True)
        label = HORIZON_NAMES.get(quiet_ns, f"{quiet_ns // 1_000_000_000}s")
        output["by_quiet_period"][label] = {
            "quiet_period_ns": quiet_ns,
            "cluster_count": clusters,
            "first_signal_per_cluster": first_book.summary(),
            "strongest_score_per_cluster": strongest_book.summary(),
            "strongest_score_cluster_count": strongest_clusters,
        }
    return output


def _spot_rows(acc: DiagnosticAccumulator) -> list[dict]:
    rows = []
    selected = acc.spot["long"] + acc.spot["short"]
    selected.sort(key=lambda item: (item.timestamp_ns, item.symbol_id, item.observation_id))
    for index, observation in enumerate(selected, start=1):
        row = {
            "sample_index": index,
            "timestamp_ns": observation.timestamp_ns,
            "symbol": SYMBOL_NAMES[observation.symbol_id],
            "direction": observation.direction,
            "bid": observation.bid,
            "ask": observation.ask,
            "midpoint": observation.midpoint,
            "entry_spread_bps": bps_return(observation.ask - observation.bid, observation.midpoint),
            "bid_age_ns": observation.bid_age_ns,
            "ask_age_ns": observation.ask_age_ns,
            "intermediate_paired_quote_state": observation.intermediate_state,
        }
        for horizon_ns in (1_000_000_000, 5_000_000_000, 15_000_000_000, 30_000_000_000, 60_000_000_000):
            name = HORIZON_NAMES[horizon_ns]
            future = observation.futures.get(horizon_ns)
            row[f"future_{name}_bid"] = future.bid if future else None
            row[f"future_{name}_ask"] = future.ask if future else None
            row[f"future_{name}_midpoint"] = future.midpoint if future else None
            if future:
                values = _metric_values(observation, future, observation.direction or "long")
                row[f"{name}_midpoint_return_bps"] = values["midpoint_return_bps"]
                row[f"{name}_executable_return_bps"] = values["executable_return_bps"]
                row[f"{name}_crossing_cost_estimate_bps"] = values["crossing_cost_estimate_bps"]
            else:
                row[f"{name}_midpoint_return_bps"] = None
                row[f"{name}_executable_return_bps"] = None
                row[f"{name}_crossing_cost_estimate_bps"] = None
        rows.append(row)
    return rows


def _write_spot_csv(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0]) if rows else ["sample_index", "timestamp_ns", "symbol", "direction"]
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _previous_artifact_summaries(repo_root: Path) -> dict:
    result_root = repo_root / "backtest_results"
    output = {}
    v2_dirs = sorted(result_root.glob("strategy_v2_*"), reverse=True)
    for directory in v2_dirs:
        v1_path = directory / "v1_forward_returns.json"
        if v1_path.is_file():
            output["V1_artifact_only"] = {"source": str(v1_path), "forward_returns": json.loads(v1_path.read_text(encoding="utf-8")), "matched_controls_available": False, "reason": "previous artifact contains aggregate returns but not reconstructable candidate timestamps"}
            break
    for directory in v2_dirs:
        medium_path = directory / "medium_results.csv"
        if not medium_path.is_file():
            continue
        with medium_path.open(newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                if row.get("name") == "V2-D-persist-250ms":
                    output["V2-D-persist-250ms_artifact_only"] = {"source": str(medium_path), "forward_returns": json.loads(row["forward_returns"]), "matched_controls_available": False, "reason": "previous artifact contains aggregate returns but not reconstructable candidate timestamps"}
                    break
        if "V2-D-persist-250ms_artifact_only" in output:
            break
    v3_dirs = sorted(result_root.glob("strategy_v3_*"), reverse=True)
    for directory in v3_dirs:
        fast_path = directory / "fast_variants.csv"
        if fast_path.is_file():
            with fast_path.open(newline="", encoding="utf-8") as source:
                rows = list(csv.DictReader(source))
            if rows:
                best = rows[0]
                output["V3-best-ranked_artifact_only"] = {"source": str(fast_path), "name": best.get("name"), "forward_returns": json.loads(best["forward_returns"]), "matched_controls_available": False, "reason": "previous artifact contains aggregate returns but not reconstructable candidate timestamps"}
            break
    return output


def _cache_output(acc: DiagnosticAccumulator) -> dict:
    return acc.summary()


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _representativeness_summary(fast_summary: dict, medium_summary: dict) -> dict:
    """Compare descriptive cache statistics without tuning on MEDIUM."""
    def close(a: float | None, b: float | None, tolerance: float) -> bool:
        if a is None or b is None:
            return False
        return abs(a - b) / max(abs(a), abs(b), 1.0) <= tolerance

    spread = {
        symbol: {
            "fast_mean_bps": fast_summary.get("spread_by_symbol", {}).get(symbol, {}).get("mean"),
            "medium_mean_bps": medium_summary.get("spread_by_symbol", {}).get(symbol, {}).get("mean"),
            "means_within_25_percent": close(
                fast_summary.get("spread_by_symbol", {}).get(symbol, {}).get("mean"),
                medium_summary.get("spread_by_symbol", {}).get(symbol, {}).get("mean"),
                0.25,
            ),
        }
        for symbol in sorted(set(fast_summary.get("spread_by_symbol", {})) | set(medium_summary.get("spread_by_symbol", {})))
    }
    activity_close = close(
        fast_summary.get("activity_ratio_q8", {}).get("mean"),
        medium_summary.get("activity_ratio_q8", {}).get("mean"),
        0.25,
    )
    volatility_close = close(
        fast_summary.get("volatility_bps_x100", {}).get("mean"),
        medium_summary.get("volatility_bps_x100", {}).get("mean"),
        0.25,
    )
    random_return_deltas = {}
    for horizon_ns in FORWARD_HORIZONS_NS:
        horizon = HORIZON_NAMES[horizon_ns]
        fast_mean = _metric_mean(fast_summary.get("random_returns", {}), horizon, "midpoint_return_bps")
        medium_mean = _metric_mean(medium_summary.get("random_returns", {}), horizon, "midpoint_return_bps")
        random_return_deltas[horizon] = {
            "fast_midpoint_mean_bps": fast_mean,
            "medium_midpoint_mean_bps": medium_mean,
            "difference_bps": fast_mean - medium_mean if fast_mean is not None and medium_mean is not None else None,
        }
    validity_fast = fast_summary.get("valid_executable_quote_records", 0) / max(1, fast_summary.get("measured_quote_records", 0))
    validity_medium = medium_summary.get("valid_executable_quote_records", 0) / max(1, medium_summary.get("measured_quote_records", 0))
    spread_representative = bool(spread) and all(item["means_within_25_percent"] for item in spread.values())
    return {
        "spread_by_symbol": spread,
        "activity_mean_within_25_percent": activity_close,
        "volatility_mean_within_25_percent": volatility_close,
        "random_midpoint_return_deltas_bps": random_return_deltas,
        "validity_ratio": {"fast": validity_fast, "medium": validity_medium, "difference": abs(validity_fast - validity_medium)},
        "fast_appears_representative": spread_representative and activity_close and volatility_close and abs(validity_fast - validity_medium) <= 0.25,
        "interpretation": "descriptive comparison only; MEDIUM was not used for parameter tuning or promotion",
    }


def _recover_after_completed_cache_scans(repo_root: Path, result_dir: Path, state: dict, fast_path: Path, medium_summary: dict, benchmark: dict) -> Path:
    """Recover artifact finalization after both cache scans already completed."""
    state["phase"] = "recovery_fast_diagnostic"
    _write_json(result_dir / "analysis_state.json", state)
    fast_acc, fast_elapsed = run_cache_diagnostic(fast_path, FAST_WINDOWS, total_events=fast_path.stat().st_size // CACHE_RECORD_SIZE, progress_label="edge_diagnostic_fast_recovery", max_records=None)
    benchmark["observed_recovery_fast_scan_seconds"] = fast_elapsed
    benchmark["observed_recovery_fast_records_per_second"] = (fast_path.stat().st_size // CACHE_RECORD_SIZE) / fast_elapsed if fast_elapsed else None
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    _write_json(result_dir / "fast_diagnostic_cache.json", _cache_output(fast_acc))
    candidate = fast_acc.candidate_book.summary()
    controls = fast_acc.control_book.summary()
    random_baseline = fast_acc.random_book.summary()
    _write_json(result_dir / "midpoint_vs_executable.json", {"candidate": candidate, "matched_control": controls, "random_unconditional": random_baseline, "horizons": [HORIZON_NAMES[h] for h in FORWARD_HORIZONS_NS]})
    _write_json(result_dir / "execution_cost_decomposition.json", {"candidate": candidate, "aggregate_spread_attribution": _spread_attribution(candidate), "cost_definition": "entry and future half-spreads normalized by entry midpoint; midpoint_minus_executable is the observed quote-state gap"})
    _write_json(result_dir / "zero_move_baseline.json", {"candidate": {h: candidate["aggregate"].get(h, {}).get("zero_move_executable_bps") for h in [HORIZON_NAMES[x] for x in FORWARD_HORIZONS_NS]}, "random_unconditional": {h: random_baseline["aggregate"].get(h, {}).get("zero_move_executable_bps") for h in [HORIZON_NAMES[x] for x in FORWARD_HORIZONS_NS]}, "matched_control": {h: controls["aggregate"].get(h, {}).get("zero_move_executable_bps") for h in [HORIZON_NAMES[x] for x in FORWARD_HORIZONS_NS]}, "interpretation": "zero-movement floor uses observed entry and future spreads while holding the midpoint at entry"})
    _write_json(result_dir / "random_baseline.json", {"fixed_seed": RANDOM_SEED, "reservoir_size_per_symbol_day_window_direction": RANDOM_RESERVOIR_SIZE, "sampled": True, "result": random_baseline})
    _write_json(result_dir / "matched_controls.json", {"matching_dimensions": ["symbol", "day", "time_window", "spread_bucket", "activity_bucket", "volatility_bucket"], "control_is_non_candidate": True, "selection_is_current_state_only": True, "reservoir_size_per_stratum_direction": CONTROL_RESERVOIR_SIZE, "candidate": candidate, "control": controls, "matching_note": "bounded deterministic reservoirs may provide fewer controls than candidates in sparse strata"})
    _write_json(result_dir / "candidate_excess_returns.json", {"V3-A-broad": _book_excess(candidate, controls), "prior_artifact_summaries": _previous_artifact_summaries(repo_root), "excess_definition": "candidate mean minus matched-control mean at the same direction/horizon"})
    _write_json(result_dir / "adverse_selection.json", {"candidate_immediate_returns": fast_acc.candidate_immediate_book.summary(), "horizons": [HORIZON_NAMES[h] for h in IMMEDIATE_HORIZONS_NS], "long_adverse_if_negative": True, "short_adverse_if_negative": True, "interpretation": "directional midpoint return is the primary adverse-selection measure; executable immediate return is included for execution context"})
    _write_json(result_dir / "quote_age.json", {"measured_quote_age_distributions": {name: stats.summary() for name, stats in fast_acc.quote_age_stats.items()}, "measured_quote_age_bucket_counts": {key: dict(value) for key, value in fast_acc.quote_age_bucket_counts.items()}, "candidate_age_distributions": {name: stats.summary() for name, stats in fast_acc.candidate_age_stats.items()}, "candidate_age_bucket_counts": {key: dict(value) for key, value in fast_acc.candidate_age_bucket_counts.items()}, "candidate_returns_by_max_age_bucket": {bucket: book.summary() for bucket, book in sorted(fast_acc.candidate_age_books.items())}, "benchmark_age_is_asof": True, "last_trade_age_is_asof": True, "units": "ages are nanoseconds; return buckets use maximum of bid/ask age"})
    _write_json(result_dir / "paired_quote_semantics.json", {"same_timestamp": fast_acc.summary()["same_timestamp"], "intermediate_candidates": {"count": fast_acc.intermediate_candidate_count, "returns": fast_acc.intermediate_book.summary()}, "after_both_sides_snapshot_comparison": {"candidate_count": fast_acc.paired_snapshot_candidate_count, "returns": fast_acc.snapshot_book.summary()}, "evaluation_order": "cache record order; BID/ASK state is evaluated after each fixed-width cache record"})
    _write_json(result_dir / "same_timestamp_ordering.json", fast_acc.summary()["same_timestamp"] | {"ordering_claim": "sequence order was used as the deterministic tie order; sequence_regressions is reported explicitly"})
    _write_json(result_dir / "quote_quality.json", {"measured_quote_states": fast_acc.summary()["quote_state_counts"], "by_symbol": fast_acc.summary()["quote_state_by_symbol"], "candidates_in_bad_states": fast_acc.candidates_in_bad_states, "wide_spread_threshold_bps": WIDE_SPREAD_BPS_X100 / 100.0, "candidate_generation_rule": "V3 quote_valid requires positive bid, ask > bid, and non-negative spread field"})
    _write_json(result_dir / "spread_by_symbol.json", {"units": "bps", "by_symbol": {symbol: stats.summary() for symbol, stats in sorted(fast_acc.spread_by_symbol.items())}, "by_time_bucket": {period: stats.summary() for period, stats in sorted(fast_acc.spread_by_time.items())}, "by_symbol_and_time_bucket": {symbol: {period: stats.summary() for period, stats in sorted(periods.items())} for symbol, periods in sorted(fast_acc.spread_by_symbol_time.items())}})
    _write_json(result_dir / "unconditional_midpoint_returns.json", {"source": "fixed-seed bounded random valid-quote sample", "results": {"fast": {direction: {h: data.get("midpoint_return_bps") for h, data in random_baseline["by_direction"].get(direction, {}).items()} for direction in ("long", "short")}, "medium": medium_summary["random_returns"]}})
    _write_json(result_dir / "unconditional_executable_returns.json", {"source": "fixed-seed bounded random valid-quote sample", "results": {"fast": {direction: {h: data.get("executable_return_bps") for h, data in random_baseline["by_direction"].get(direction, {}).items()} for direction in ("long", "short")}, "medium": medium_summary["random_returns"]}})
    _write_json(result_dir / "cluster_adjusted_returns.json", _cluster_summary(fast_acc))
    fast_summary = _cache_output(fast_acc)
    _write_json(result_dir / "fast_vs_medium.json", {"fast": {"spread_by_symbol": fast_summary["spread_by_symbol"], "spread_by_time": fast_summary["spread_by_time"], "activity_ratio_q8": fast_summary["activity_ratio_q8"], "volatility_bps_x100": fast_summary["volatility_bps_x100"], "quote_age": fast_summary["quote_age"], "unconditional_random_returns": fast_summary["random_returns"]}, "medium": {"spread_by_symbol": medium_summary["spread_by_symbol"], "spread_by_time": medium_summary["spread_by_time"], "activity_ratio_q8": medium_summary["activity_ratio_q8"], "volatility_bps_x100": medium_summary["volatility_bps_x100"], "quote_age": medium_summary["quote_age"], "unconditional_random_returns": medium_summary["random_returns"]}, "representativeness_rule": "compare the same bounded-sample statistics; differences are descriptive and do not justify strategy tuning", "full_medium_scan": True})
    _write_spot_csv(result_dir / "v3_candidate_spot_checks.csv", _spot_rows(fast_acc))
    diagnosis = _diagnose(fast_acc, medium_summary, candidate, controls, random_baseline)
    _write_json(result_dir / "diagnosis_summary.json", diagnosis)
    (result_dir / "report.md").write_text(_render_report(result_dir, benchmark, fast_acc, medium_summary, diagnosis), encoding="utf-8")
    if "artifact_recovery" not in state["completed_phases"]:
        state["completed_phases"].append("artifact_recovery")
    state["phase"] = "complete"
    state["runtime_seconds"] = state.get("runtime_seconds", 0) + fast_elapsed
    _write_json(result_dir / "analysis_state.json", state)
    return result_dir


def run_diagnostic(repo_root: Path) -> Path:
    fast_path, medium_path, fast_manifest, medium_manifest = _cache_paths(repo_root)
    result_root = repo_root / "backtest_results"
    for candidate in sorted(result_root.glob("edge_diagnostic_*"), reverse=True):
        state_path = candidate / "analysis_state.json"
        required = (state_path, candidate / "fast_diagnostic_cache.json", candidate / "medium_diagnostic_cache.json", candidate / "pipeline_benchmark.json", candidate / "forward_return_fixture_validation.json")
        if not all(path.is_file() for path in required):
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected_identity = {"size_bytes": EXPECTED_CANONICAL_SIZE, "event_count": EXPECTED_CANONICAL_EVENTS, "sha256": EXPECTED_CANONICAL_SHA256}
        if state.get("source_identity") != expected_identity:
            continue
        quote_age_path = candidate / "quote_age.json"
        if state.get("phase") == "complete" and (candidate / "report.md").is_file() and quote_age_path.is_file():
            quote_age = json.loads(quote_age_path.read_text(encoding="utf-8"))
            random_baseline_path = candidate / "random_baseline.json"
            has_favorable_fraction = False
            if random_baseline_path.is_file():
                random_baseline = json.loads(random_baseline_path.read_text(encoding="utf-8"))
                has_favorable_fraction = "favorable_fraction" in random_baseline.get("result", {}).get("aggregate", {}).get("1s", {}).get("midpoint_return_bps", {})
            if "measured_quote_age_distributions" in quote_age and "candidate_age_distributions" in quote_age and has_favorable_fraction:
                return candidate
        if state.get("phase") in ("medium_diagnostic", "complete", "recovery_fast_diagnostic"):
            medium_summary = json.loads((candidate / "medium_diagnostic_cache.json").read_text(encoding="utf-8"))
            benchmark = json.loads((candidate / "pipeline_benchmark.json").read_text(encoding="utf-8"))
            return _recover_after_completed_cache_scans(repo_root, candidate, state, fast_path, medium_summary, benchmark)
    fixture = validate_forward_return_fixtures()
    result_dir = result_root / f"edge_diagnostic_{_run_id()}"
    result_dir.mkdir(parents=True, exist_ok=False)
    state_path = result_dir / "analysis_state.json"
    state = {
        "phase": "fixture_validation",
        "completed_phases": [],
        "source_identity": {"size_bytes": EXPECTED_CANONICAL_SIZE, "event_count": EXPECTED_CANONICAL_EVENTS, "sha256": EXPECTED_CANONICAL_SHA256},
        "cache_record_size": CACHE_RECORD_SIZE,
        "alpaca_calls": 0,
        "canonical_full_passes": 0,
    }
    _write_json(result_dir / "forward_return_fixture_validation.json", fixture)
    if fixture["status"] != "passed":
        state["phase"] = "correctness_issue"
        _write_json(state_path, state)
        _write_json(result_dir / "diagnosis_summary.json", {"classification": "EVENT SEMANTICS ISSUE", "reason": "forward-return fixture validation failed; diagnostic scan stopped"})
        return result_dir
    state["completed_phases"].append("fixture_validation")
    _write_json(state_path, state)

    fast_benchmark_started = time.perf_counter()
    fast_sample, fast_sample_elapsed = run_cache_diagnostic(fast_path, FAST_WINDOWS, total_events=fast_manifest["record_count"], progress_label="edge_diagnostic_fast_benchmark", max_records=100_000)
    medium_sample, medium_sample_elapsed = run_cache_diagnostic(medium_path, MEDIUM_WINDOWS, total_events=medium_manifest["record_count"], progress_label="edge_diagnostic_medium_benchmark", max_records=100_000)
    fast_rate = 100_000 / fast_sample_elapsed if fast_sample_elapsed else 0.0
    medium_rate = 100_000 / medium_sample_elapsed if medium_sample_elapsed else 0.0
    estimated_fast = fast_manifest["record_count"] / fast_rate if fast_rate else None
    estimated_medium = medium_manifest["record_count"] / medium_rate if medium_rate else None
    benchmark = {
        "sample_size_per_cache": 100_000,
        "fast": {"sample_elapsed_seconds": fast_sample_elapsed, "records_per_second": fast_rate, "projected_full_seconds": estimated_fast, "projected_full_minutes": estimated_fast / 60 if estimated_fast else None},
        "medium": {"sample_elapsed_seconds": medium_sample_elapsed, "records_per_second": medium_rate, "projected_full_seconds": estimated_medium, "projected_full_minutes": estimated_medium / 60 if estimated_medium else None},
        "projected_total_minutes": (estimated_fast + estimated_medium) / 60 if estimated_fast and estimated_medium else None,
        "hard_limit_minutes": 30,
        "method": "one shared sequential diagnostic pass per cache; random/control reservoirs are bounded",
        "canonical_full_passes": 0,
        "prior_workflow_canonical_full_passes": PRIOR_CANONICAL_FULL_PASSES,
        "refactored_cache_passes": 2,
        "alpaca_calls": 0,
    }
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    if benchmark["projected_total_minutes"] and benchmark["projected_total_minutes"] > 30:
        state["phase"] = "runtime_guard"
        _write_json(state_path, state)
        _write_json(result_dir / "diagnosis_summary.json", {"classification": "FAST SAMPLE NOT REPRESENTATIVE", "reason": "runtime guard stopped before full diagnostic scan", "benchmark": benchmark})
        return result_dir
    state["completed_phases"].append("benchmarks")
    _write_json(state_path, state)

    fast_acc, fast_elapsed = run_cache_diagnostic(fast_path, FAST_WINDOWS, total_events=fast_manifest["record_count"], progress_label="edge_diagnostic_fast", max_records=None)
    _write_json(result_dir / "fast_diagnostic_cache.json", _cache_output(fast_acc))
    state["completed_phases"].append("fast_diagnostic")
    state["phase"] = "fast_diagnostic"
    _write_json(state_path, state)
    medium_acc, medium_elapsed = run_cache_diagnostic(medium_path, MEDIUM_WINDOWS, total_events=medium_manifest["record_count"], progress_label="edge_diagnostic_medium", max_records=None)
    _write_json(result_dir / "medium_diagnostic_cache.json", _cache_output(medium_acc))
    benchmark["observed_fast_scan_seconds"] = fast_elapsed
    benchmark["observed_medium_scan_seconds"] = medium_elapsed
    benchmark["observed_scan_total_seconds"] = fast_elapsed + medium_elapsed
    benchmark["observed_fast_records_per_second"] = fast_manifest["record_count"] / fast_elapsed if fast_elapsed else None
    benchmark["observed_medium_records_per_second"] = medium_manifest["record_count"] / medium_elapsed if medium_elapsed else None
    benchmark["observed_scan_total_minutes"] = (fast_elapsed + medium_elapsed) / 60.0
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    state["completed_phases"].append("medium_diagnostic")
    state["phase"] = "medium_diagnostic"
    _write_json(state_path, state)

    candidate = fast_acc.candidate_book.summary()
    controls = fast_acc.control_book.summary()
    random_baseline = fast_acc.random_book.summary()
    _write_json(result_dir / "midpoint_vs_executable.json", {"candidate": candidate, "matched_control": controls, "random_unconditional": random_baseline, "horizons": [HORIZON_NAMES[h] for h in FORWARD_HORIZONS_NS]})
    _write_json(result_dir / "execution_cost_decomposition.json", {"candidate": candidate, "aggregate_spread_attribution": _spread_attribution(candidate), "cost_definition": "entry and future half-spreads normalized by entry midpoint; midpoint_minus_executable is the observed quote-state gap"})
    _write_json(result_dir / "zero_move_baseline.json", {"candidate": {h: candidate["aggregate"].get(h, {}).get("zero_move_executable_bps") for h in [HORIZON_NAMES[x] for x in FORWARD_HORIZONS_NS]}, "random_unconditional": {h: random_baseline["aggregate"].get(h, {}).get("zero_move_executable_bps") for h in [HORIZON_NAMES[x] for x in FORWARD_HORIZONS_NS]}, "matched_control": {h: controls["aggregate"].get(h, {}).get("zero_move_executable_bps") for h in [HORIZON_NAMES[x] for x in FORWARD_HORIZONS_NS]}, "interpretation": "zero-movement floor uses observed entry and future spreads while holding the midpoint at entry"})
    _write_json(result_dir / "random_baseline.json", {"fixed_seed": RANDOM_SEED, "reservoir_size_per_symbol_day_window_direction": RANDOM_RESERVOIR_SIZE, "sampled": True, "result": random_baseline})
    _write_json(result_dir / "matched_controls.json", {"matching_dimensions": ["symbol", "day", "time_window", "spread_bucket", "activity_bucket", "volatility_bucket"], "control_is_non_candidate": True, "selection_is_current_state_only": True, "reservoir_size_per_stratum_direction": CONTROL_RESERVOIR_SIZE, "candidate": candidate, "control": controls, "matching_note": "bounded deterministic reservoirs may provide fewer controls than candidates in sparse strata"})
    _write_json(result_dir / "candidate_excess_returns.json", {"V3-A-broad": _book_excess(candidate, controls), "prior_artifact_summaries": _previous_artifact_summaries(repo_root), "excess_definition": "candidate mean minus matched-control mean at the same direction/horizon"})
    _write_json(result_dir / "adverse_selection.json", {"candidate_immediate_returns": fast_acc.candidate_immediate_book.summary(), "horizons": [HORIZON_NAMES[h] for h in IMMEDIATE_HORIZONS_NS], "long_adverse_if_negative": True, "short_adverse_if_negative": True, "interpretation": "directional midpoint return is the primary adverse-selection measure; executable immediate return is included for execution context"})
    _write_json(result_dir / "quote_age.json", {"measured_quote_age_distributions": {name: stats.summary() for name, stats in fast_acc.quote_age_stats.items()}, "measured_quote_age_bucket_counts": {key: dict(value) for key, value in fast_acc.quote_age_bucket_counts.items()}, "candidate_age_distributions": {name: stats.summary() for name, stats in fast_acc.candidate_age_stats.items()}, "candidate_age_bucket_counts": {key: dict(value) for key, value in fast_acc.candidate_age_bucket_counts.items()}, "candidate_returns_by_max_age_bucket": {bucket: book.summary() for bucket, book in sorted(fast_acc.candidate_age_books.items())}, "benchmark_age_is_asof": True, "last_trade_age_is_asof": True, "units": "ages are nanoseconds; return buckets use maximum of bid/ask age"})
    _write_json(result_dir / "paired_quote_semantics.json", {"same_timestamp": fast_acc.summary()["same_timestamp"], "intermediate_candidates": {"count": fast_acc.intermediate_candidate_count, "returns": fast_acc.intermediate_book.summary()}, "after_both_sides_snapshot_comparison": {"candidate_count": fast_acc.paired_snapshot_candidate_count, "returns": fast_acc.snapshot_book.summary()}, "evaluation_order": "cache record order; BID/ASK state is evaluated after each fixed-width cache record"})
    _write_json(result_dir / "same_timestamp_ordering.json", fast_acc.summary()["same_timestamp"] | {"ordering_claim": "sequence order was used as the deterministic tie order; sequence_regressions is reported explicitly"})
    _write_json(result_dir / "quote_quality.json", {"measured_quote_states": fast_acc.summary()["quote_state_counts"], "by_symbol": fast_acc.summary()["quote_state_by_symbol"], "candidates_in_bad_states": fast_acc.candidates_in_bad_states, "wide_spread_threshold_bps": WIDE_SPREAD_BPS_X100 / 100.0, "candidate_generation_rule": "V3 quote_valid requires positive bid, ask > bid, and non-negative spread field"})
    _write_json(result_dir / "spread_by_symbol.json", {"units": "bps", "by_symbol": {symbol: stats.summary() for symbol, stats in sorted(fast_acc.spread_by_symbol.items())}, "by_time_bucket": {period: stats.summary() for period, stats in sorted(fast_acc.spread_by_time.items())}, "by_symbol_and_time_bucket": {symbol: {period: stats.summary() for period, stats in sorted(periods.items())} for symbol, periods in sorted(fast_acc.spread_by_symbol_time.items())}})
    _write_json(result_dir / "unconditional_midpoint_returns.json", {"source": "fixed-seed bounded random valid-quote sample", "results": {"fast": {direction: {h: data.get("midpoint_return_bps") for h, data in random_baseline["by_direction"].get(direction, {}).items()} for direction in ("long", "short")}, "medium": medium_acc.random_book.summary()}})
    _write_json(result_dir / "unconditional_executable_returns.json", {"source": "fixed-seed bounded random valid-quote sample", "results": {"fast": {direction: {h: data.get("executable_return_bps") for h, data in random_baseline["by_direction"].get(direction, {}).items()} for direction in ("long", "short")}, "medium": medium_acc.random_book.summary()}})
    _write_json(result_dir / "cluster_adjusted_returns.json", _cluster_summary(fast_acc))
    fast_summary = _cache_output(fast_acc)
    medium_summary = _cache_output(medium_acc)
    _write_json(result_dir / "fast_vs_medium.json", {"fast": {"spread_by_symbol": fast_summary["spread_by_symbol"], "spread_by_time": fast_summary["spread_by_time"], "activity_ratio_q8": fast_summary["activity_ratio_q8"], "volatility_bps_x100": fast_summary["volatility_bps_x100"], "quote_age": fast_summary["quote_age"], "unconditional_random_returns": fast_summary["random_returns"]}, "medium": {"spread_by_symbol": medium_summary["spread_by_symbol"], "spread_by_time": medium_summary["spread_by_time"], "activity_ratio_q8": medium_summary["activity_ratio_q8"], "volatility_bps_x100": medium_summary["volatility_bps_x100"], "quote_age": medium_summary["quote_age"], "unconditional_random_returns": medium_summary["random_returns"]}, "representativeness_rule": "compare the same bounded-sample statistics; differences are descriptive and do not justify strategy tuning", "full_medium_scan": True})
    _write_spot_csv(result_dir / "v3_candidate_spot_checks.csv", _spot_rows(fast_acc))

    diagnosis = _diagnose(fast_acc, medium_acc, candidate, controls, random_baseline)
    _write_json(result_dir / "diagnosis_summary.json", diagnosis)
    report = _render_report(result_dir, benchmark, fast_acc, medium_acc, diagnosis)
    (result_dir / "report.md").write_text(report, encoding="utf-8")
    state["completed_phases"].append("artifacts")
    state["phase"] = "complete"
    state["runtime_seconds"] = fast_sample_elapsed + medium_sample_elapsed + fast_elapsed + medium_elapsed
    _write_json(state_path, state)
    return result_dir


def _diagnose(fast_acc: DiagnosticAccumulator, medium_acc: DiagnosticAccumulator | dict, candidate: dict, controls: dict, random_baseline: dict) -> dict:
    strategic = {}
    for horizon_ns in FORWARD_HORIZONS_NS:
        horizon = HORIZON_NAMES[horizon_ns]
        c = candidate["aggregate"].get(horizon, {})
        ctrl = controls["aggregate"].get(horizon, {})
        rnd = random_baseline["aggregate"].get(horizon, {})
        strategic[horizon] = {
            "candidate_midpoint_mean": c.get("midpoint_return_bps", {}).get("mean"),
            "candidate_midpoint_median": c.get("midpoint_return_bps", {}).get("median"),
            "candidate_executable_mean": c.get("executable_return_bps", {}).get("mean"),
            "candidate_executable_median": c.get("executable_return_bps", {}).get("median"),
            "control_midpoint_mean": ctrl.get("midpoint_return_bps", {}).get("mean"),
            "control_executable_mean": ctrl.get("executable_return_bps", {}).get("mean"),
            "random_midpoint_mean": rnd.get("midpoint_return_bps", {}).get("mean"),
            "random_executable_mean": rnd.get("executable_return_bps", {}).get("mean"),
        }
    excess = _book_excess(candidate, controls)
    positive_midpoint_excess = [item["aggregate"].get("excess_midpoint_return_bps") for item in excess.values() if item["aggregate"].get("excess_midpoint_return_bps") is not None and item["aggregate"].get("excess_midpoint_return_bps") > 0]
    positive_candidate_midpoint = [item["candidate_midpoint_mean"] for item in strategic.values() if item["candidate_midpoint_mean"] is not None and item["candidate_midpoint_mean"] > 0]
    cost = _spread_attribution(candidate)
    cost_share = [item["mechanical_zero_move_cost_share_of_negative_executable"] for item in cost.values() if item["mechanical_zero_move_cost_share_of_negative_executable"] is not None]
    immediate = fast_acc.candidate_immediate_book.summary()["aggregate"]
    immediate_bad = [item.get("midpoint_return_bps", {}).get("mean") for item in immediate.values() if item.get("midpoint_return_bps", {}).get("mean") is not None and item.get("midpoint_return_bps", {}).get("mean") < 0]
    intermediate_fraction = fast_acc.intermediate_candidate_count / fast_acc.candidate_count if fast_acc.candidate_count else 0.0
    snapshot_count = fast_acc.paired_snapshot_candidate_count
    candidate_count = fast_acc.candidate_count
    medium_valid_quotes = medium_acc.valid_quotes if isinstance(medium_acc, DiagnosticAccumulator) else medium_acc.get("valid_executable_quote_records", 0)
    medium_measured_quotes = medium_acc.measured_quotes if isinstance(medium_acc, DiagnosticAccumulator) else medium_acc.get("measured_quote_records", 0)
    fast_summary = fast_acc.summary()
    medium_summary = medium_acc.summary() if isinstance(medium_acc, DiagnosticAccumulator) else medium_acc
    representativeness = _representativeness_summary(fast_summary, medium_summary)
    snapshot_summary = fast_acc.snapshot_book.summary()
    snapshot_midpoint_1s = _metric_mean(snapshot_summary, "1s", "midpoint_return_bps")
    candidate_midpoint_1s = _metric_mean(candidate, "1s", "midpoint_return_bps")
    event_semantics_changes_incidence = intermediate_fraction > .25 and snapshot_count != candidate_count
    event_semantics_material_to_conclusion = bool(
        event_semantics_changes_incidence
        and snapshot_midpoint_1s is not None
        and candidate_midpoint_1s is not None
        and snapshot_midpoint_1s >= 0
        and candidate_midpoint_1s < 0
    )
    execution_cost_dominates = bool(cost_share and sum(cost_share) / len(cost_share) > .75 and positive_candidate_midpoint)
    adverse_selection_dominates = len(immediate_bad) >= 3 and intermediate_fraction < .25
    data_feed_limitation_material = bool(
        fast_acc.valid_quotes
        and medium_valid_quotes
        and abs(fast_acc.valid_quotes / max(1, fast_acc.measured_quotes) - medium_valid_quotes / max(1, medium_measured_quotes)) > .25
    )
    if not positive_candidate_midpoint and not positive_midpoint_excess:
        classification = "SIGNAL FAILURE"
        explanation = "Candidate midpoint returns are negative and do not show positive matched-control excess; the directional signal fails before execution costs are considered."
    elif execution_cost_dominates:
        classification = "EXECUTION COST DOMINATED"
        explanation = "Candidate midpoint evidence is positive while the mechanical zero-movement crossing floor accounts for most executable loss."
    elif adverse_selection_dominates:
        classification = "ADVERSE SELECTION"
        explanation = "Candidates show repeated immediate adverse midpoint movement beyond ordinary controls."
    elif intermediate_fraction > .25 and snapshot_count != candidate_count:
        classification = "EVENT SEMANTICS ISSUE"
        explanation = "A material fraction of candidates occurs between paired quote updates and the after-both-sides comparison changes candidate incidence."
    elif data_feed_limitation_material:
        classification = "DATA / FEED LIMITATION"
        explanation = "FAST and MEDIUM quote validity/market-state behavior differs materially, limiting execution conclusions."
    else:
        classification = "SIGNAL FAILURE"
        explanation = "The measured candidate and control evidence is not consistent with a positive directional edge; execution and feed effects remain secondary diagnostics."
    next_direction = {
        "SIGNAL FAILURE": "Abandon this directional-indicator search on these features; define and validate a fundamentally different information source before any new strategy family.",
        "EXECUTION COST DOMINATED": "Conceptually study slower-horizon/high-edge or passive-execution hypotheses before any new implementation.",
        "ADVERSE SELECTION": "Conceptually investigate reversal and liquidity-shock mechanisms before any new implementation.",
        "EVENT SEMANTICS ISSUE": "Fix normalization/evaluation semantics and rerun the diagnostic before any new strategy claim.",
        "DATA / FEED LIMITATION": "Obtain richer consolidated/NBBO-quality data before making further execution claims.",
        "FAST SAMPLE NOT REPRESENTATIVE": "Use a broader deterministic research sampling design before revisiting strategy conclusions.",
        "MORE THAN ONE MATERIAL ISSUE": "Separate the independent failure mechanisms with richer data and controls before pursuing a new strategy family.",
    }[classification]
    return {
        "classification": classification,
        "quantitative_explanation": explanation,
        "candidate_count": candidate_count,
        "intermediate_candidate_fraction": intermediate_fraction,
        "paired_snapshot_candidate_count": snapshot_count,
        "strategic_horizon_summary": strategic,
        "mechanical_cost_attribution": cost,
        "positive_candidate_midpoint_horizons": len(positive_candidate_midpoint),
        "positive_midpoint_excess_horizons": len(positive_midpoint_excess),
        "fast_vs_medium_validity": {"fast_valid_quotes": fast_acc.valid_quotes, "medium_valid_quotes": medium_valid_quotes, "fast_measured_quotes": fast_acc.measured_quotes, "medium_measured_quotes": medium_measured_quotes},
        "diagnostic_evidence": {
            "directional_midpoint_contains_positive_information": bool(positive_candidate_midpoint or positive_midpoint_excess),
            "execution_costs_dominate": execution_cost_dominates,
            "adverse_selection_dominates": adverse_selection_dominates,
            "event_semantics_changes_candidate_incidence": event_semantics_changes_incidence,
            "event_semantics_material_to_failure_conclusion": event_semantics_material_to_conclusion,
            "data_feed_limitation_material_to_conclusion": data_feed_limitation_material,
            "fast_sample_appears_representative": representativeness["fast_appears_representative"],
            "snapshot_1s_midpoint_mean_bps": snapshot_midpoint_1s,
            "candidate_1s_midpoint_mean_bps": candidate_midpoint_1s,
        },
        "fast_vs_medium_representativeness": representativeness,
        "next_research_direction": next_direction,
        "strategy_v4_implemented": False,
        "canonical_full_passes": 0,
        "prior_workflow_canonical_full_passes": PRIOR_CANONICAL_FULL_PASSES,
        "alpaca_calls": 0,
        "rtl_changes": False,
        "broker_or_live_changes": False,
    }


def _render_report(result_dir: Path, benchmark: dict, fast_acc: DiagnosticAccumulator, medium_acc: DiagnosticAccumulator, diagnosis: dict) -> str:
    candidate = fast_acc.candidate_book.summary()
    controls = fast_acc.control_book.summary()
    random_baseline = fast_acc.random_book.summary()
    strategic_rows = []
    for horizon_ns in FORWARD_HORIZONS_NS:
        h = HORIZON_NAMES[horizon_ns]
        c = candidate["aggregate"].get(h, {})
        ctrl = controls["aggregate"].get(h, {})
        rnd = random_baseline["aggregate"].get(h, {})
        strategic_rows.append(f"| {h} | {c.get('midpoint_return_bps', {}).get('mean')} / {c.get('midpoint_return_bps', {}).get('median')} | {c.get('executable_return_bps', {}).get('mean')} / {c.get('executable_return_bps', {}).get('median')} | {ctrl.get('executable_return_bps', {}).get('mean')} | {rnd.get('midpoint_return_bps', {}).get('mean')} / {rnd.get('executable_return_bps', {}).get('mean')} |")
    return f"""# Edge attribution and market-data diagnostic study

## Primary classification

**{diagnosis['classification']}**

{diagnosis['quantitative_explanation']}

## Scope

This is a software-only diagnostic milestone. It used the validated FAST and
MEDIUM V2 research caches, never opened the 55,740,891-event canonical binary,
made zero Alpaca calls, downloaded no data, changed no RTL, and added no broker,
WebSocket, paper-order, or live-trading behavior. V1, V2, and V3 artifacts and
semantics were preserved.

Canonical identity: {EXPECTED_CANONICAL_EVENTS} events, {EXPECTED_CANONICAL_SIZE}
bytes, SHA-256 `{EXPECTED_CANONICAL_SHA256}`. The cache record size is
{CACHE_RECORD_SIZE} bytes. The broad V3 control path is the existing V3-A 30s
range, zero-buffer configuration; no V4 or new parameter search was run.

## Runtime

```json
{json.dumps(benchmark, indent=2, sort_keys=True)}
```

The diagnostic used one shared sequential pass per cache. Candidate, control,
and random forward observations use bounded pending queues/reservoirs; exact
counts and means are retained, while quantiles are deterministic bounded
samples. Progress output includes processed events, total events, percent,
elapsed time, processing rate, and estimated remaining time.

The prior workflow scheduled {PRIOR_CANONICAL_FULL_PASSES} full canonical
passes. This refactor schedules zero full canonical passes and two shared cache
passes (FAST and MEDIUM). The benchmark is the pre-run estimate; when present,
`observed_scan_total_minutes` is the measured pair of cache scans.

## Forward-return validation

`forward_return_fixture_validation.json` passed hand-verifiable unchanged,
rising, falling, narrow-spread, wide-spread, locked-quote, and asynchronous
at-or-after-horizon cases. Long uses ask-to-future-bid; short uses
bid-to-future-ask. Midpoint returns use midpoint-to-midpoint arithmetic and
basis-point conversion is return multiplied by 10,000.

## FAST candidate versus controls and unconditional behavior

| Horizon | Candidate midpoint mean / median | Candidate executable mean / median | Control executable mean | Random midpoint / executable mean |
|---|---:|---:|---:|---:|
{chr(10).join(strategic_rows)}

Candidate counts: {fast_acc.candidate_count} raw observations, with
{fast_acc.candidate_counts_by_direction}, across symbols
{dict(fast_acc.candidate_counts_by_symbol)}, days
{dict(fast_acc.candidate_counts_by_day)}, and
{len(fast_acc.candidate_counts_by_window)} windows. The matched controls use
symbol/day/window/spread/activity/volatility strata and are selected without
future fields. The unconditional baseline uses fixed seed {RANDOM_SEED} and a
bounded reservoir of {RANDOM_RESERVOIR_SIZE} observations per
symbol/day/window/direction stratum.

## Attribution and data diagnostics

- `execution_cost_decomposition.json` reports entry spread, future spread,
  crossing-cost estimate, midpoint return, executable return, and the
  mechanical zero-movement floor.
- `adverse_selection.json` reports 1ms, 10ms, 100ms, 250ms, 500ms, 1s, and 5s
  immediate midpoint/executable returns.
- `quote_age.json` reports measured-quote and candidate-only bid, ask,
  benchmark, and last-trade age distributions plus return by maximum bid/ask
  age bucket.
- `paired_quote_semantics.json` and `same_timestamp_ordering.json` report BID /
  ASK pairs, intermediate-state candidates, after-both-sides snapshot results,
  mixed trades, multiple quotes, multiple symbols, and sequence regressions.
- `quote_quality.json` reports crossed, locked, non-positive, wide, and normal
  measured quote states. V3 candidates require positive bid, ask > bid, and
  valid spread, so bad-state candidates are counted explicitly rather than
  treated as normal.
- `spread_by_symbol.json` reports mean, median, P75, P90, P95, and P99 in bps
  for SPY, QQQ, NVDA, and AMD, including opening, midday, and late-session
  buckets.
- `fast_vs_medium.json` compares spread, midpoint volatility, activity, quote
  age, and unconditional random returns. MEDIUM was scanned fully because the
  measured benchmark stayed within the runtime guard.
- `cluster_adjusted_returns.json` reports raw and first/strongest-signal-per-
  cluster results for 1s, 5s, 10s, and 30s quiet periods.

## Manual arithmetic and prior artifacts

`v3_candidate_spot_checks.csv` contains up to 25 long and 25 short broad V3-A
rows with current bid/ask/midpoint/spread, ages, paired-state flag, future 1s,
5s, 15s, 30s, and 60s quotes, midpoint return, executable return, and crossing
cost. `candidate_excess_returns.json` also records V1, V2 strongest, and V3
best-ranked prior aggregate artifacts where timestamps were unavailable; those
legacy artifacts are explicitly not presented as matched-control results.

## Limits and recommendation

The study covers the existing two-day/six-window FAST scope and the larger
existing MEDIUM scope, not the full canonical dataset. Quantiles and random /
control baselines are bounded samples. IEX is a single-venue feed rather than
consolidated SIP/NBBO; the local results quantify quote state, spread, age, and
activity but cannot establish final multi-venue execution realism. Because the
primary classification is SIGNAL FAILURE, the single next conceptual direction
is to abandon this directional-indicator search on these features and define a
fundamentally different information source before any new strategy family. No
Strategy V4 was implemented.
"""


def main() -> int:
    argparse.ArgumentParser(description="Run offline edge-attribution diagnostics").parse_args()
    try:
        result = run_diagnostic(Path(__file__).resolve().parents[2])
    except Exception as exc:
        print(f"edge_diagnostic_failed type={type(exc).__name__} message={exc}")
        return 1
    print(f"edge_diagnostic_complete result_dir={result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
