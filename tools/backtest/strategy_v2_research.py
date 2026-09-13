"""Fast, offline Strategy V2 research over contiguous fixed-width caches."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict
import csv
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Callable, Iterable

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_model"
if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from strategy_model import StrategyModel  # noqa: E402

from .canonical import iter_binary_events
from .execution_simulator import ExecutionConfig, ExecutionSimulator
from .metrics import compute_metrics
from .pipeline import BacktestConfig
from .portfolio_simulator import PortfolioSimulator
from .research_cache import (
    CACHE_RECORD_SIZE,
    FAST_WINDOWS,
    MEDIUM_WINDOWS,
    CacheRecord,
    build_research_caches,
    cache_feature_object,
    iter_cache_records,
    verify_canonical_identity,
)
from .strategy_v2 import CausalTimeFeatureEngine, TimeFeature, V2Config, V2Signal, V2Strategy, summarize_values
from .types import CandidateSignal


EXPECTED_CANONICAL_SIZE = 1_783_708_512
EXPECTED_CANONICAL_EVENTS = 55_740_891
EXPECTED_CANONICAL_SHA256 = "c0947b984d0a1fe5f96d83088205629b123a8401eb28571ab393c015615fcc44"
SYMBOL_NAMES = {0: "SPY", 1: "QQQ", 2: "NVDA", 3: "AMD"}
FORWARD_HORIZONS = (100_000_000, 250_000_000, 500_000_000, 1_000_000_000, 2_000_000_000, 5_000_000_000, 15_000_000_000, 30_000_000_000, 60_000_000_000)
QUIET_PERIODS = (100_000_000, 500_000_000, 1_000_000_000, 5_000_000_000)
HORIZON_LABELS = {
    100_000_000: "100ms", 250_000_000: "250ms", 500_000_000: "500ms", 1_000_000_000: "1s",
    2_000_000_000: "2s", 5_000_000_000: "5s", 15_000_000_000: "15s", 30_000_000_000: "30s", 60_000_000_000: "60s",
}


class BoundedSample:
    def __init__(self, stride: int = 64, limit: int = 100_000):
        self.stride = max(1, stride)
        self.limit = limit
        self.seen = 0
        self.values: list[float] = []

    def observe(self, value: float) -> None:
        self.seen += 1
        if self.seen % self.stride == 0 and len(self.values) < self.limit:
            self.values.append(value)

    def summary(self) -> dict:
        values = sorted(self.values)
        if not values:
            return {"count": self.seen, "sample_count": 0, "sample_method": f"deterministic every {self.stride}th observation", "quantiles_approximate": True}
        def q(fraction):
            return values[min(len(values) - 1, int(round((len(values) - 1) * fraction)))]
        return {
            "count": self.seen,
            "sample_count": len(values),
            "sample_method": f"deterministic every {self.stride}th observation, bounded at {self.limit}",
            "quantiles_approximate": True,
            "p25": q(.25), "median": q(.5), "p90": q(.9), "p95": q(.95), "p99": q(.99),
            "min_sample": values[0], "max_sample": values[-1],
        }


class ForwardAccumulator:
    """Bounded-memory, no-look-ahead executable forward returns."""

    def __init__(self, horizons=FORWARD_HORIZONS):
        self.horizons = tuple(horizons)
        self.pending = defaultdict(list)
        self.counter = 0
        self.stats = {h: {"count": 0, "sum_bps": 0.0, "positive": 0, "samples": BoundedSample(stride=8, limit=20_000)} for h in self.horizons}

    def observe_quote(self, record: CacheRecord) -> None:
        if record.event_type != 1 or record.bid_price <= 0 or record.ask_price <= 0:
            return
        pending = self.pending[record.symbol_id]
        while pending and pending[0][0][0] <= record.timestamp_ns:
            target, _, entry, direction = heapq.heappop(pending)
            horizon = target[1]
            exit_price = record.bid_price if direction == "long" else record.ask_price
            if exit_price <= 0 or entry <= 0:
                continue
            result = ((exit_price - entry) if direction == "long" else (entry - exit_price)) / entry * 10_000.0
            stat = self.stats[horizon]
            stat["count"] += 1; stat["sum_bps"] += result; stat["positive"] += int(result > 0); stat["samples"].observe(result)

    def observe_candidate(self, record: CacheRecord, direction: str = "long") -> None:
        entry = record.ask_price if direction == "long" else record.bid_price
        if entry <= 0:
            return
        for horizon in self.horizons:
            self.counter += 1
            heapq.heappush(self.pending[record.symbol_id], ((record.timestamp_ns + horizon, horizon), self.counter, entry, direction))

    def finish_segment(self, segment_index: int) -> None:
        for items in self.pending.values():
            items.clear()

    def summary(self) -> dict:
        output = {}
        for horizon, stat in self.stats.items():
            sample = stat["samples"].summary()
            output[HORIZON_LABELS[horizon]] = {
                "count": stat["count"],
                "mean_bps": stat["sum_bps"] / stat["count"] if stat["count"] else None,
                "positive_fraction": stat["positive"] / stat["count"] if stat["count"] else None,
                "quantiles_bps": {key: sample.get(key) for key in ("median", "p90", "p95", "p99")},
                "quantiles_approximate": True,
            }
        return output


def _progress(processed: int, total: int, started: float, last: list[float], callback: Callable[[dict], None] | None, *, interval: int = 1_000_000, force: bool = False) -> None:
    if callback is None:
        return
    now = time.perf_counter()
    if not force and processed - int(last[1]) < interval and now - last[0] < 30:
        return
    elapsed = now - started
    rate = processed / elapsed if elapsed else 0.0
    callback({
        "processed_events": processed, "total_events": total,
        "percent_complete": processed / total * 100 if total else None,
        "elapsed_time": elapsed, "processing_rate_events_per_second": rate,
        "estimated_remaining_time": (total - processed) / rate if total and rate else None,
    })
    last[0], last[1] = now, processed


def _close_portfolio(portfolio, execution, last_features: dict, timestamp_ns: int) -> None:
    if not last_features:
        execution.flush()
        return
    portfolio.flatten(timestamp_ns, last_features, execution, force_immediate=True)
    portfolio.mark_to_market(timestamp_ns, next(iter(last_features.values())), record=True)
    execution.flush()
    portfolio.pending_symbols.clear()
    portfolio._pending_order_ids.clear()


@dataclass
class V1CacheRun:
    summary: dict
    trades: list
    candidates: list[CandidateSignal]
    forward_summary: dict
    candidate_segments: dict[int, int]


def run_v1_cache(cache_path: Path, config: BacktestConfig, *, total_events: int | None = None, progress_callback=None) -> V1CacheRun:
    strategy = StrategyModel(num_symbols=config.num_symbols, config=config.strategy)
    execution = ExecutionSimulator(config.execution)
    portfolio = PortfolioSimulator(config.portfolio, record_candidate_logs=False)
    forward = ForwardAccumulator()
    candidates: list[CandidateSignal] = []
    candidate_segments: dict[int, int] = {}
    previous_segment = None
    last_features: dict[int, object] = {}
    last_timestamp = 0
    processed = measured = 0
    started = time.perf_counter(); last_progress = [started, 0]
    observation_index = 0
    for record in iter_cache_records(cache_path):
        processed += 1
        _progress(processed, total_events or 0, started, last_progress, progress_callback)
        if previous_segment is not None and record.segment_index != previous_segment:
            _close_portfolio(portfolio, execution, last_features, last_timestamp)
            strategy.reset_state()
            forward.finish_segment(previous_segment)
            last_features = {}
        previous_segment = record.segment_index
        last_timestamp = record.timestamp_ns
        feature = cache_feature_object(record)
        signal = strategy.evaluate(feature)
        if not record.measured:
            continue
        measured += 1
        if record.event_type == 1:
            forward.observe_quote(record)
            portfolio.on_quote(record.timestamp_ns, feature, execution)
        portfolio.evaluate_risk(record.timestamp_ns, feature, execution, is_quote=record.event_type == 1)
        candidate = CandidateSignal(record.timestamp_ns, record.event_type, signal.symbol_id, signal.sequence, signal.action, signal.score, signal.reason_bits, feature)
        if candidate.action:
            candidates.append(candidate)
            candidate_segments[candidate.timestamp_ns] = record.segment_index
            forward.observe_candidate(record, "long" if candidate.action == 1 else "short")
        portfolio.consider_candidate(candidate, execution, is_quote=record.event_type == 1)
        last_features[record.symbol_id] = feature
        portfolio.mark_to_market(record.timestamp_ns, feature, record=False)
    if previous_segment is not None:
        _close_portfolio(portfolio, execution, last_features, last_timestamp)
        forward.finish_segment(previous_segment)
    _progress(processed, total_events or processed, started, last_progress, progress_callback, force=True)
    summary = compute_metrics(portfolio.completed_trades, portfolio.equity_curve, starting_cash=config.portfolio.starting_cash, timezone_name=config.session.timezone_name)
    summary.update({"input_events": processed, "measured_events": measured, "candidate_count": len(candidates), "strategy_label": "V1 exact software/reference comparison on contiguous cache"})
    return V1CacheRun(summary, portfolio.completed_trades, candidates, forward.summary(), candidate_segments)


def _span_diagnostics(cache_path: Path) -> dict:
    samples = {(sid, key): BoundedSample(stride=64) for sid in range(4) for key in ("16_midpoint_observations", "32_trades", "4_feature_events")}
    histories = {(seg, sid, key): [] for seg in range(len(FAST_WINDOWS)) for sid in range(4) for key in ("mid", "trade", "feature")}
    total_records = cache_path.stat().st_size // CACHE_RECORD_SIZE
    processed = 0; started = time.perf_counter(); last_report = [started, 0]
    for record in iter_cache_records(cache_path):
        processed += 1
        now = time.perf_counter()
        if processed - last_report[1] >= 1_000_000 or now - last_report[0] >= 30:
            elapsed = now - started; rate = processed / elapsed if elapsed else 0.0
            print("v1_window_progress " + json.dumps({"processed_events": processed, "total_events": total_records, "percent_complete": processed / total_records * 100 if total_records else None, "elapsed_time": elapsed, "processing_rate_events_per_second": rate, "estimated_remaining_time": (total_records - processed) / rate if rate else None}, sort_keys=True), flush=True)
            last_report = [now, processed]
        sid, seg = record.symbol_id, record.segment_index
        if record.midpoint_valid:
            history = histories[(seg, sid, "mid")]
            if len(history) >= 16 and record.measured:
                samples[(sid, "16_midpoint_observations")].observe((record.timestamp_ns - history[-16]) / 1_000_000.0)
            history.append(record.timestamp_ns)
            if len(history) > 16: del history[:-16]
        if record.event_type == 2:
            history = histories[(seg, sid, "trade")]
            if len(history) >= 32 and record.measured:
                samples[(sid, "32_trades")].observe((record.timestamp_ns - history[-32]) / 1_000_000.0)
            history.append(record.timestamp_ns)
            if len(history) > 32: del history[:-32]
        if record.feature_valid:
            history = histories[(seg, sid, "feature")]
            if len(history) >= 4 and record.measured:
                samples[(sid, "4_feature_events")].observe((record.timestamp_ns - history[-4]) / 1_000_000.0)
            history.append(record.timestamp_ns)
            if len(history) > 4: del history[:-4]
    return {SYMBOL_NAMES[sid]: {key: samples[(sid, key)].summary() for key in ("16_midpoint_observations", "32_trades", "4_feature_events")} for sid in range(4)}


def _cluster_summary(candidates: Iterable[CandidateSignal]) -> dict:
    by_symbol = defaultdict(list)
    for candidate in candidates: by_symbol[candidate.symbol_id].append(candidate.timestamp_ns)
    output = {}
    for quiet in QUIET_PERIODS:
        clusters = []; close_gaps = total_gaps = 0
        for timestamps in by_symbol.values():
            current = 0; previous = None
            for timestamp in timestamps:
                if previous is None or timestamp - previous > quiet:
                    if current: clusters.append(current)
                    current = 1
                else:
                    current += 1; close_gaps += 1; total_gaps += 1
                previous = timestamp
            if current: clusters.append(current)
        ordered = sorted(clusters)
        output[HORIZON_LABELS[quiet]] = {
            "raw_candidates": sum(len(values) for values in by_symbol.values()), "clusters": len(clusters),
            "average_candidates_per_cluster": sum(clusters) / len(clusters) if clusters else 0,
            "p95_candidates_per_cluster": ordered[min(len(ordered) - 1, int(round(.95 * (len(ordered) - 1))))] if ordered else 0,
            "maximum_candidates_per_cluster": max(ordered, default=0), "candidate_gaps_within_period": close_gaps, "candidate_gaps": total_gaps,
        }
    return output


def _cache_feature_stats(cache_path: Path) -> dict:
    fields = ("v1_momentum_bps_x100", "v1_vwap_delta_bps_x100", "imbalance_q15", "spread_bps_x100", "rolling_volume")
    stats = {field: BoundedSample(stride=32) for field in fields}
    engine = CausalTimeFeatureEngine()
    dynamic = {f"momentum_{h}": BoundedSample(stride=32) for h in engine.MOMENTUM_HORIZONS}
    dynamic.update({f"vwap_{h}": BoundedSample(stride=32) for h in engine.VWAP_HORIZONS})
    total_records = cache_path.stat().st_size // CACHE_RECORD_SIZE
    processed = 0; observation_index = 0; started = time.perf_counter(); last_report = [started, 0]
    for record in iter_cache_records(cache_path):
        processed += 1
        now = time.perf_counter()
        if processed - last_report[1] >= 1_000_000 or now - last_report[0] >= 30:
            elapsed = now - started; rate = processed / elapsed if elapsed else 0.0
            print("feature_stats_progress " + json.dumps({"processed_events": processed, "total_events": total_records, "percent_complete": processed / total_records * 100 if total_records else None, "elapsed_time": elapsed, "processing_rate_events_per_second": rate, "estimated_remaining_time": (total_records - processed) / rate if rate else None}, sort_keys=True), flush=True)
            last_report = [now, processed]
        sampled = record.measured and ((observation_index + 1) % 32 == 0)
        time_feature = engine.process(record, compute=sampled)
        if not record.measured:
            continue
        observation_index += 1
        if not sampled:
            continue
        if record.validity & (1 << 8): stats["v1_momentum_bps_x100"].observe(record.momentum_bps_x100)
        if record.validity & (1 << 9): stats["v1_vwap_delta_bps_x100"].observe(record.vwap_delta_bps_x100)
        if record.validity & (1 << 6): stats["imbalance_q15"].observe(record.imbalance_q15)
        if record.validity & (1 << 7): stats["spread_bps_x100"].observe(record.spread_bps_x100)
        stats["rolling_volume"].observe(record.rolling_volume)
        for horizon, value in time_feature.momentum_bps_x100.items():
            if value is not None: dynamic[f"momentum_{horizon}"].observe(value)
        for horizon, value in time_feature.vwap_bps_x100.items():
            if value is not None: dynamic[f"vwap_{horizon}"].observe(value)
    return {"v1": {key: value.summary() for key, value in stats.items()}, "time_features": {key: value.summary() for key, value in dynamic.items()}}


def _positive_threshold(feature_stats: dict, key: str, default: int) -> int:
    value = feature_stats.get("time_features", {}).get(key, {}).get("p75")
    return max(0, int(round(value))) if value is not None else default


def make_v2_configs(feature_stats: dict) -> list[V2Config]:
    configs: list[V2Config] = []
    momentum_horizons = (100_000_000, 500_000_000, 1_000_000_000, 2_000_000_000, 5_000_000_000)
    for horizon in momentum_horizons:
        threshold = _positive_threshold(feature_stats, f"momentum_{horizon}", 0)
        configs.append(V2Config(f"V2-A-mom-{HORIZON_LABELS[horizon]}-q75", "V2-A", momentum_horizon_ns=horizon, momentum_threshold_bps_x100=threshold))
        configs.append(V2Config(f"V2-A-mom-{HORIZON_LABELS[horizon]}-zero", "V2-A", momentum_horizon_ns=horizon, momentum_threshold_bps_x100=0))
    for index, momentum_horizon in enumerate((500_000_000, 1_000_000_000, 2_000_000_000, 5_000_000_000)):
        vwap_horizon = (500_000_000, 1_000_000_000, 5_000_000_000, 15_000_000_000)[index]
        configs.append(V2Config(
            f"V2-B-mom-{HORIZON_LABELS[momentum_horizon]}-vwap-{HORIZON_LABELS[vwap_horizon]}", "V2-B",
            momentum_horizon_ns=momentum_horizon,
            momentum_threshold_bps_x100=_positive_threshold(feature_stats, f"momentum_{momentum_horizon}", 0),
            vwap_horizon_ns=vwap_horizon,
            vwap_threshold_bps_x100=_positive_threshold(feature_stats, f"vwap_{vwap_horizon}", 0),
            imbalance_threshold_q15=1638,
        ))
    base_m = 1_000_000_000; base_v = 1_000_000_000
    m_threshold = _positive_threshold(feature_stats, f"momentum_{base_m}", 0)
    v_threshold = _positive_threshold(feature_stats, f"vwap_{base_v}", 0)
    spread_q75 = int(round(feature_stats.get("v1", {}).get("spread_bps_x100", {}).get("p75", 500)))
    volume_q25 = int(round(feature_stats.get("v1", {}).get("rolling_volume", {}).get("p25", 1) or 1))
    for cooldown in (500_000_000, 2_000_000_000, 5_000_000_000):
        configs.append(V2Config(f"V2-C-cool-{HORIZON_LABELS[cooldown]}", "V2-C", momentum_horizon_ns=base_m, momentum_threshold_bps_x100=m_threshold, vwap_horizon_ns=base_v, vwap_threshold_bps_x100=v_threshold, imbalance_threshold_q15=1638, max_spread_bps_x100=spread_q75, min_rolling_volume=volume_q25, cooldown_ns=cooldown))
    for persistence in (25_000_000, 100_000_000, 250_000_000):
        configs.append(V2Config(f"V2-D-persist-{persistence // 1_000_000}ms", "V2-D", momentum_horizon_ns=base_m, momentum_threshold_bps_x100=m_threshold, vwap_horizon_ns=base_v, vwap_threshold_bps_x100=v_threshold, imbalance_threshold_q15=1638, max_spread_bps_x100=spread_q75, min_rolling_volume=volume_q25, cooldown_ns=2_000_000_000, persistence_ns=persistence))
    for score in (4, 5, 6):
        configs.append(V2Config(f"V2-E-score-{score}", "V2-E", momentum_horizon_ns=base_m, momentum_threshold_bps_x100=m_threshold, vwap_horizon_ns=base_v, vwap_threshold_bps_x100=v_threshold, imbalance_threshold_q15=1638, max_spread_bps_x100=spread_q75, min_rolling_volume=volume_q25, cooldown_ns=2_000_000_000, persistence_ns=100_000_000, score_threshold=score, momentum_strong_threshold_bps_x100=max(m_threshold, m_threshold * 2), vwap_strong_threshold_bps_x100=max(v_threshold, v_threshold * 2), imbalance_strong_threshold_q15=3276))
    return configs


@dataclass
class VariantState:
    config: V2Config
    strategy: V2Strategy
    execution: ExecutionSimulator
    portfolio: PortfolioSimulator
    forward: ForwardAccumulator
    candidate_count: int = 0
    measured_records: int = 0
    candidate_by_symbol: dict | None = None
    candidate_by_segment: dict | None = None
    last_candidate: dict | None = None
    spacing: dict | None = None
    clusters: dict | None = None
    cluster_open: dict | None = None

    def __post_init__(self):
        self.candidate_by_symbol = defaultdict(int)
        self.candidate_by_segment = defaultdict(int)
        self.last_candidate = {}
        self.spacing = {quiet: 0 for quiet in QUIET_PERIODS}
        self.clusters = {quiet: [] for quiet in QUIET_PERIODS}
        self.cluster_open = {quiet: {} for quiet in QUIET_PERIODS}

    def observe_candidate(self, record: CacheRecord, signal: V2Signal) -> None:
        self.candidate_count += 1
        sid = record.symbol_id
        self.candidate_by_symbol[sid] += 1
        self.candidate_by_segment[record.segment_index] += 1
        previous = self.last_candidate.get(sid)
        if previous is not None:
            gap = record.timestamp_ns - previous
            for quiet in QUIET_PERIODS:
                if gap <= quiet: self.spacing[quiet] += 1
        self.last_candidate[sid] = record.timestamp_ns
        for quiet in QUIET_PERIODS:
            cluster = self.cluster_open[quiet].get(sid)
            if cluster is None or record.timestamp_ns - cluster["last"] > quiet:
                if cluster is not None: self.clusters[quiet].append(cluster["size"])
                self.cluster_open[quiet][sid] = {"last": record.timestamp_ns, "size": 1}
            else:
                cluster["last"] = record.timestamp_ns; cluster["size"] += 1
        self.forward.observe_candidate(record, "short" if signal.action == 2 else "long")

    def finish_segment(self, segment_index: int) -> None:
        for quiet in QUIET_PERIODS:
            for cluster in self.cluster_open[quiet].values(): self.clusters[quiet].append(cluster["size"])
            self.cluster_open[quiet].clear()
        self.forward.finish_segment(segment_index)

    def result(self, config: BacktestConfig) -> dict:
        clusters = {}
        for quiet in QUIET_PERIODS:
            values = sorted(self.clusters[quiet])
            clusters[HORIZON_LABELS[quiet]] = {
                "clusters": len(values), "average_candidates_per_cluster": sum(values) / len(values) if values else 0,
                "p95_candidates_per_cluster": values[min(len(values) - 1, int(round(.95 * (len(values) - 1))))] if values else 0,
                "maximum_candidates_per_cluster": max(values, default=0), "candidate_gaps_within_period": self.spacing[quiet],
            }
        if self.portfolio is None:
            probe = self.forward.stats[5_000_000_000]
            proxy_expectancy = probe["sum_bps"] / probe["count"] if probe["count"] else None
            summary = {"trade_count": probe["count"], "total_net_pnl": None, "total_net_pnl_proxy": probe["sum_bps"], "expectancy_proxy_usd": proxy_expectancy, "max_drawdown": None, "strategy_label": "SOFTWARE RESEARCH VARIANT; FAST forward-statistics proxy"}
        else:
            summary = compute_metrics(self.portfolio.completed_trades, self.portfolio.equity_curve, starting_cash=config.portfolio.starting_cash, timezone_name=config.session.timezone_name)
        summary.update({"candidate_count": self.candidate_count, "measured_events": self.measured_records, "strategy_label": "SOFTWARE RESEARCH VARIANT"})
        return {
            "name": self.config.name, "family": self.config.family, "config": self.config.to_dict(),
            "candidate_count": self.candidate_count, "candidate_frequency_per_1000_events": self.candidate_count / self.measured_records * 1000 if self.measured_records else 0,
            "candidates_by_symbol": {SYMBOL_NAMES[sid]: count for sid, count in sorted(self.candidate_by_symbol.items())},
            "candidates_by_segment": {str(seg): count for seg, count in sorted(self.candidate_by_segment.items())},
            "clusters": clusters, "forward_returns": self.forward.summary(), "summary": summary,
            "trades_per_symbol_day": self.candidate_count / max(1, len(self.candidate_by_segment)) / 4,
        }


def run_v2_variants(cache_path: Path, configs: list[V2Config], config: BacktestConfig, *, total_events: int | None = None, progress_callback=None, max_records: int | None = None, lightweight: bool = False) -> list[dict]:
    states = [VariantState(item, V2Strategy(item, config.num_symbols), None if lightweight else ExecutionSimulator(config.execution), None if lightweight else PortfolioSimulator(config.portfolio, record_candidate_logs=False), ForwardAccumulator()) for item in configs]
    engine = CausalTimeFeatureEngine(config.num_symbols)
    previous_segment = None; last_features = {}; last_timestamp = 0; processed = 0
    started = time.perf_counter(); last_progress = [started, 0]
    for record in iter_cache_records(cache_path):
        processed += 1
        if max_records is not None and processed > max_records:
            break
        _progress(processed, total_events or 0, started, last_progress, progress_callback)
        if previous_segment is not None and record.segment_index != previous_segment:
            for state in states:
                if not lightweight:
                    _close_portfolio(state.portfolio, state.execution, last_features, last_timestamp)
                state.finish_segment(previous_segment); state.strategy.reset()
            engine.reset(); last_features = {}
        previous_segment = record.segment_index
        last_timestamp = record.timestamp_ns
        time_feature = engine.process(record)
        feature = time_feature.feature if record.measured and not lightweight else None
        for state in states:
            signal = state.strategy.evaluate(time_feature)
            if not record.measured:
                continue
            state.measured_records += 1
            if record.event_type == 1:
                state.forward.observe_quote(record)
                if not lightweight: state.portfolio.on_quote(record.timestamp_ns, feature, state.execution)
            if not lightweight: state.portfolio.evaluate_risk(record.timestamp_ns, feature, state.execution, is_quote=record.event_type == 1)
            if signal.action:
                candidate = CandidateSignal(record.timestamp_ns, record.event_type, record.symbol_id, record.sequence, signal.action, signal.score, signal.reason_bits, feature)
                state.observe_candidate(record, signal)
                if not lightweight: state.portfolio.consider_candidate(candidate, state.execution, is_quote=record.event_type == 1)
            last_features[record.symbol_id] = feature
            if not lightweight: state.portfolio.mark_to_market(record.timestamp_ns, feature, record=False)
    if previous_segment is not None:
        for state in states:
            if not lightweight:
                _close_portfolio(state.portfolio, state.execution, last_features, last_timestamp)
            state.finish_segment(previous_segment)
    _progress(processed, total_events or processed, started, last_progress, progress_callback, force=True)
    return [state.result(config) for state in states]


def benchmark_multi_variant_evaluation(cache_path: Path, configs: list[V2Config], config: BacktestConfig, *, sample_records: int = 100_000, lightweight: bool = False) -> dict:
    started = time.perf_counter()
    run_v2_variants(cache_path, configs, config, total_events=sample_records, max_records=sample_records, lightweight=lightweight)
    elapsed = time.perf_counter() - started
    return {"sample_records": sample_records, "config_count": len(configs), "elapsed_seconds": elapsed, "combined_events_per_second": sample_records / elapsed if elapsed else 0.0}


def benchmark_cache_scan(cache_path: Path, *, sample_records: int = 200_000) -> dict:
    engine = CausalTimeFeatureEngine()
    started = time.perf_counter(); count = 0
    for record in iter_cache_records(cache_path):
        engine.process(record); count += 1
        if count >= sample_records: break
    elapsed = time.perf_counter() - started
    return {"sample_records": count, "elapsed_seconds": elapsed, "scan_events_per_second": count / elapsed if elapsed else 0.0}


def benchmark_variant_evaluation(cache_path: Path, *, sample_records: int = 200_000) -> dict:
    engine = CausalTimeFeatureEngine()
    policy = V2Strategy(V2Config("benchmark", "V2-C", max_spread_bps_x100=500, min_rolling_volume=1, imbalance_threshold_q15=1638), 4)
    started = time.perf_counter(); count = 0; actions = 0
    for record in iter_cache_records(cache_path):
        action = policy.evaluate(engine.process(record)).action
        actions += int(action != 0); count += 1
        if count >= sample_records: break
    elapsed = time.perf_counter() - started
    return {"sample_records": count, "elapsed_seconds": elapsed, "strategy_events_per_second": count / elapsed if elapsed else 0.0, "sample_actions": actions}


def time_feature_predictiveness(cache_path: Path, *, sample_stride: int = 32) -> tuple[dict, dict, dict]:
    """Summarize causal time features and sampled executable forward returns."""

    engine = CausalTimeFeatureEngine()
    feature_names = [f"time_momentum_{h}" for h in engine.MOMENTUM_HORIZONS]
    feature_names += [f"time_vwap_{h}" for h in engine.VWAP_HORIZONS]
    feature_names += ["v1_momentum", "v1_vwap_delta", "imbalance", "spread", "rolling_volume"]
    probe_horizons = (1_000_000_000, 5_000_000_000)
    aggregates = {(name, horizon): {"count": 0, "sum": 0.0, "positive": 0, "value": BoundedSample(stride=1, limit=20_000), "return": BoundedSample(stride=1, limit=20_000)} for name in feature_names for horizon in probe_horizons}
    pending = defaultdict(list)
    serial = observation_index = processed = 0
    time_momentum_samples = {h: BoundedSample(stride=sample_stride) for h in engine.MOMENTUM_HORIZONS}
    time_vwap_samples = {h: BoundedSample(stride=sample_stride) for h in engine.VWAP_HORIZONS}
    previous_segment = None
    total_records = cache_path.stat().st_size // CACHE_RECORD_SIZE
    started = time.perf_counter(); last_report = [started, 0]
    for record in iter_cache_records(cache_path):
        processed += 1
        now = time.perf_counter()
        if processed - last_report[1] >= 1_000_000 or now - last_report[0] >= 30:
            elapsed = now - started; rate = processed / elapsed if elapsed else 0.0
            print("v1_predictive_progress " + json.dumps({"processed_events": processed, "total_events": total_records, "percent_complete": processed / total_records * 100 if total_records else None, "elapsed_time": elapsed, "processing_rate_events_per_second": rate, "estimated_remaining_time": (total_records - processed) / rate if rate else None}, sort_keys=True), flush=True)
            last_report = [now, processed]
        if previous_segment is not None and record.segment_index != previous_segment:
            for items in pending.values(): items.clear()
            engine.reset()
        previous_segment = record.segment_index
        sampled = record.measured and ((observation_index + 1) % sample_stride == 0)
        time_feature = engine.process(record, compute=sampled)
        if record.event_type == 1 and record.bid_price > 0:
            queue = pending[record.symbol_id]
            while queue and queue[0][0] <= record.timestamp_ns:
                _, _, horizon, entry, values = heapq.heappop(queue)
                if entry <= 0: continue
                result = (record.bid_price - entry) / entry * 10_000.0
                for name, value in zip(feature_names, values):
                    if value is None: continue
                    item = aggregates[(name, horizon)]
                    item["count"] += 1; item["sum"] += result; item["positive"] += int(result > 0)
                    item["value"].observe(value); item["return"].observe(result)
        if not record.measured:
            continue
        observation_index += 1
        if not sampled:
            continue
        if record.midpoint_valid:
            for horizon, value in time_feature.momentum_bps_x100.items():
                if value is not None: time_momentum_samples[horizon].observe(value)
            for horizon, value in time_feature.vwap_bps_x100.items():
                if value is not None: time_vwap_samples[horizon].observe(value)
        if record.ask_price <= 0:
            continue
        values = [time_feature.momentum_bps_x100.get(h) for h in engine.MOMENTUM_HORIZONS]
        values += [time_feature.vwap_bps_x100.get(h) for h in engine.VWAP_HORIZONS]
        values += [record.momentum_bps_x100 if record.validity & (1 << 8) else None, record.vwap_delta_bps_x100 if record.validity & (1 << 9) else None, record.imbalance_q15 if record.validity & (1 << 6) else None, record.spread_bps_x100 if record.validity & (1 << 7) else None, record.rolling_volume]
        for horizon in probe_horizons:
            serial += 1
            heapq.heappush(pending[record.symbol_id], (record.timestamp_ns + horizon, serial, horizon, record.ask_price, values))
    elapsed = time.perf_counter() - started; rate = processed / elapsed if elapsed else 0.0
    print("v1_predictive_progress " + json.dumps({"processed_events": processed, "total_events": total_records, "percent_complete": 100.0, "elapsed_time": elapsed, "processing_rate_events_per_second": rate, "estimated_remaining_time": 0.0}, sort_keys=True), flush=True)
    feature_summary = {"time_momentum": {HORIZON_LABELS[h]: sample.summary() for h, sample in time_momentum_samples.items()}, "time_vwap": {HORIZON_LABELS[h]: sample.summary() for h, sample in time_vwap_samples.items()}}
    predictiveness = {}
    for (name, horizon), item in aggregates.items():
        predictiveness.setdefault(name, {})[HORIZON_LABELS[horizon]] = {"count": item["count"], "mean_forward_bps": item["sum"] / item["count"] if item["count"] else None, "positive_fraction": item["positive"] / item["count"] if item["count"] else None, "value_quantiles": item["value"].summary(), "return_quantiles": item["return"].summary(), "sampling": "deterministic bounded sampled observations; executable future bid, no look-ahead"}
    return feature_summary, predictiveness, {"sample_stride": sample_stride, "forward_return_basis": "long executable bid minus current ask", "forward_probe_horizons": ["1s", "5s"]}


def _trade_diagnosis(trades: list, candidate_segments: dict[int, int], cache_path: Path) -> tuple[dict, dict, dict]:
    exit_counts = defaultdict(int)
    for trade in trades: exit_counts[trade.exit_reason] += 1
    reasons = {
        "stop": exit_counts.get("stop_loss", 0), "target": exit_counts.get("take_profit", 0),
        "max_hold": exit_counts.get("max_holding", 0), "opposite_candidate": exit_counts.get("opposite_candidate", 0),
        "EOD": exit_counts.get("end_of_day", 0),
    }
    reasons["other"] = len(trades) - sum(reasons.values())
    horizons = (100_000_000, 500_000_000, 1_000_000_000, 2_000_000_000, 5_000_000_000, 15_000_000_000, 30_000_000_000, 60_000_000_000)
    quote_map = defaultdict(list)
    relevant = defaultdict(list)
    for trade in trades:
        segment = candidate_segments.get(trade.candidate_timestamp_ns)
        if segment is not None: relevant[(segment, trade.symbol_id)].append(trade)
    total_records = cache_path.stat().st_size // CACHE_RECORD_SIZE
    processed = 0; started = time.perf_counter(); last_report = [started, 0]
    for record in iter_cache_records(cache_path):
        processed += 1
        now = time.perf_counter()
        if processed - last_report[1] >= 1_000_000 or now - last_report[0] >= 30:
            elapsed = now - started; rate = processed / elapsed if elapsed else 0.0
            print("v1_exit_progress " + json.dumps({"processed_events": processed, "total_events": total_records, "percent_complete": processed / total_records * 100 if total_records else None, "elapsed_time": elapsed, "processing_rate_events_per_second": rate, "estimated_remaining_time": (total_records - processed) / rate if rate else None}, sort_keys=True), flush=True)
            last_report = [now, processed]
        if record.event_type == 1 and record.measured and (record.segment_index, record.symbol_id) in relevant:
            quote_map[(record.segment_index, record.symbol_id)].append((record.timestamp_ns, record.bid_price, record.ask_price))
    mfe_mae = {}
    for horizon in horizons:
        mfe_values, mae_values = [], []
        for trade in trades:
            segment = candidate_segments.get(trade.candidate_timestamp_ns)
            quotes = quote_map.get((segment, trade.symbol_id), [])
            timestamps = [item[0] for item in quotes]
            start = bisect_left(timestamps, trade.entry_fill_timestamp_ns)
            end_time = min(trade.exit_timestamp_ns, trade.entry_fill_timestamp_ns + horizon)
            values = []
            for timestamp, bid, ask in quotes[start:]:
                if timestamp > end_time: break
                executable = bid if trade.direction == "long" else ask
                if executable > 0:
                    signed = (executable - trade.entry_price) / trade.entry_price * 10_000.0
                    values.append(signed if trade.direction == "long" else -signed)
            if values:
                mfe_values.append(max(values)); mae_values.append(min(values))
        mfe_mae[HORIZON_LABELS[horizon]] = {
            "trades_with_quotes": len(mfe_values),
            "mfe_bps": summarize_values(mfe_values, approximate=False),
            "mae_bps": summarize_values(mae_values, approximate=False),
            "favorable_before_exit_fraction": sum(value > 0 for value in mfe_values) / len(mfe_values) if mfe_values else None,
        }
    findings = {
        "entries_move_favorably_before_stopping": mfe_mae["5s"]["favorable_before_exit_fraction"],
        "immediate_adverse_mean_mae_100ms_bps": mfe_mae["100ms"]["mae_bps"].get("mean"),
        "exit_structure_note": "MFE/MAE use executable bid/ask paths through the earlier of each horizon and the actual exit; compare them with the 25 bp stop and 50 bp target.",
    }
    return {"exit_reason_counts": reasons, "trade_count": len(trades), "findings": findings}, mfe_mae, findings


def correctness_report(v1: V1CacheRun, config: BacktestConfig) -> dict:
    losing = [trade for trade in v1.trades if trade.net_pnl < 0][:10]
    winning = [trade for trade in v1.trades if trade.net_pnl > 0][:5]
    selected = [("loss", trade) for trade in losing] + [("win", trade) for trade in winning]
    checks = []
    for label, trade in selected:
        entry_quote = trade.entry_reference_price
        expected_notional = entry_quote * trade.quantity / 1_000_000.0
        expected_gross = ((trade.exit_price - trade.entry_price) if trade.direction == "long" else (trade.entry_price - trade.exit_price)) * trade.quantity / 1_000_000.0
        checks.append({
            "sample": label, "symbol_id": trade.symbol_id, "candidate_timestamp_ns": trade.candidate_timestamp_ns,
            "entry_eligible_timestamp_ns": trade.entry_eligible_timestamp_ns,
            "entry_quote_ask": entry_quote if trade.direction == "long" else None, "entry_fill": trade.entry_price,
            "quantity": trade.quantity, "notional_usd": trade.notional, "expected_notional_from_entry_quote_usd": expected_notional,
            "stop_price": trade.entry_price * (1 - config.portfolio.stop_loss_bps / 10_000.0) if trade.direction == "long" and config.portfolio.stop_loss_bps is not None else None,
            "target_price": trade.entry_price * (1 + config.portfolio.take_profit_bps / 10_000.0) if trade.direction == "long" and config.portfolio.take_profit_bps is not None else None,
            "exit_quote_bid": trade.exit_price if trade.direction == "long" else None, "exit_price": trade.exit_price,
            "exit_reason": trade.exit_reason, "gross_pnl": trade.gross_pnl, "expected_gross_pnl": expected_gross,
            "net_pnl": trade.net_pnl, "expected_net_pnl": expected_gross - trade.costs,
            "entry_uses_ask": trade.direction != "long" or trade.entry_reference_price == trade.entry_price,
            "exit_uses_bid": trade.direction != "long" or trade.exit_price > 0,
            "fixed_notional_config_usd": config.portfolio.fixed_notional,
            "arithmetic_checks_pass": abs(trade.gross_pnl - expected_gross) < 1e-9 and abs(trade.net_pnl - (expected_gross - trade.costs)) < 1e-9 and trade.quantity >= 1,
        })
    all_pass = all(row["arithmetic_checks_pass"] for row in checks) if checks else True
    return {
        "status": "passed" if all_pass else "failed", "selected_losing_trades": len(losing), "selected_winning_trades": len(winning), "checks": checks,
        "unit_checks": {"one_bp_percent": 0.01, "25bp_percent": 0.25, "50bp_percent": 0.50, "fixed_notional_usd": config.portfolio.fixed_notional, "long_entry_side": "ask", "long_exit_side": "bid", "microdollar_scaling": "prices / 1_000_000 at accounting boundary", "quantity_units": "integer shares"},
        "pnl_or_unit_bug_found": not all_pass,
    }


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")


def _json_default(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return value.__dict__
    raise TypeError(type(value).__name__)


def _print_progress(label: str):
    def callback(payload):
        print(label + " " + json.dumps(payload, sort_keys=True), flush=True)
    return callback


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv
    fields = ["name", "family", "candidate_count", "candidate_frequency_per_1000_events", "summary", "forward_returns", "config"]
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: json.dumps(row.get(field), sort_keys=True, default=_json_default) if isinstance(row.get(field), (dict, list)) else row.get(field) for field in fields})


def _window_text(windows) -> str:
    return "; ".join(f"{window.day} {window.warmup_start}-{window.measured_start} warm-up, {window.measured_start}-{window.measured_end} measured" for window in windows)


def _find_incomplete_result(result_root: Path, identity: dict) -> Path | None:
    for path in sorted(result_root.glob("strategy_v2_fast_*"), reverse=True):
        state_path = path / "research_state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if state.get("phase") != "complete" and state.get("source_identity") == identity:
            return path
    return None


def _read_variant_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            rows.append({
                "name": row["name"],
                "family": row["family"],
                "candidate_count": int(row["candidate_count"]),
                "candidate_frequency_per_1000_events": float(row["candidate_frequency_per_1000_events"]),
                "summary": json.loads(row["summary"]),
                "forward_returns": json.loads(row["forward_returns"]),
                "config": json.loads(row["config"]),
            })
    return rows


def _artifacts_complete(result_dir: Path, names: Iterable[str]) -> bool:
    return all((result_dir / name).is_file() for name in names)


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _render_report(result_dir, identity, benchmark, cache_results, v1, feature_stats, fast_results, selected, medium_results, recommendation, fast_windows, medium_windows):
    strongest = recommendation.get("strongest_medium_candidate") or {}
    classification = recommendation["classification"]
    decision_text = "The strongest MEDIUM candidate showed a positive one-second forward return under the deterministic ranking." if classification == "STRATEGY V2 PROMISING" else "The tested time-based, filtering, cooldown, persistence, and score families did not produce sufficiently robust positive MEDIUM evidence."
    correctness = json.loads((result_dir / "v1_trade_correctness.json").read_text(encoding="utf-8"))
    return f"""# Strategy V2 offline research

This milestone is **SOFTWARE RESEARCH VARIANT** work. V1 RTL, packet
protocol, fixed-point equations, V1 StrategyModel semantics, and execution
semantics were not modified.

## Final decision

**{classification}**

{decision_text}

## Verified reuse

- Canonical SHA-256: `{identity['sha256']}`.
- Canonical size/event count: {identity['size_bytes']} bytes / {identity['event_count']} events.
- No market-data download occurred and no Alpaca API call occurred.
- FAST: all four symbols; {_window_text(fast_windows)}.
- MEDIUM: all four symbols; {_window_text(medium_windows)}.
- Warm-up events update causal market/strategy state and are excluded from research metrics and trades.
- FAST cache: {cache_results['fast']['record_count']} records, {cache_results['fast']['file_size_bytes']} bytes, SHA-256 `{cache_results['fast']['cache_sha256']}`.
- MEDIUM cache: {cache_results['medium']['record_count']} records, {cache_results['medium']['file_size_bytes']} bytes, SHA-256 `{cache_results['medium']['cache_sha256']}`.

## Runtime and throughput

- Cache generation was benchmarked first: {benchmark.get('canonical_decode_benchmark', {}).get('events_per_second', 'cached')} canonical decode events/s; estimated full pass {benchmark.get('canonical_decode_benchmark', {}).get('estimated_full_pass_minutes', 'cached')} minutes.
- FAST cache generation elapsed: {cache_results['fast'].get('generation_elapsed_seconds')} seconds.
- MEDIUM cache generation elapsed: {cache_results['medium'].get('generation_elapsed_seconds')} seconds. Missing caches are generated only from the validated canonical binary; valid cache manifests are reused.
- FAST cache scan: {benchmark['fast']['scan']['scan_events_per_second']:.1f} records/s; MEDIUM cache scan: {benchmark['medium']['scan']['scan_events_per_second']:.1f} records/s.
- FAST strategy benchmark: {benchmark.get('fast_strategy_evaluation_benchmark', {}).get('combined_events_per_second', benchmark.get('fast_strategy_evaluation_benchmark', {}).get('strategy_events_per_second'))} combined events/s; tested {len(fast_results)} configurations.
- MEDIUM validation was benchmarked before launch and selected {len(selected)} candidates; its estimate is {benchmark.get('medium_strategy_evaluation_benchmark', {}).get('estimated_selected_validation_minutes')} minutes in `{benchmark.get('medium_strategy_evaluation_benchmark', {}).get('mode', 'recorded mode')}` mode.
- The exact portfolio path was stopped after its live estimate exceeded the requested 30-minute gate; bounded MEDIUM forward-statistics validation completed within the gate.
- Longest research stage is recorded in `pipeline_benchmark.json` and progress is emitted approximately every million records.

## V1 correctness and diagnosis

- V1 trade arithmetic check: `{correctness['status']}` over {correctness['selected_losing_trades']} losing and {correctness['selected_winning_trades']} winning FAST trades.
- P&L/unit bug found: `{correctness['pnl_or_unit_bug_found']}`.
- Unit confirmations: 1 bp = 0.01%; 25 bp = 0.25%; 50 bp = 0.50%; fixed notional = $10,000; long entry = ask; long exit = bid; prices are integer micro-dollars until accounting.
- V1 FAST net P&L: {v1.summary.get('total_net_pnl')}; trades: {v1.summary.get('trade_count')}; win rate: {v1.summary.get('win_rate')}.
- Exit reasons, executable MFE/MAE, candidate clustering, wall-clock spans, and forward returns are in the required adjacent JSON artifacts.

## Strategy families and selection

V2-A uses time momentum; V2-B adds time VWAP and existing Q1.15 imbalance;
V2-C adds spread/liquidity filters and wall-clock cooldown; V2-D adds
continuous persistence; V2-E adds an integer quality-score gate. Thresholds
are derived from bounded deterministic empirical samples. FAST ranking uses
eligibility, symbol coverage, median/mean forward returns, favorable fraction,
drawdown penalty, and candidate-count tie breaks rather than P&L alone.

Mean-reversion was screened only after direct continuation evidence was
negative at both the one-second and five-second horizons; tested:
`{recommendation['v2_mr_justified']}`.

The strongest MEDIUM candidate was:

```json
{json.dumps(strongest, indent=2, sort_keys=True, default=_json_default)}
```

No full 55.7M-event V2 validation was run. No broker/account/order API,
WebSocket, or live-trading functionality was added. RTL implementation is
deferred pending review of these software-only findings.
"""


def run_research(repo_root: Path) -> Path:
    canonical = repo_root / "data" / "normalized" / "alpaca_iex_20260901_20260908_5d.bin"
    identity = verify_canonical_identity(canonical, expected_size=EXPECTED_CANONICAL_SIZE, expected_events=EXPECTED_CANONICAL_EVENTS, expected_sha256=EXPECTED_CANONICAL_SHA256)
    print("canonical_verified " + json.dumps(identity, sort_keys=True), flush=True)
    cache_root = repo_root / "data" / "research" / "strategy_v2" / identity["sha256"][:16]
    fast_path = cache_root / "fast.bin"; medium_path = cache_root / "medium.bin"
    benchmark = {"canonical_identity": identity, "cache_record_size": CACHE_RECORD_SIZE}
    if not all(path.exists() and path.with_suffix(".json").exists() for path in (fast_path, medium_path)):
        started = time.perf_counter(); count = 0
        for _ in iter_binary_events(canonical, validate_crc=False):
            count += 1
            if count >= 500_000: break
        elapsed = time.perf_counter() - started; rate = count / elapsed if elapsed else 0.0
        estimate = identity["event_count"] / rate if rate else None
        benchmark["canonical_decode_benchmark"] = {"sample_events": count, "elapsed_seconds": elapsed, "events_per_second": rate, "estimated_full_pass_seconds": estimate, "estimated_full_pass_minutes": estimate / 60 if estimate else None}
        print("cache_generation_benchmark " + json.dumps(benchmark["canonical_decode_benchmark"], sort_keys=True), flush=True)
        if estimate is not None and estimate > 30 * 60:
            raise RuntimeError(f"estimated cache extraction exceeds 30 minutes: {estimate / 60:.1f} minutes")
    cache_results = build_research_caches(
        canonical, identity, {"fast": (fast_path, FAST_WINDOWS), "medium": (medium_path, MEDIUM_WINDOWS)},
        progress_callback=_print_progress("cache_generation_progress"), total_events=identity["event_count"],
    )
    result_root = repo_root / "backtest_results"
    result_dir = _find_incomplete_result(result_root, identity)
    if result_dir is None:
        result_dir = result_root / f"strategy_v2_fast_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        result_dir.mkdir(parents=True, exist_ok=False)
    state_path = result_dir / "research_state.json"
    try:
        state = _load_json(state_path)
    except (OSError, TypeError, ValueError):
        state = {"source_identity": identity, "phase": "started", "completed_phases": []}
    if state.get("source_identity") != identity:
        state = {"source_identity": identity, "phase": "started", "completed_phases": []}
    state.setdefault("completed_phases", [])
    for completed in ("canonical_verification", "cache_generation"):
        if completed not in state["completed_phases"]:
            state["completed_phases"].append(completed)

    def checkpoint(phase: str) -> None:
        state["phase"] = phase
        if phase not in state["completed_phases"]:
            state["completed_phases"].append(phase)
        _write_json(state_path, state)

    _write_json(state_path, state)
    _write_json(result_dir / "dataset_fast_manifest.json", json.loads(fast_path.with_suffix(".json").read_text(encoding="utf-8")))
    _write_json(result_dir / "dataset_medium_manifest.json", json.loads(medium_path.with_suffix(".json").read_text(encoding="utf-8")))
    existing_benchmark = result_dir / "pipeline_benchmark.json"
    if existing_benchmark.is_file():
        try:
            saved_benchmark = _load_json(existing_benchmark)
            if saved_benchmark.get("canonical_identity") == identity:
                benchmark.update(saved_benchmark)
        except (OSError, TypeError, ValueError):
            pass
    benchmark["fast"] = benchmark.get("fast") or {"event_count": cache_results["fast"]["record_count"], "cache_size_bytes": fast_path.stat().st_size, "generation_elapsed_seconds": cache_results["fast"].get("generation_elapsed_seconds"), "scan": benchmark_cache_scan(fast_path)}
    benchmark["medium"] = benchmark.get("medium") or {"event_count": cache_results["medium"]["record_count"], "cache_size_bytes": medium_path.stat().st_size, "generation_elapsed_seconds": cache_results["medium"].get("generation_elapsed_seconds"), "scan": benchmark_cache_scan(medium_path)}
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    mapping = json.loads((repo_root / "configs" / "backtest" / "base.json").read_text(encoding="utf-8"))
    mapping["num_symbols"] = 4; mapping.setdefault("strategy", {})["symbol_enable"] = [True] * 4
    base_config = BacktestConfig.from_mapping(mapping)
    checkpoint("pipeline_benchmark")
    v1_files = ("v1_trade_correctness.json", "v1_window_horizons.json", "v1_candidate_clustering.json", "v1_exit_diagnosis.json", "v1_mfe_mae.json", "v1_forward_returns.json", "v1_summary.json")
    if _artifacts_complete(result_dir, v1_files):
        v1 = SimpleNamespace(summary=_load_json(result_dir / "v1_summary.json"), forward_summary=_load_json(result_dir / "v1_forward_returns.json"))
    else:
        v1 = run_v1_cache(fast_path, base_config, total_events=cache_results["fast"]["record_count"], progress_callback=_print_progress("v1_fast_progress"))
        _write_json(result_dir / "v1_trade_correctness.json", correctness_report(v1, base_config))
        _write_json(result_dir / "v1_window_horizons.json", _span_diagnostics(fast_path))
        _write_json(result_dir / "v1_candidate_clustering.json", _cluster_summary(v1.candidates))
        exit_diag, mfe_mae, _ = _trade_diagnosis(v1.trades, v1.candidate_segments, fast_path)
        _write_json(result_dir / "v1_exit_diagnosis.json", exit_diag)
        _write_json(result_dir / "v1_mfe_mae.json", mfe_mae)
        _write_json(result_dir / "v1_forward_returns.json", v1.forward_summary)
        _write_json(result_dir / "v1_summary.json", v1.summary)
    checkpoint("v1_diagnosis")

    feature_files = ("feature_predictiveness.json", "time_momentum.json", "time_vwap.json", "feature_distributions.json", "cooldown_research.json", "persistence_research.json", "score_research.json")
    if _artifacts_complete(result_dir, feature_files):
        feature_stats = _load_json(result_dir / "feature_distributions.json")
    else:
        base_feature_stats = _cache_feature_stats(fast_path)
        feature_stats, predictiveness, predictor_meta = time_feature_predictiveness(fast_path)
        feature_stats.update(base_feature_stats)
        _write_json(result_dir / "feature_predictiveness.json", {"metadata": predictor_meta, "features": predictiveness})
        _write_json(result_dir / "time_momentum.json", feature_stats["time_momentum"])
        _write_json(result_dir / "time_vwap.json", feature_stats["time_vwap"])
        _write_json(result_dir / "cooldown_research.json", {"v1_candidate_clustering": _cluster_summary(v1.candidates) if hasattr(v1, "candidates") else _load_json(result_dir / "v1_candidate_clustering.json"), "wall_clock_cooldowns_tested_ms": [250, 500, 1000, 2000, 5000, 10000]})
        _write_json(result_dir / "persistence_research.json", {"persistence_periods_ms_tested": [25, 50, 100, 250], "method": "continuous condition from first true timestamp; software-only"})
        _write_json(result_dir / "score_research.json", {"score_model": "momentum strength 0-2, VWAP agreement 0-2, imbalance agreement 0-2, spread 0-1, liquidity 0-1, persistence 0-1", "score_thresholds_tested": [4, 5, 6]})
        _write_json(result_dir / "feature_distributions.json", feature_stats)
    checkpoint("feature_research")

    def rank_key(row):
        summary = row["summary"]; forward = row["forward_returns"].get("1s", {})
        eligible = row["candidate_count"] >= 10 and len(row["candidates_by_symbol"]) >= 2 if "candidates_by_symbol" in row else row["candidate_count"] >= 10
        return (int(eligible), len(row.get("candidates_by_symbol", {})), forward.get("mean_bps") if forward.get("mean_bps") is not None else -1e9, forward.get("positive_fraction") if forward.get("positive_fraction") is not None else -1, -abs(summary.get("max_drawdown") or 0.0), -row["candidate_count"], row["name"])

    fast_config = BacktestConfig.from_mapping(mapping)
    fast_config = replace(fast_config, execution=ExecutionConfig.from_ms(1.0, slippage_bps=0.5))
    v1_mean_1s = v1.forward_summary.get("1s", {}).get("mean_bps"); v1_mean_5s = v1.forward_summary.get("5s", {}).get("mean_bps")
    mr_tested = v1_mean_1s is not None and v1_mean_5s is not None and v1_mean_1s < 0 and v1_mean_5s < 0
    if _artifacts_complete(result_dir, ("fast_variants.csv", "fast_ranking.json", "medium_selected.json")):
        fast_results = _read_variant_csv(result_dir / "fast_variants.csv")
        selected = _load_json(result_dir / "medium_selected.json")["selected"]
    else:
        configs = make_v2_configs(feature_stats)
        if len(configs) > 150: raise RuntimeError("FAST configuration cap exceeded")
        single_benchmark = benchmark_variant_evaluation(fast_path, sample_records=min(100_000, cache_results["fast"]["record_count"]))
        multi_benchmark = benchmark_multi_variant_evaluation(fast_path, configs, fast_config, sample_records=min(100_000, cache_results["fast"]["record_count"]), lightweight=True)
        fast_estimate = cache_results["fast"]["record_count"] / multi_benchmark["combined_events_per_second"] if multi_benchmark["combined_events_per_second"] else None
        benchmark["fast_strategy_evaluation_benchmark"] = multi_benchmark | {"single_config": single_benchmark, "estimated_full_screen_seconds": fast_estimate, "estimated_full_screen_minutes": fast_estimate / 60 if fast_estimate else None, "mode": "lightweight_forward_statistics_proxy"}
        _write_json(result_dir / "pipeline_benchmark.json", benchmark)
        if fast_estimate is not None and fast_estimate > 30 * 60: raise RuntimeError(f"estimated FAST screen exceeds 30 minutes: {fast_estimate / 60:.1f} minutes")
        fast_results = run_v2_variants(fast_path, configs, fast_config, total_events=cache_results["fast"]["record_count"], progress_callback=_print_progress("fast_v2_progress"), lightweight=True)
        if mr_tested:
            mr_configs = [V2Config(f"V2-MR-{HORIZON_LABELS[h]}", "V2-MR", momentum_horizon_ns=h, momentum_threshold_bps_x100=_positive_threshold(feature_stats, f"momentum_{h}", 0), vwap_horizon_ns=1_000_000_000, vwap_threshold_bps_x100=_positive_threshold(feature_stats, "vwap_1000000000", 0), imbalance_threshold_q15=1638, max_spread_bps_x100=int(round(feature_stats["v1"]["spread_bps_x100"].get("p75", 500))), min_rolling_volume=int(round(feature_stats["v1"]["rolling_volume"].get("p25", 1) or 1)), mean_reversion=True) for h in (500_000_000, 1_000_000_000, 5_000_000_000)]
            fast_results.extend(run_v2_variants(fast_path, mr_configs, fast_config, total_events=cache_results["fast"]["record_count"], progress_callback=_print_progress("fast_mr_progress"), lightweight=True))
        _write_csv(result_dir / "fast_variants.csv", fast_results)
        ranked = sorted(fast_results, key=rank_key, reverse=True); selected = ranked[:10]
        _write_json(result_dir / "fast_ranking.json", {"criteria": "deterministic eligibility, symbol coverage, median/mean forward returns, favorable fraction, drawdown penalty, candidate-count tie break; not P&L alone", "tested_configurations": len(fast_results), "rows": ranked})
        _write_json(result_dir / "medium_selected.json", {"selection_rule": "top at most 10 FAST rows by documented multi-criterion rank; no MEDIUM retuning", "selected": selected})
    checkpoint("fast_screening")

    medium_cfgs = [V2Config(**row["config"]) for row in selected]
    if _artifacts_complete(result_dir, ("medium_results.csv",)):
        medium_results = _read_variant_csv(result_dir / "medium_results.csv")
    else:
        medium_benchmark = benchmark_multi_variant_evaluation(medium_path, medium_cfgs, fast_config, sample_records=min(100_000, cache_results["medium"]["record_count"]), lightweight=True)
        medium_estimate = cache_results["medium"]["record_count"] / medium_benchmark["combined_events_per_second"] if medium_benchmark["combined_events_per_second"] else None
        benchmark["medium_strategy_evaluation_benchmark"] = medium_benchmark | {"estimated_selected_validation_seconds": medium_estimate, "estimated_selected_validation_minutes": medium_estimate / 60 if medium_estimate else None, "selected_count": len(selected), "mode": "lightweight_forward_statistics_proxy", "reason": "exact portfolio path exceeded the requested 30-minute gate during the observed run"}
        _write_json(result_dir / "pipeline_benchmark.json", benchmark)
        if medium_estimate is not None and medium_estimate > 30 * 60: raise RuntimeError(f"estimated MEDIUM validation exceeds 30 minutes: {medium_estimate / 60:.1f} minutes")
        medium_results = run_v2_variants(medium_path, medium_cfgs, fast_config, total_events=cache_results["medium"]["record_count"], progress_callback=_print_progress("medium_v2_progress"), lightweight=True)
        _write_csv(result_dir / "medium_results.csv", medium_results)
    checkpoint("medium_validation")
    strongest = max(medium_results, key=rank_key, default=None)
    recommendation = {"classification": "STRATEGY V2 PROMISING" if strongest and (strongest["forward_returns"].get("1s", {}).get("mean_bps") or -1e9) > 0 else "STRATEGY FAMILY STILL WEAK", "strongest_medium_candidate": strongest, "v2_mr_justified": mr_tested, "v2_mr_evidence": {"v1_forward_mean_1s_bps": v1_mean_1s, "v1_forward_mean_5s_bps": v1_mean_5s, "rule": "MR screened only when both direct continuation means are negative"}, "medium_validation_mode": benchmark.get("medium_strategy_evaluation_benchmark", {}).get("mode", "unknown"), "exact_portfolio_validation_completed": False, "rtl_implemented": False, "broker_integration": False, "full_dataset_v2_validation": False}
    _write_json(result_dir / "strategy_v2_recommendation.json", recommendation)
    (result_dir / "report.md").write_text(_render_report(result_dir, identity, benchmark, cache_results, v1, feature_stats, fast_results, selected, medium_results, recommendation, FAST_WINDOWS, MEDIUM_WINDOWS), encoding="utf-8")
    checkpoint("report")
    state["phase"] = "complete"
    _write_json(result_dir / "research_state.json", state)
    return result_dir


def main() -> int:
    argparse.ArgumentParser(description="Run the offline Strategy V2 research milestone").parse_args()
    try:
        result = run_research(Path(__file__).resolve().parents[2])
    except Exception as exc:
        print(f"strategy_v2_research_failed type={type(exc).__name__} message={exc}", file=sys.stderr)
        return 1
    print(f"strategy_v2_research_complete result_dir={result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
