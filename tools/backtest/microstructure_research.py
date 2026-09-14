"""Causal microstructure information-source discovery on validated V2 caches.

This module is deliberately independent of V1/V2/V3 strategy code.  It reads
only the fixed-width FAST/MEDIUM research caches, coalesces same-timestamp
BID/ASK updates into complete IEX top-of-book snapshots, and computes the
requested information-source diagnostics in one shared chronological pass.

The forward-return side of the analysis is a deterministic bounded sample.
Event counts, minima/maxima, means of sampled observations, quote/trade counts,
and window sums are maintained directly; sampled quantiles are labelled as
approximate in every result.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import time
from typing import Callable, Iterable, Iterator

from .research_cache import (
    CACHE_RECORD_SIZE,
    FAST_WINDOWS,
    MEDIUM_WINDOWS,
    CacheRecord,
    iter_cache_records,
    sha256_file,
)
from .strategy_v3_research import _cache_paths, SYMBOL_NAMES


HORIZONS_NS = (10_000_000, 100_000_000, 500_000_000, 1_000_000_000, 5_000_000_000, 15_000_000_000)
HORIZON_NAMES = {10_000_000: "10ms", 100_000_000: "100ms", 500_000_000: "500ms", 1_000_000_000: "1s", 5_000_000_000: "5s", 15_000_000_000: "15s"}
WINDOW_HORIZONS_NS = (10_000_000, 100_000_000, 500_000_000, 1_000_000_000, 5_000_000_000)
WINDOW_NAMES = {h: HORIZON_NAMES[h] for h in WINDOW_HORIZONS_NS}
SAMPLE_STRIDE = 64
STATS_SAMPLE_LIMIT = 4_000
QUIET_PERIODS_NS = (100_000_000, 500_000_000, 1_000_000_000, 5_000_000_000)
TRADE_SIGN_NAMES = ("BUY", "SELL", "LIKELY_BUY", "LIKELY_SELL", "UNKNOWN")


def _trunc_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ZeroDivisionError
    return numerator // denominator if numerator >= 0 else -((-numerator) // denominator)


def microprice(bid_price: int, ask_price: int, bid_size: int, ask_size: int) -> int | None:
    """Integer microprice, rounded toward zero, or None for zero liquidity."""
    denominator = bid_size + ask_size
    if denominator <= 0:
        return None
    return _trunc_div(ask_price * bid_size + bid_price * ask_size, denominator)


def microprice_minus_mid_bps_x100(value: int, midpoint: int) -> int | None:
    if midpoint <= 0:
        return None
    return _trunc_div((value - midpoint) * 100_000_000, midpoint)


def ofi_delta(previous: "Snapshot", current: "Snapshot") -> int:
    """Cont et al. top-of-book OFI, using integer quantities.

    Bid contribution is current bid size when price improves, negative prior
    bid size when price worsens, and size change at an unchanged price.  Ask
    contribution is negative prior ask size on an ask-price improvement,
    current ask size on an ask-price worsening, and size change unchanged;
    OFI is bid contribution minus ask contribution.
    """
    if current.bid_price > previous.bid_price:
        bid = current.bid_size
    elif current.bid_price < previous.bid_price:
        bid = -previous.bid_size
    else:
        bid = current.bid_size - previous.bid_size
    if current.ask_price > previous.ask_price:
        ask = -previous.ask_size
    elif current.ask_price < previous.ask_price:
        ask = current.ask_size
    else:
        ask = current.ask_size - previous.ask_size
    return bid - ask


@dataclass(frozen=True, slots=True)
class Snapshot:
    timestamp_ns: int
    symbol_id: int
    segment_index: int
    session_index: int
    measured: bool
    bid_price: int
    ask_price: int
    bid_size: int
    ask_size: int
    midpoint: int
    spread: int
    bid_updates: int = 1
    ask_updates: int = 1

    @property
    def valid(self) -> bool:
        return self.bid_price > 0 and self.ask_price > 0 and self.bid_size >= 0 and self.ask_size >= 0


@dataclass(frozen=True, slots=True)
class TradeEvent:
    timestamp_ns: int
    symbol_id: int
    segment_index: int
    session_index: int
    measured: bool
    price: int
    quantity: int


class SnapshotCoalescer:
    """Emit complete snapshots without exposing intermediate one-sided state."""

    def __init__(self, on_snapshot: Callable[[Snapshot], None], on_trade: Callable[[TradeEvent], None], on_reset: Callable[[int, int], None] | None = None):
        self.on_snapshot = on_snapshot
        self.on_trade = on_trade
        self.on_reset = on_reset
        self._scope: tuple[int, int] | None = None
        self._key: tuple[int, int, int, int] | None = None
        self._bid: CacheRecord | None = None
        self._ask: CacheRecord | None = None
        self._emitted_bid_sequence: int | None = None
        self._emitted_ask_sequence: int | None = None
        self.paired_groups = 0
        self.incomplete_groups = 0
        self.duplicate_side_updates = 0
        self.trade_records = 0

    def _flush_group(self) -> None:
        if self._key is not None:
            if self._bid is not None and self._ask is not None:
                self.paired_groups += 1
            else:
                self.incomplete_groups += 1
        self._key = None
        self._bid = None
        self._ask = None
        self._emitted_bid_sequence = None
        self._emitted_ask_sequence = None

    def _start_scope_if_needed(self, record: CacheRecord) -> None:
        scope = (record.segment_index, record.session_index)
        if self._scope is None:
            self._scope = scope
        elif scope != self._scope:
            self._flush_group()
            self._scope = scope
            if self.on_reset:
                self.on_reset(*scope)

    def process(self, record: CacheRecord) -> None:
        self._start_scope_if_needed(record)
        if record.event_type != 1:
            key = (record.segment_index, record.session_index, record.symbol_id, record.timestamp_ns)
            # A trade between BID and ASK at the same source timestamp must not
            # discard the quote pair.  It is emitted immediately using the
            # prior complete quote; the pair is emitted only when the second
            # side arrives.
            if self._key != key:
                self._flush_group()
            self.trade_records += 1
            self.on_trade(TradeEvent(record.timestamp_ns, record.symbol_id, record.segment_index, record.session_index, record.measured, record.price, record.quantity))
            return
        key = (record.segment_index, record.session_index, record.symbol_id, record.timestamp_ns)
        if self._key != key:
            self._flush_group()
            self._key = key
        if record.side == 0:
            if self._bid is not None:
                self.duplicate_side_updates += 1
            self._bid = record
        else:
            if self._ask is not None:
                self.duplicate_side_updates += 1
            self._ask = record
        if self._bid is None or self._ask is None:
            return
        if self._emitted_bid_sequence == self._bid.sequence and self._emitted_ask_sequence == self._ask.sequence:
            return
        midpoint = (self._bid.bid_price + self._ask.ask_price) // 2
        snapshot = Snapshot(
            timestamp_ns=record.timestamp_ns,
            symbol_id=record.symbol_id,
            segment_index=record.segment_index,
            session_index=record.session_index,
            measured=self._bid.measured and self._ask.measured,
            bid_price=self._bid.bid_price,
            ask_price=self._ask.ask_price,
            bid_size=self._bid.bid_quantity,
            ask_size=self._ask.ask_quantity,
            midpoint=midpoint,
            spread=self._ask.ask_price - self._bid.bid_price,
        )
        self._emitted_bid_sequence = self._bid.sequence
        self._emitted_ask_sequence = self._ask.sequence
        self.on_snapshot(snapshot)

    def flush(self) -> None:
        self._flush_group()


def classify_trade(price: int, bid: int | None, ask: int | None, midpoint: int | None) -> str:
    if bid is None or ask is None or midpoint is None or bid <= 0 or ask <= 0 or ask < bid:
        return "UNKNOWN"
    if price >= ask:
        return "BUY"
    if price <= bid:
        return "SELL"
    if price > midpoint:
        return "LIKELY_BUY"
    if price < midpoint:
        return "LIKELY_SELL"
    return "UNKNOWN"


def bucket_edges(values: Iterable[float]) -> list[float]:
    ordered = sorted(values)
    if not ordered:
        return []
    return [ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * p)))] for p in (0.0, .10, .40, .60, .90, 1.0)]


def assign_bucket(value: float, edges: list[float]) -> str:
    labels = ("most_negative", "negative", "near_zero", "positive", "most_positive")
    if not edges:
        return "unbucketed"
    for index in range(1, len(edges)):
        if value <= edges[index]:
            return labels[index - 1]
    return labels[-1]


def aggregate_window(points: deque[tuple[int, int]], now_ns: int, horizon_ns: int) -> int:
    while points and points[0][0] < now_ns - horizon_ns:
        points.popleft()
    return sum(value for _, value in points)


def cluster_event_times(timestamps: Iterable[int], quiet_ns: int) -> tuple[int, list[int]]:
    first: list[int] = []
    last = None
    for timestamp in sorted(timestamps):
        if last is None or timestamp - last >= quiet_ns:
            first.append(timestamp)
        last = timestamp
    return len(first), first


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    lm = statistics.fmean(left)
    rm = statistics.fmean(right)
    numerator = sum((a - lm) * (b - rm) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a - lm) ** 2 for a in left) * sum((b - rm) ** 2 for b in right))
    return numerator / denominator if denominator else 0.0


class Stats:
    __slots__ = ("count", "total", "minimum", "maximum", "positive", "sample")

    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.minimum = None
        self.maximum = None
        self.positive = 0
        self.sample: list[float] = []

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        self.positive += int(value > 0)
        if len(self.sample) < STATS_SAMPLE_LIMIT:
            self.sample.append(value)
        elif self.count % 16 == 0:
            self.sample[(self.count // 16) % STATS_SAMPLE_LIMIT] = value

    def summary(self) -> dict:
        ordered = sorted(self.sample)
        return {
            "count": self.count,
            "mean_bps": self.total / self.count if self.count else None,
            "minimum_bps": self.minimum,
            "maximum_bps": self.maximum,
            "favorable_fraction": self.positive / self.count if self.count else None,
            "median_bps_approx": statistics.median(ordered) if ordered else None,
            "quantiles_approximate": True,
            "quantile_sample_count": len(ordered),
        }


@dataclass(slots=True)
class SampleObservation:
    timestamp_ns: int
    symbol_id: int
    day: str
    window: str
    entry_midpoint: int
    features: dict[str, float]
    future: dict[int, float] = field(default_factory=dict)


@dataclass(slots=True)
class LeadLagObservation:
    timestamp_ns: int
    source: str
    target: str
    feature: str
    value: float
    entry_midpoint: int
    future: dict[int, float] = field(default_factory=dict)


@dataclass(slots=True)
class EventPoint:
    timestamp_ns: int
    ofi: int
    bid_withdrawal: int
    ask_withdrawal: int
    bid_replenishment: int
    ask_replenishment: int
    spread_change: int
    quote_updates: int
    bid_updates: int
    ask_updates: int


@dataclass(slots=True)
class TradePoint:
    timestamp_ns: int
    quantity: int
    signed_quantity: int
    sign: str


class RollingSums:
    """Amortized-O(1) timestamp window sums for one requested horizon."""

    __slots__ = ("horizon_ns", "points", "totals")

    def __init__(self, horizon_ns: int, width: int):
        self.horizon_ns = horizon_ns
        self.points: deque[tuple[int, tuple[int, ...]]] = deque()
        self.totals = [0] * width

    def append(self, timestamp_ns: int, values: tuple[int, ...]) -> None:
        self.points.append((timestamp_ns, values))
        for index, value in enumerate(values):
            self.totals[index] += value
        self.trim(timestamp_ns)

    def trim(self, now_ns: int) -> None:
        cutoff = now_ns - self.horizon_ns
        while self.points and self.points[0][0] < cutoff:
            _, values = self.points.popleft()
            for index, value in enumerate(values):
                self.totals[index] -= value

    def values(self, now_ns: int) -> tuple[int, ...]:
        self.trim(now_ns)
        return tuple(self.totals)


def _return_bps(entry: int, future: int) -> float | None:
    if entry <= 0 or future <= 0:
        return None
    return (future - entry) * 10_000.0 / entry


def _window_name(segment_index: int, windows) -> tuple[str, str]:
    if 0 <= segment_index < len(windows):
        window = windows[segment_index]
        return window.day, window.name
    return "unknown", f"segment_{segment_index}"


class DiscoveryEngine:
    def __init__(self, windows, *, total_records: int, progress_label: str, max_records: int | None = None, stage_limit_seconds: float = 1_800.0):
        self.windows = windows
        self.total_records = min(total_records, max_records) if max_records else total_records
        self.progress_label = progress_label
        self.max_records = max_records
        self.stage_limit_seconds = stage_limit_seconds
        self.started = time.perf_counter()
        self.elapsed_seconds = 0.0
        self.last_progress = [self.started, 0]
        self.processed_records = 0
        self.quote_records = 0
        self.trade_records = 0
        self.measured_quotes = 0
        self.complete_snapshots = 0
        self.valid_snapshots = 0
        self.bad_snapshots = 0
        self.segment_resets = 0
        self.session_resets = 0
        self.per_symbol_snapshots = defaultdict(int)
        self.per_symbol_trades = defaultdict(int)
        self.trade_sign_counts = defaultdict(int)
        self.trade_quote_age = defaultdict(Stats)
        self.previous: dict[int, Snapshot] = {}
        self.latest_quote: dict[int, Snapshot] = {}
        self.event_windows: dict[int, dict[int, RollingSums]] = defaultdict(dict)
        self.trade_windows: dict[int, dict[int, RollingSums]] = defaultdict(dict)
        self.samples: dict[int, list[SampleObservation]] = defaultdict(list)
        self.pending_samples: dict[int, list[SampleObservation]] = defaultdict(list)
        self.resolved_samples: list[SampleObservation] = []
        self.sample_serial = 0
        self.latest_signal: dict[int, tuple[int, dict[str, float]]] = {}
        self.lead_pending: dict[int, list[LeadLagObservation]] = defaultdict(list)
        self.lead_resolved: list[LeadLagObservation] = []
        self.snapshot_validation = defaultdict(int)
        self.withdrawal_counts = defaultdict(int)
        self.replenishment_counts = defaultdict(int)
        self.quote_update_counts = defaultdict(int)
        self.flow_counts = defaultdict(int)
        self._active_scope: tuple[int, int] | None = None

    def _progress(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and self.processed_records - self.last_progress[1] < 1_000_000 and now - self.last_progress[0] < 30:
            return
        elapsed = now - self.started
        rate = self.processed_records / elapsed if elapsed else 0.0
        remaining = (self.total_records - self.processed_records) / rate if rate else None
        print(self.progress_label + " " + json.dumps({
            "processed_events": self.processed_records,
            "total_events": self.total_records,
            "percent_complete": self.processed_records / self.total_records * 100 if self.total_records else None,
            "elapsed_time": elapsed,
            "processing_rate_events_per_second": rate,
            "estimated_remaining_time": remaining,
        }, sort_keys=True), flush=True)
        self.last_progress[:] = [now, self.processed_records]

    def reset(self, segment_index: int, session_index: int) -> None:
        self.segment_resets += 1
        if self._active_scope is not None and self._active_scope[1] != session_index:
            self.session_resets += 1
        self._active_scope = (segment_index, session_index)
        self.previous.clear()
        self.latest_quote.clear()
        self.event_windows.clear()
        self.trade_windows.clear()
        self.latest_signal.clear()
        self.pending_samples.clear()
        self.lead_pending.clear()

    def _resolve_samples(self, symbol_id: int, timestamp_ns: int, midpoint: int) -> None:
        queue = self.pending_samples[symbol_id]
        for observation in queue:
            for horizon in HORIZONS_NS:
                if horizon not in observation.future and timestamp_ns >= observation.timestamp_ns + horizon:
                    value = _return_bps(observation.entry_midpoint, midpoint)
                    if value is not None:
                        observation.future[horizon] = value
        while queue and len(queue[0].future) == len(HORIZONS_NS):
            self.resolved_samples.append(queue.pop(0))

    def _resolve_lead(self, symbol_id: int, timestamp_ns: int, midpoint: int) -> None:
        queue = self.lead_pending[symbol_id]
        for observation in queue:
            for horizon in WINDOW_HORIZONS_NS:
                if horizon not in observation.future and timestamp_ns >= observation.timestamp_ns + horizon:
                    value = _return_bps(observation.entry_midpoint, midpoint)
                    if value is not None:
                        observation.future[horizon] = value
        while queue and len(queue[0].future) == len(WINDOW_HORIZONS_NS):
            self.lead_resolved.append(queue.pop(0))

    def _window_values(self, sid: int, timestamp_ns: int) -> dict[str, float]:
        points = self.event_windows[sid]
        trades = self.trade_windows[sid]
        result: dict[str, float] = {}
        for horizon in WINDOW_HORIZONS_NS:
            name = WINDOW_NAMES[horizon]
            event_values = points.setdefault(horizon, RollingSums(horizon, 4)).values(timestamp_ns)
            trade_values = trades.setdefault(horizon, RollingSums(horizon, 4)).values(timestamp_ns)
            result[f"ofi_{name}"] = float(event_values[0])
            result[f"quote_intensity_{name}"] = float(event_values[1])
            result[f"bid_quote_intensity_{name}"] = float(event_values[2])
            result[f"ask_quote_intensity_{name}"] = float(event_values[3])
            result[f"quote_update_asymmetry_{name}"] = float(event_values[2] - event_values[3])
            result[f"flow_signed_volume_{name}"] = float(trade_values[0])
            result[f"flow_signed_count_{name}"] = float(trade_values[1])
            result[f"flow_buy_volume_{name}"] = float(trade_values[2])
            result[f"flow_sell_volume_{name}"] = float(trade_values[3])
        return result

    def on_trade(self, trade: TradeEvent) -> None:
        self.trade_records += 1
        self.per_symbol_trades[SYMBOL_NAMES.get(trade.symbol_id, str(trade.symbol_id))] += 1
        quote = self.latest_quote.get(trade.symbol_id)
        if quote is None or quote.timestamp_ns > trade.timestamp_ns:
            sign = "UNKNOWN"
            age = None
        else:
            sign = classify_trade(trade.price, quote.bid_price, quote.ask_price, quote.midpoint)
            age = trade.timestamp_ns - quote.timestamp_ns
        self.trade_sign_counts[sign] += 1
        self.flow_counts[f"{SYMBOL_NAMES.get(trade.symbol_id, str(trade.symbol_id))}:{sign}"] += 1
        if age is not None:
            self.trade_quote_age[SYMBOL_NAMES.get(trade.symbol_id, str(trade.symbol_id))].observe(age / 1_000_000.0)
        signed = trade.quantity if sign in ("BUY", "LIKELY_BUY") else -trade.quantity if sign in ("SELL", "LIKELY_SELL") else 0
        windows = self.trade_windows[trade.symbol_id]
        for horizon in WINDOW_HORIZONS_NS:
            windows.setdefault(horizon, RollingSums(horizon, 4)).append(
                trade.timestamp_ns,
                (signed, 1 if signed > 0 else -1 if signed < 0 else 0,
                 trade.quantity if sign in ("BUY", "LIKELY_BUY") else 0,
                 trade.quantity if sign in ("SELL", "LIKELY_SELL") else 0),
            )

    def on_snapshot(self, current: Snapshot) -> None:
        self.complete_snapshots += 1
        symbol = SYMBOL_NAMES.get(current.symbol_id, str(current.symbol_id))
        self.per_symbol_snapshots[symbol] += 1
        if current.measured:
            self.measured_quotes += 1
        if current.bid_price <= 0 or current.ask_price <= 0 or current.bid_size < 0 or current.ask_size < 0:
            self.bad_snapshots += 1
            self.snapshot_validation["bad_price_or_size"] += 1
        else:
            self.valid_snapshots += 1
        if current.bid_price <= current.ask_price:
            self.snapshot_validation["bid_le_ask"] += 1
        else:
            self.snapshot_validation["crossed"] += 1
        if current.midpoint == (current.bid_price + current.ask_price) // 2:
            self.snapshot_validation["midpoint_correct"] += 1
        else:
            self.snapshot_validation["midpoint_incorrect"] += 1
        if current.spread == current.ask_price - current.bid_price:
            self.snapshot_validation["spread_correct"] += 1
        else:
            self.snapshot_validation["spread_incorrect"] += 1
        self._resolve_samples(current.symbol_id, current.timestamp_ns, current.midpoint)
        self._resolve_lead(current.symbol_id, current.timestamp_ns, current.midpoint)
        previous = self.previous.get(current.symbol_id)
        instant_ofi = ofi_delta(previous, current) if previous is not None else None
        bid_withdrawal = ask_withdrawal = bid_replenishment = ask_replenishment = 0
        spread_change = 0
        if previous is not None:
            if current.bid_price < previous.bid_price:
                bid_withdrawal = previous.bid_size
            elif current.bid_price == previous.bid_price and current.bid_size < previous.bid_size:
                bid_withdrawal = previous.bid_size - current.bid_size
            if current.ask_price > previous.ask_price:
                ask_withdrawal = previous.ask_size
            elif current.ask_price == previous.ask_price and current.ask_size < previous.ask_size:
                ask_withdrawal = previous.ask_size - current.ask_size
            if current.bid_price > previous.bid_price or (current.bid_price == previous.bid_price and current.bid_size > previous.bid_size):
                bid_replenishment = max(0, current.bid_size - previous.bid_size)
            if current.ask_price < previous.ask_price or (current.ask_price == previous.ask_price and current.ask_size > previous.ask_size):
                ask_replenishment = max(0, current.ask_size - previous.ask_size)
            spread_change = current.spread - previous.spread
        self.withdrawal_counts[f"{symbol}:bid"] += int(bid_withdrawal > 0)
        self.withdrawal_counts[f"{symbol}:ask"] += int(ask_withdrawal > 0)
        self.replenishment_counts[f"{symbol}:bid"] += int(bid_replenishment > 0)
        self.replenishment_counts[f"{symbol}:ask"] += int(ask_replenishment > 0)
        point = EventPoint(current.timestamp_ns, instant_ofi or 0, bid_withdrawal, ask_withdrawal, bid_replenishment, ask_replenishment, spread_change, 1, 1, 1)
        event_windows = self.event_windows[current.symbol_id]
        for horizon in WINDOW_HORIZONS_NS:
            event_windows.setdefault(horizon, RollingSums(horizon, 4)).append(
                current.timestamp_ns, (point.ofi, point.quote_updates, point.bid_updates, point.ask_updates)
            )
        self.quote_update_counts[symbol] += 1
        window_values = self._window_values(current.symbol_id, current.timestamp_ns)
        micro = microprice(current.bid_price, current.ask_price, current.bid_size, current.ask_size)
        micro_bps = microprice_minus_mid_bps_x100(micro, current.midpoint) if micro is not None else None
        denominator = current.bid_size + current.ask_size
        static_imbalance = _trunc_div((current.bid_size - current.ask_size) * 32_768, denominator) if denominator else None
        day, window = _window_name(current.segment_index, self.windows)
        features: dict[str, float] = {}
        if micro_bps is not None:
            features["microprice_bps_x100"] = float(micro_bps)
        if static_imbalance is not None:
            features["static_imbalance_q15"] = float(static_imbalance)
        if instant_ofi is not None:
            features["ofi_instant"] = float(instant_ofi)
        features.update(window_values)
        if previous is not None:
            features["bid_withdrawal"] = float(bid_withdrawal)
            features["ask_withdrawal"] = float(ask_withdrawal)
            features["bid_replenishment"] = float(bid_replenishment)
            features["ask_replenishment"] = float(ask_replenishment)
            features["withdrawal_asymmetry"] = float(ask_withdrawal - bid_withdrawal)
            features["replenishment_asymmetry"] = float(ask_replenishment - bid_replenishment)
            features["spread_change"] = float(spread_change)
            features["spread_widening"] = float(max(0, spread_change))
            features["spread_narrowing"] = float(max(0, -spread_change))
        quote_intensity = window_values.get("quote_intensity_1s", 0.0)
        trade_count = abs(window_values.get("flow_signed_count_1s", 0.0))
        features["quote_trade_activity_ratio_1s"] = quote_intensity / (trade_count + 1.0)
        self.sample_serial += 1
        if current.measured and current.valid and self.sample_serial % SAMPLE_STRIDE == 0 and current.midpoint > 0:
            observation = SampleObservation(current.timestamp_ns, current.symbol_id, day, window, current.midpoint, features)
            self.pending_samples[current.symbol_id].append(observation)
            self.samples[current.symbol_id].append(observation)
        signal = {"ofi_1s": window_values.get("ofi_1s", 0.0), "microprice_bps_x100": float(micro_bps or 0), "flow_signed_volume_1s": window_values.get("flow_signed_volume_1s", 0.0)}
        self.latest_signal[current.symbol_id] = (current.timestamp_ns, signal)
        for source, targets in ((1, (2, 3)), (0, (1,))):
            if current.symbol_id not in targets:
                continue
            latest = self.latest_signal.get(source)
            if latest is None or latest[0] > current.timestamp_ns:
                continue
            source_name = SYMBOL_NAMES[source]
            target_name = SYMBOL_NAMES[current.symbol_id]
            for feature_name, value in latest[1].items():
                if self.sample_serial % SAMPLE_STRIDE == 0:
                    self.lead_pending[current.symbol_id].append(LeadLagObservation(current.timestamp_ns, source_name, target_name, feature_name, value, current.midpoint))
        self.previous[current.symbol_id] = current
        self.latest_quote[current.symbol_id] = current

    def process(self, record: CacheRecord) -> None:
        if self.max_records is not None and self.processed_records >= self.max_records:
            return
        self.processed_records += 1
        self._progress()
        if time.perf_counter() - self.started > self.stage_limit_seconds:
            raise TimeoutError(f"{self.progress_label} exceeded automatic stage limit of {self.stage_limit_seconds:.0f}s")
        if record.event_type == 1:
            self.quote_records += 1
        coalescer = getattr(self, "coalescer", None)
        if coalescer is None:
            self.coalescer = SnapshotCoalescer(self.on_snapshot, self.on_trade, self.reset)
            coalescer = self.coalescer
        coalescer.process(record)

    def finish(self) -> dict:
        if hasattr(self, "coalescer"):
            self.coalescer.flush()
            self.paired_quote_groups = self.coalescer.paired_groups
            self.incomplete_quote_groups = self.coalescer.incomplete_groups
            self.duplicate_side_updates = self.coalescer.duplicate_side_updates
        self._progress(force=True)
        elapsed = time.perf_counter() - self.started
        self.elapsed_seconds = elapsed
        return self.summary(elapsed)

    def summary(self, elapsed: float) -> dict:
        return {
            "processed_records": self.processed_records,
            "quote_records": self.quote_records,
            "trade_records": self.trade_records,
            "measured_quote_records": self.measured_quotes,
            "complete_snapshots": self.complete_snapshots,
            "valid_snapshots": self.valid_snapshots,
            "bad_snapshots": self.bad_snapshots,
            "paired_quote_groups": getattr(self, "paired_quote_groups", 0),
            "incomplete_quote_groups": getattr(self, "incomplete_quote_groups", 0),
            "duplicate_side_updates": getattr(self, "duplicate_side_updates", 0),
            "segment_resets": self.segment_resets,
            "session_resets": self.session_resets,
            "per_symbol_snapshots": dict(sorted(self.per_symbol_snapshots.items())),
            "per_symbol_trades": dict(sorted(self.per_symbol_trades.items())),
            "snapshot_validation": dict(sorted(self.snapshot_validation.items())),
            "trade_sign_counts": dict(sorted(self.trade_sign_counts.items())),
            "elapsed_seconds": elapsed,
            "records_per_second": self.processed_records / elapsed if elapsed else None,
            "sample_stride": SAMPLE_STRIDE,
            "sample_count": len(self.resolved_samples),
        }


def _stats_for(values: Iterable[float]) -> dict:
    stats = Stats()
    for value in values:
        stats.observe(value)
    return stats.summary()


def summarize_feature(observations: list[SampleObservation], feature_name: str) -> dict:
    usable = [o for o in observations if feature_name in o.features]
    edges = bucket_edges(o.features[feature_name] for o in usable)
    result = {"feature": feature_name, "observations": len(usable), "raw_event_count_exact_not_sampled": len(observations), "bucket_edges_approx": edges, "bucket_method": "signed empirical sample edges at 0/10/40/60/90/100 percentiles", "horizons": {}}
    for horizon in HORIZONS_NS:
        buckets: dict[str, Stats] = {}
        by_symbol: dict[str, dict[str, Stats]] = defaultdict(dict)
        by_day: dict[str, dict[str, Stats]] = defaultdict(dict)
        by_window: dict[str, dict[str, Stats]] = defaultdict(dict)
        for observation in usable:
            value = observation.future.get(horizon)
            if value is None:
                continue
            bucket = assign_bucket(observation.features[feature_name], edges)
            buckets.setdefault(bucket, Stats()).observe(value)
            symbol = SYMBOL_NAMES.get(observation.symbol_id, str(observation.symbol_id))
            by_symbol.setdefault(symbol, {}).setdefault(bucket, Stats()).observe(value)
            by_day.setdefault(observation.day, {}).setdefault(bucket, Stats()).observe(value)
            by_window.setdefault(observation.window, {}).setdefault(bucket, Stats()).observe(value)
        def render(mapping):
            return {key: {bucket: stats.summary() for bucket, stats in sorted(bucket_stats.items())} for key, bucket_stats in sorted(mapping.items())}
        aggregate = {bucket: stats.summary() for bucket, stats in sorted(buckets.items())}
        ordered_labels = ("most_negative", "negative", "near_zero", "positive", "most_positive")
        means = [aggregate.get(label, {}).get("mean_bps") for label in ordered_labels]
        present = [mean for mean in means if mean is not None]
        monotonic_pairs = sum(1 for left, right in zip(present, present[1:]) if right >= left)
        top_bottom = present[-1] - present[0] if len(present) >= 2 else None
        result["horizons"][HORIZON_NAMES[horizon]] = {
            "aggregate": aggregate,
            "by_symbol": render(by_symbol),
            "by_day": render(by_day),
            "by_window": render(by_window),
            "top_bottom_mean_bps": top_bottom,
            "monotonicity_fraction": monotonic_pairs / (len(present) - 1) if len(present) > 1 else None,
            "favorable_fraction_top_bucket": aggregate.get("most_positive", {}).get("favorable_fraction"),
        }
    return result


def summarize_lead_lag(observations: list[LeadLagObservation]) -> dict:
    grouped: dict[str, list[SampleObservation]] = defaultdict(list)
    for observation in observations:
        grouped[f"{observation.source}->{observation.target}:{observation.feature}"].append(SampleObservation(observation.timestamp_ns, 0, "unknown", "unknown", observation.entry_midpoint, {"value": observation.value}, observation.future))
    return {key: summarize_feature(value, "value") for key, value in sorted(grouped.items())}


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_csv(path: Path, observations: list[SampleObservation], feature_names: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["timestamp_ns", "symbol", "day", "window", *feature_names, *[f"future_{HORIZON_NAMES[h]}_bps" for h in HORIZONS_NS]])
        for observation in observations:
            writer.writerow([observation.timestamp_ns, SYMBOL_NAMES.get(observation.symbol_id, str(observation.symbol_id)), observation.day, observation.window, *[observation.features.get(name) for name in feature_names], *[observation.future.get(h) for h in HORIZONS_NS]])


def _benchmark_cache(path: Path, windows, total: int, label: str, sample: int = 100_000) -> dict:
    engine = DiscoveryEngine(windows, total_records=total, progress_label=label, max_records=sample)
    started = time.perf_counter()
    for record in iter_cache_records(path):
        engine.process(record)
        if engine.processed_records >= sample:
            break
    summary = engine.finish()
    elapsed = time.perf_counter() - started
    rate = sample / elapsed if elapsed else 0.0
    projected = total / rate if rate else None
    return {"sample_records": sample, "elapsed_seconds": elapsed, "records_per_second": rate, "projected_full_seconds": projected, "projected_full_minutes": projected / 60 if projected else None, "summary": summary}


def _run_cache(path: Path, windows, total: int, label: str, *, stage_limit_seconds: float = 1_800.0) -> DiscoveryEngine:
    engine = DiscoveryEngine(windows, total_records=total, progress_label=label, stage_limit_seconds=stage_limit_seconds)
    for record in iter_cache_records(path):
        engine.process(record)
    engine.finish()
    return engine


def _feature_artifacts(engine: DiscoveryEngine, result_dir: Path, prefix: str) -> dict:
    features = sorted({name for observation in engine.resolved_samples for name in observation.features})
    summaries = {feature: summarize_feature(engine.resolved_samples, feature) for feature in features}
    micro = {"units": "bps_x100", "formula": "microprice=(ask_price*bid_size + bid_price*ask_size)/(bid_size+ask_size), integer truncation toward zero; displacement=(microprice-midpoint)*100000000/midpoint", "result": summaries.get("microprice_bps_x100", {})}
    static = {"units": "q15", "formula": "(bid_size-ask_size)*32768/(bid_size+ask_size), integer truncation toward zero", "result": summaries.get("static_imbalance_q15", {}), "comparison_note": "This is a comparison to the existing cached imbalance semantics, not an independent signal when highly correlated."}
    ofi_names = [name for name in features if name == "ofi_instant" or name.startswith("ofi_")]
    flow_names = [name for name in features if name.startswith("flow_")]
    withdrawal_names = [name for name in features if "withdrawal" in name]
    replenishment_names = [name for name in features if "replenishment" in name]
    spread_names = [name for name in features if name.startswith("spread_")]
    intensity_names = [name for name in features if "intensity" in name]
    activity_names = [name for name in features if "activity_ratio" in name]
    payloads = {
        "microprice.json": micro,
        "static_imbalance_comparison.json": static,
        "ofi.json": {"exact_formula": "bid_delta - ask_delta; bid_delta=curr_size if bid improves, -prev_size if bid worsens, curr-prev unchanged; ask_delta=-prev_size if ask improves, curr_size if ask worsens, curr-prev unchanged", "features": {name: summaries[name] for name in ofi_names}},
        "liquidity_withdrawal.json": {"definition": "bid withdrawal is bid price moving lower or unchanged-price bid size decrease; ask withdrawal is ask price moving higher or unchanged-price ask size decrease", "counts": dict(sorted(engine.withdrawal_counts.items())), "features": {name: summaries[name] for name in withdrawal_names}},
        "liquidity_replenishment.json": {"definition": "bid replenishment is bid price improving or unchanged-price bid size increase; ask replenishment is ask price improving or unchanged-price ask size increase", "counts": dict(sorted(engine.replenishment_counts.items())), "features": {name: summaries[name] for name in replenishment_names}},
        "spread_dynamics.json": {"features": {name: summaries[name] for name in spread_names}},
        "quote_intensity.json": {"exact_quote_update_counts": dict(sorted(engine.quote_update_counts.items())), "features": {name: summaries[name] for name in intensity_names}},
        "signed_trade_flow.json": {"sign_definitions": TRADE_SIGN_NAMES, "exact_trade_sign_counts": dict(sorted(engine.trade_sign_counts.items())), "features": {name: summaries[name] for name in flow_names}},
        "quote_trade_activity.json": {"features": {name: summaries[name] for name in activity_names}, "interpretation": "descriptive quote-update to signed-trade-count ratio; not a strategy input"},
    }
    for filename, payload in payloads.items():
        _write_json(result_dir / (prefix + filename if prefix else filename), payload)
    # Report sample-based shock candidates, with empirical bounded-sample
    # thresholds and explicit raw/de-clustered counts.
    shock_rows = []
    shock_specs = {
        "ofi_1s": "absolute",
        "bid_withdrawal": "upper",
        "ask_withdrawal": "upper",
        "spread_widening": "upper",
        "flow_signed_volume_1s": "absolute",
    }
    thresholds = {}
    for family, mode in shock_specs.items():
        values = [abs(o.features[family]) if mode == "absolute" else o.features[family] for o in engine.resolved_samples if family in o.features]
        ordered = sorted(values)
        thresholds[family] = ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * .90)))] if ordered else None
    for observation in engine.resolved_samples:
        for family, mode in shock_specs.items():
            threshold = thresholds[family]
            if family not in observation.features or threshold is None:
                continue
            value = abs(observation.features[family]) if mode == "absolute" else observation.features[family]
            if value >= threshold and value > 0:
                shock_rows.append((observation.timestamp_ns, family))
    shock = {}
    for family in sorted({family for _, family in shock_rows}):
        timestamps = [timestamp for timestamp, item in shock_rows if item == family]
        shock[family] = {"raw_sample_event_count": len(timestamps), "clustered_first_event_counts": {str(q): cluster_event_times(timestamps, q)[0] for q in QUIET_PERIODS_NS}, "quiet_periods_ns": list(QUIET_PERIODS_NS), "empirical_p90_threshold": thresholds[family], "threshold_mode": shock_specs[family]}
    _write_json(result_dir / (prefix + "liquidity_shocks.json" if prefix else "liquidity_shocks.json"), {"events": shock, "thresholds": thresholds, "dependence_note": "raw and quiet-period de-clustered counts are shown; thresholds are bounded-sample empirical P90 values; no formal naive significance is claimed"})
    return summaries


def _feature_redundancy(engine: DiscoveryEngine) -> dict:
    names = ("microprice_bps_x100", "static_imbalance_q15", "ofi_1s", "flow_signed_volume_1s")
    result = {"correlations": {}}
    for left_index, left in enumerate(names):
        for right in names[left_index + 1:]:
            pairs = [(o.features[left], o.features[right]) for o in engine.resolved_samples if left in o.features and right in o.features]
            result["correlations"][f"{left}__{right}"] = {"pearson_approx": _pearson([a for a, _ in pairs], [b for _, b in pairs]), "sample_count": len(pairs)}
    result["interpretation"] = "high correlation or identical monotonic response means the features are redundant and must not be counted as independent discoveries"
    return result


def _write_report(result_dir: Path, benchmark: dict, engine: DiscoveryEngine, summaries: dict, promoted: list[str], medium: dict | None) -> None:
    summary = engine.summary(benchmark.get("fast", {}).get("observed_elapsed_seconds", 0.0))
    top = []
    for feature in ("microprice_bps_x100", "ofi_1s", "flow_signed_volume_1s", "spread_change"):
        item = summaries.get(feature)
        if item:
            horizon = item.get("horizons", {}).get("500ms", {})
            top.append((feature, horizon.get("top_bottom_mean_bps"), horizon.get("monotonicity_fraction")))
    lines = [
        "# Microstructure information-source discovery",
        "",
        "This is an independent research diagnostic. It does not create Strategy V4, alter V1/V2/V3, simulate P&L, or change FPGA semantics.",
        "",
        "## Data and runtime",
        f"- FAST records reused: {summary['processed_records']:,}; complete snapshots: {summary['complete_snapshots']:,}.",
        f"- MEDIUM promotion requested: {', '.join(promoted) if promoted else 'none; manual review required'}.",
        f"- Full canonical passes: 0; Alpaca/download calls: 0.",
        f"- Benchmark: {json.dumps(benchmark, sort_keys=True)}",
        "",
        "## Causality and units",
        "Same-timestamp BID/ASK updates are coalesced before feature evaluation. Trades use the latest complete quote at or before the trade. Forward returns are midpoint returns and all bucket quantiles are bounded-sample approximations.",
        "",
        "## FAST directional review",
    ]
    for feature, separation, monotonicity in top:
        lines.append(f"- {feature}: 500ms top-minus-bottom mean={separation!r} bps; monotonicity={monotonicity!r}.")
    lines += ["", "## Promotion and interpretation", "", f"Promoted families: {', '.join(promoted) if promoted else 'none'}. MEDIUM is only valid with the identical formulas and buckets; no threshold retuning is performed.", "", "The source venue is IEX Level1, not NBBO/SIP. AMD is reported separately because its spread is structurally wide."]
    if medium:
        lines += ["", "## MEDIUM validation", "", json.dumps(medium, sort_keys=True)]
    (result_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_discovery(repo_root: Path, *, promote: list[str] | None = None, stage_limit_seconds: float = 1_800.0) -> Path:
    fast_path, medium_path, fast_manifest, medium_manifest = _cache_paths(repo_root)
    promote = [item for item in (promote or []) if item]
    result_dir = repo_root / "backtest_results" / f"microstructure_discovery_{_run_id()}"
    result_dir.mkdir(parents=True, exist_ok=False)
    state = {"phase": "benchmark", "completed_phases": [], "canonical_full_passes": 0, "alpaca_calls": 0, "downloads": 0, "cache_record_size": CACHE_RECORD_SIZE, "fast_parent_cache_sha256": fast_manifest.get("cache_sha256"), "medium_parent_cache_sha256": medium_manifest.get("cache_sha256"), "promoted": promote}
    _write_json(result_dir / "analysis_state.json", state)
    fast_total = int(fast_manifest["record_count"])
    medium_total = int(medium_manifest["record_count"])
    started = time.perf_counter()
    fast_benchmark = _benchmark_cache(fast_path, FAST_WINDOWS, fast_total, "microstructure_fast_benchmark")
    medium_benchmark = _benchmark_cache(medium_path, MEDIUM_WINDOWS, medium_total, "microstructure_medium_benchmark")
    projected_total = (fast_benchmark["projected_full_seconds"] or 0) + (medium_benchmark["projected_full_seconds"] or 0)
    benchmark = {"fast": fast_benchmark, "medium": medium_benchmark, "projected_total_minutes": projected_total / 60 if projected_total else None, "hard_limit_minutes": stage_limit_seconds / 60, "subset_benchmark_before_full_scan": True}
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    state["completed_phases"].append("benchmark")
    state["phase"] = "fast_discovery"
    _write_json(result_dir / "analysis_state.json", state)
    if fast_benchmark["projected_full_seconds"] and fast_benchmark["projected_full_seconds"] > stage_limit_seconds:
        raise RuntimeError(f"FAST projected runtime exceeds automatic stage limit: {fast_benchmark['projected_full_seconds'] / 60:.1f} minutes")
    fast_engine = _run_cache(fast_path, FAST_WINDOWS, fast_total, "microstructure_fast_discovery", stage_limit_seconds=stage_limit_seconds)
    fast_summary = fast_engine.summary(fast_engine.elapsed_seconds)
    _write_json(result_dir / "snapshot_validation.json", {"cache": "FAST", "parent_cache_sha256": fast_manifest.get("cache_sha256"), **fast_summary, "coalescing_semantics": "same timestamp, symbol, segment and session BID/ASK records are combined; no intermediate one-sided state is evaluated"})
    summaries = _feature_artifacts(fast_engine, result_dir, "")
    _write_json(result_dir / "trade_sign_quality.json", {"exact_counts": dict(sorted(fast_engine.trade_sign_counts.items())), "quote_age_ms_by_symbol": {key: value.summary() for key, value in sorted(fast_engine.trade_quote_age.items())}, "rules": ["trade_price >= ask => BUY", "trade_price <= bid => SELL", "trade_price > midpoint => LIKELY_BUY", "trade_price < midpoint => LIKELY_SELL", "otherwise UNKNOWN"], "unknown_is_separate": True})
    _write_json(result_dir / "lead_lag.json", {"causal_mappings": ["QQQ->NVDA", "QQQ->AMD", "SPY->QQQ"], "horizons": [HORIZON_NAMES[h] for h in WINDOW_HORIZONS_NS], "result": summarize_lead_lag(fast_engine.lead_resolved), "no_future_benchmark_use": True})
    redundancy = _feature_redundancy(fast_engine)
    _write_json(result_dir / "feature_redundancy.json", redundancy)
    feature_names = sorted({name for observation in fast_engine.resolved_samples for name in observation.features})
    _write_csv(result_dir / "fast_feature_summary.csv", fast_engine.resolved_samples[:5000], feature_names)
    promoted = promote
    _write_json(result_dir / "fast_promoted.json", {"promoted": promoted, "manual_review_required": True, "selection_rule": "Only manually selected FAST families may be validated; no automatic promotion or retuning.", "fast_summary": {name: {"500ms_top_bottom_mean_bps": data.get("horizons", {}).get("500ms", {}).get("top_bottom_mean_bps"), "500ms_monotonicity": data.get("horizons", {}).get("500ms", {}).get("monotonicity_fraction")} for name, data in summaries.items()}})
    state["completed_phases"].append("fast_discovery")
    state["phase"] = "medium_validation" if promoted else "complete"
    _write_json(result_dir / "analysis_state.json", state)
    medium_result = None
    if promoted:
        if medium_benchmark["projected_full_seconds"] and medium_benchmark["projected_full_seconds"] > stage_limit_seconds:
            raise RuntimeError(f"MEDIUM projected runtime exceeds automatic stage limit: {medium_benchmark['projected_full_seconds'] / 60:.1f} minutes")
        medium_engine = DiscoveryEngine(MEDIUM_WINDOWS, total_records=medium_total, progress_label="microstructure_medium_validation", stage_limit_seconds=stage_limit_seconds)
        for record in iter_cache_records(medium_path):
            medium_engine.process(record)
        medium_engine.finish()
        medium_result = {"summary": medium_engine.summary(0.0), "promoted_families": promoted, "identical_formulas_and_buckets": True, "result": {name: summarize_feature(medium_engine.resolved_samples, name) for name in promoted if name in {n for o in medium_engine.resolved_samples for n in o.features}}}
        _write_json(result_dir / "medium_validation.json", medium_result)
        state["completed_phases"].append("medium_validation")
    else:
        _write_json(result_dir / "medium_validation.json", {"status": "not_run", "reason": "No FAST family was manually promoted; this avoids automatic promotion."})
    feature_summary = {"fast": fast_summary, "promoted": promoted, "medium": medium_result, "strongest_source_candidates": sorted(((name, data.get("horizons", {}).get("500ms", {}).get("top_bottom_mean_bps")) for name, data in summaries.items() if data.get("horizons", {}).get("500ms", {}).get("top_bottom_mean_bps") is not None), key=lambda item: abs(item[1]), reverse=True)[:10]}
    _write_json(result_dir / "feature_discovery_summary.json", feature_summary)
    benchmark["observed_fast_elapsed_seconds"] = time.perf_counter() - started
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    _write_report(result_dir, benchmark, fast_engine, summaries, promoted, medium_result)
    state["completed_phases"].append("artifact_finalization")
    state["phase"] = "complete"
    state["runtime_seconds"] = time.perf_counter() - started
    _write_json(result_dir / "analysis_state.json", state)
    return result_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--promote", default="", help="comma-separated FAST feature names for manual MEDIUM validation")
    args = parser.parse_args(argv)
    result = run_discovery(args.repo_root, promote=[item.strip() for item in args.promote.split(",") if item.strip()])
    print(json.dumps({"result_dir": str(result)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
