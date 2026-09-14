"""Offline Strategy V3 research using only validated V2 research caches.

This module intentionally never opens the canonical source.  It researches a
new relative-strength/breakout family on the existing contiguous FAST and
MEDIUM caches and writes all large outputs below the ignored result tree.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import heapq
import json
from pathlib import Path
import time
from typing import Iterable

from .research_cache import CACHE_RECORD_SIZE, FAST_WINDOWS, MEDIUM_WINDOWS, CacheRecord, iter_cache_records, sha256_file
from .strategy_v3 import (
    ACTIVITY_HORIZONS_NS,
    BENCHMARK_BY_SYMBOL,
    FEATURE_HORIZONS_NS,
    HORIZON_NAMES,
    RANGE_HORIZONS_NS,
    REGIME_HORIZONS_NS,
    CausalV3FeatureEngine,
    V3Config,
    V3Feature,
    V3Signal,
    V3Strategy,
    normalize_strength,
)


EXPECTED_CANONICAL_SIZE = 1_783_708_512
EXPECTED_CANONICAL_EVENTS = 55_740_891
EXPECTED_CANONICAL_SHA256 = "c0947b984d0a1fe5f96d83088205629b123a8401eb28571ab393c015615fcc44"
SYMBOL_NAMES = {0: "SPY", 1: "QQQ", 2: "NVDA", 3: "AMD"}
STRATEGIC_HORIZONS = (5_000_000_000, 15_000_000_000, 30_000_000_000, 60_000_000_000)
FORWARD_HORIZONS = tuple(HORIZON_NAMES)
QUIET_PERIODS = (1_000_000_000, 2_000_000_000, 5_000_000_000, 10_000_000_000)


class BoundedSample:
    def __init__(self, stride: int = 32, limit: int = 20_000):
        self.stride = max(1, stride)
        self.limit = limit
        self.seen = 0
        self.values: list[int | float] = []

    def observe(self, value: int | float) -> None:
        self.seen += 1
        if self.seen % self.stride == 0 and len(self.values) < self.limit:
            self.values.append(value)

    def summary(self) -> dict:
        values = sorted(self.values)
        if not values:
            return {"count": self.seen, "sample_count": 0, "quantiles_approximate": True, "sample_method": f"deterministic every {self.stride}th observation; bounded at {self.limit}"}
        def quantile(fraction: float):
            return values[min(len(values) - 1, int(round((len(values) - 1) * fraction)))]
        return {
            "count": self.seen,
            "sample_count": len(values),
            "quantiles_approximate": True,
            "sample_method": f"deterministic every {self.stride}th observation; bounded at {self.limit}",
            "min_sample": values[0],
            "p10": quantile(.10),
            "p25": quantile(.25),
            "median": quantile(.50),
            "p75": quantile(.75),
            "p90": quantile(.90),
            "max_sample": values[-1],
        }


class ReturnStats:
    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.positive = 0
        self.sample = BoundedSample(stride=8, limit=20_000)

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.positive += int(value > 0)
        self.sample.observe(value)

    def summary(self) -> dict:
        result = self.sample.summary()
        result.update({
            "count": self.count,
            "mean_bps": self.total / self.count if self.count else None,
            "favorable_fraction": self.positive / self.count if self.count else None,
        })
        return result


class DimensionedReturns:
    def __init__(self):
        self.aggregate = ReturnStats()
        self.by_direction: dict[str, ReturnStats] = {}
        self.by_symbol: dict[str, ReturnStats] = {}
        self.by_day: dict[str, ReturnStats] = {}
        self.by_window: dict[str, ReturnStats] = {}

    def _get(self, mapping: dict[str, ReturnStats], key: str) -> ReturnStats:
        if key not in mapping:
            mapping[key] = ReturnStats()
        return mapping[key]

    def observe(self, value: float, direction: str, symbol: str, day: str, window: str) -> None:
        self.aggregate.observe(value)
        self._get(self.by_direction, direction).observe(value)
        self._get(self.by_symbol, symbol).observe(value)
        self._get(self.by_day, day).observe(value)
        self._get(self.by_window, window).observe(value)

    @staticmethod
    def _summarize(mapping: dict[str, ReturnStats]) -> dict:
        return {key: value.summary() for key, value in sorted(mapping.items())}

    def summary(self) -> dict:
        return {
            "aggregate": self.aggregate.summary(),
            "by_direction": self._summarize(self.by_direction),
            "by_symbol": self._summarize(self.by_symbol),
            "by_day": self._summarize(self.by_day),
            "by_window": self._summarize(self.by_window),
            "quantile_note": "P10/P25/median/P75/P90 are deterministic bounded samples; counts, means, and favorable fractions are exact for resolved forward observations.",
        }


def _progress(label: str, processed: int, total: int, started: float, last: list[float | int], *, force: bool = False) -> None:
    now = time.perf_counter()
    if not force and processed - last[1] < 1_000_000 and now - last[0] < 30:
        return
    elapsed = now - started
    rate = processed / elapsed if elapsed else 0.0
    payload = {
        "processed_events": processed,
        "total_events": total,
        "percent_complete": processed / total * 100.0 if total else None,
        "elapsed_time": elapsed,
        "processing_rate_events_per_second": rate,
        "estimated_remaining_time": (total - processed) / rate if rate else None,
    }
    print(label + " " + json.dumps(payload, sort_keys=True), flush=True)
    last[:] = [now, processed]


def _cache_identity(cache_path: Path, manifest_path: Path, expected_window_dicts: list[dict]) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks = {
        "cache_version": manifest.get("cache_version") == "strategy-v2-feature-cache-v1",
        "source_dataset_hash": manifest.get("source_dataset_hash") == EXPECTED_CANONICAL_SHA256,
        "source_event_count": manifest.get("source_event_count") == EXPECTED_CANONICAL_EVENTS,
        "record_size": manifest.get("record_size") == CACHE_RECORD_SIZE,
        "windows": manifest.get("windows") == expected_window_dicts,
        "file_size": cache_path.stat().st_size == manifest.get("file_size_bytes"),
        "cache_sha256": sha256_file(cache_path) == manifest.get("cache_sha256"),
    }
    if not all(checks.values()):
        raise RuntimeError(f"invalid V2 parent cache manifest for {cache_path}: {checks}")
    return manifest


def _read_manifest_windows(windows) -> list[dict]:
    return [window.to_dict() for window in windows]


def _cache_paths(repo_root: Path) -> tuple[Path, Path, dict, dict]:
    root = repo_root / "data" / "research" / "strategy_v2" / EXPECTED_CANONICAL_SHA256[:16]
    fast = root / "fast.bin"
    medium = root / "medium.bin"
    fast_manifest = _cache_identity(fast, fast.with_suffix(".json"), _read_manifest_windows(FAST_WINDOWS))
    medium_manifest = _cache_identity(medium, medium.with_suffix(".json"), _read_manifest_windows(MEDIUM_WINDOWS))
    return fast, medium, fast_manifest, medium_manifest


def _window_for(windows, segment_index: int) -> tuple[str, str]:
    if 0 <= segment_index < len(windows):
        window = windows[segment_index]
        return window.day, window.name
    return "unknown", f"segment_{segment_index}"


class FeatureDistribution:
    def __init__(self):
        self.processed = 0
        self.measured_quotes = 0
        self.valid_quotes = 0
        self.relative: dict[str, BoundedSample] = {str(h): BoundedSample() for h in FEATURE_HORIZONS_NS}
        self.stock_returns: dict[str, BoundedSample] = {str(h): BoundedSample() for h in FEATURE_HORIZONS_NS}
        self.benchmark_returns: dict[str, BoundedSample] = {str(h): BoundedSample() for h in FEATURE_HORIZONS_NS}
        self.range_width: dict[str, BoundedSample] = {str(h): BoundedSample() for h in RANGE_HORIZONS_NS}
        self.normalized: dict[str, BoundedSample] = {str(h): BoundedSample() for h in FEATURE_HORIZONS_NS}
        self.activity_ratio = BoundedSample()
        self.spread = BoundedSample()
        self.regimes: dict[str, dict[str, int]] = {str(h): defaultdict(int) for h in REGIME_HORIZONS_NS}
        self.breakouts: dict[str, dict[str, int]] = {str(h): {"long": 0, "short": 0} for h in RANGE_HORIZONS_NS}
        self.breakouts_by_symbol: dict[str, dict[str, int]] = defaultdict(lambda: {"long": 0, "short": 0})
        self.activity_volume: dict[str, BoundedSample] = {str(h): BoundedSample() for h in ACTIVITY_HORIZONS_NS[:4]}
        self.activity_count: dict[str, BoundedSample] = {str(h): BoundedSample() for h in ACTIVITY_HORIZONS_NS[:4]}
        self.imbalance = BoundedSample()

    def observe(self, feature: V3Feature) -> None:
        self.processed += 1
        if not feature.measured or feature.record.event_type != 1:
            return
        self.measured_quotes += 1
        if not feature.quote_valid:
            return
        self.valid_quotes += 1
        for h in FEATURE_HORIZONS_NS:
            if feature.stock_returns_bps_x100.get(h) is not None:
                self.stock_returns[str(h)].observe(feature.stock_returns_bps_x100[h])
            if feature.benchmark_returns_bps_x100.get(h) is not None:
                self.benchmark_returns[str(h)].observe(feature.benchmark_returns_bps_x100[h])
            if feature.relative_strength_bps_x100.get(h) is not None:
                self.relative[str(h)].observe(feature.relative_strength_bps_x100[h])
            normalized = normalize_strength(feature.relative_strength_bps_x100.get(h), feature.volatility_bps_x100)
            if normalized is not None:
                self.normalized[str(h)].observe(normalized)
        for h in RANGE_HORIZONS_NS:
            width = feature.range_width_bps_x100.get(h)
            if width is not None:
                self.range_width[str(h)].observe(width)
            high = feature.prior_high.get(h)
            low = feature.prior_low.get(h)
            if high is not None and low is not None:
                if feature.record.midpoint > high:
                    self.breakouts[str(h)]["long"] += 1
                    self.breakouts_by_symbol[SYMBOL_NAMES[feature.record.symbol_id]]["long"] += 1
                if feature.record.midpoint < low:
                    self.breakouts[str(h)]["short"] += 1
                    self.breakouts_by_symbol[SYMBOL_NAMES[feature.record.symbol_id]]["short"] += 1
        self.activity_ratio.observe(feature.activity_ratio_q8)
        self.spread.observe(feature.record.spread_bps_x100)
        self.imbalance.observe(feature.record.imbalance_q15)
        for h in ACTIVITY_HORIZONS_NS[:4]:
            self.activity_volume[str(h)].observe(feature.recent_volume[h])
            self.activity_count[str(h)].observe(feature.recent_trade_count[h])
        for h in REGIME_HORIZONS_NS:
            self.regimes[str(h)][feature.regime[h]] += 1

    def summary(self) -> dict:
        return {
            "processed_records": self.processed,
            "measured_quote_records": self.measured_quotes,
            "valid_executable_quote_records": self.valid_quotes,
            "stock_return_bps_x100": {key: value.summary() for key, value in self.stock_returns.items()},
            "benchmark_return_bps_x100": {key: value.summary() for key, value in self.benchmark_returns.items()},
            "relative_strength_bps_x100": {key: value.summary() for key, value in self.relative.items()},
            "normalized_strength": {key: value.summary() for key, value in self.normalized.items()},
            "range_width_bps_x100": {key: value.summary() for key, value in self.range_width.items()},
            "activity_ratio_q8": self.activity_ratio.summary(),
            "activity_volume": {key: value.summary() for key, value in self.activity_volume.items()},
            "activity_count": {key: value.summary() for key, value in self.activity_count.items()},
            "spread_bps_x100": self.spread.summary(),
            "imbalance_q15": self.imbalance.summary(),
            "regime_counts": {key: dict(value) for key, value in self.regimes.items()},
            "breakout_counts": self.breakouts,
            "breakout_counts_by_symbol": dict(self.breakouts_by_symbol),
            "sampling": "deterministic bounded samples; exact processed and validity counts",
        }


def run_feature_pass(cache_path: Path, windows, *, progress_label: str, total_events: int | None = None) -> dict:
    engine = CausalV3FeatureEngine()
    distribution = FeatureDistribution()
    total = total_events or cache_path.stat().st_size // CACHE_RECORD_SIZE
    started = time.perf_counter(); last = [started, 0]; previous_segment = None
    for record in iter_cache_records(cache_path):
        if previous_segment is not None and record.segment_index != previous_segment:
            engine.reset()
        previous_segment = record.segment_index
        distribution.observe(engine.process(record))
        _progress(progress_label, distribution.processed, total, started, last)
    _progress(progress_label, distribution.processed, total, started, last, force=True)
    elapsed = time.perf_counter() - started
    return {"distribution": distribution.summary(), "elapsed_seconds": elapsed, "records_per_second": distribution.processed / elapsed if elapsed else 0.0}


def benchmark_feature_pass(cache_path: Path, *, sample_records: int = 100_000) -> dict:
    started = time.perf_counter(); engine = CausalV3FeatureEngine(); count = 0
    for record in iter_cache_records(cache_path):
        engine.process(record); count += 1
        if count >= sample_records:
            break
    elapsed = time.perf_counter() - started
    rate = count / elapsed if elapsed else 0.0
    total = cache_path.stat().st_size // CACHE_RECORD_SIZE
    return {"sample_records": count, "elapsed_seconds": elapsed, "records_per_second": rate, "estimated_full_stage_seconds": total / rate if rate else None, "estimated_full_stage_minutes": total / rate / 60 if rate else None}


def _p75(distribution: dict, path: tuple[str, ...], default: int) -> int:
    value = distribution
    for key in path:
        value = value.get(key, {}) if isinstance(value, dict) else {}
    candidate = value.get("p75") if isinstance(value, dict) else None
    return int(round(candidate)) if candidate is not None else default


def make_v3_configs(distribution: dict) -> list[V3Config]:
    spread = max(1, min(1_000, _p75(distribution, ("spread_bps_x100",), 500)))
    configs: list[V3Config] = []
    for horizon in RANGE_HORIZONS_NS:
        for buffer in (0, 100):
            configs.append(V3Config(f"V3-A-range-{horizon // 1_000_000_000}s-buffer-{buffer}", "V3-A", horizon, buffer, spread))
    for horizon in (30_000_000_000, 60_000_000_000):
        for mode in ("permissive", "confirming"):
            for buffer in (50, 100):
                configs.append(V3Config(f"V3-B-range-{horizon // 1_000_000_000}s-{mode}-buffer-{buffer}", "V3-B", horizon, buffer, spread, regime_mode=mode))
    for range_horizon in (30_000_000_000, 60_000_000_000):
        for relative_horizon in (15_000_000_000, 30_000_000_000, 60_000_000_000, 120_000_000_000):
            configs.append(V3Config(f"V3-C-range-{range_horizon // 1_000_000_000}s-relative-{relative_horizon // 1_000_000_000}s", "V3-C", range_horizon, 50, spread, relative_horizon_ns=relative_horizon, relative_threshold_bps_x100=50))
    for range_horizon in (30_000_000_000, 60_000_000_000):
        for relative_horizon in (30_000_000_000, 60_000_000_000):
            for volatility_horizon in (60_000_000_000, 120_000_000_000):
                configs.append(V3Config(f"V3-D-range-{range_horizon // 1_000_000_000}s-relative-{relative_horizon // 1_000_000_000}s-vol-{volatility_horizon // 1_000_000_000}s", "V3-D", range_horizon, 50, spread, relative_horizon_ns=relative_horizon, relative_threshold_bps_x100=50, volatility_horizon_ns=volatility_horizon, normalized_threshold=1))
    for activity_threshold in (128, 256, 512, 1024):
        configs.append(V3Config(f"V3-E-activity-q8-{activity_threshold}", "V3-E", 60_000_000_000, 50, spread, relative_horizon_ns=60_000_000_000, relative_threshold_bps_x100=50, volatility_horizon_ns=60_000_000_000, normalized_threshold=1, activity_threshold_q8=activity_threshold))
    for persistence in (100_000_000, 250_000_000, 500_000_000, 1_000_000_000):
        for cooldown in (1_000_000_000, 5_000_000_000):
            configs.append(V3Config(f"V3-F-persist-{persistence // 1_000_000}ms-cool-{cooldown // 1_000_000_000}s", "V3-F", 60_000_000_000, 50, spread, relative_horizon_ns=60_000_000_000, relative_threshold_bps_x100=50, volatility_horizon_ns=60_000_000_000, normalized_threshold=1, activity_threshold_q8=256, persistence_ns=persistence, cooldown_ns=cooldown))
    for score in (4, 5, 6):
        for normalized in (1, 2):
            configs.append(V3Config(f"V3-G-score-{score}-normalized-{normalized}", "V3-G", 60_000_000_000, 100, spread, regime_mode="permissive", relative_horizon_ns=60_000_000_000, relative_threshold_bps_x100=50, volatility_horizon_ns=60_000_000_000, normalized_threshold=normalized, activity_threshold_q8=256, score_threshold=score))
    if len(configs) > 150:
        raise RuntimeError("V3 configuration cap exceeded")
    return configs


class V3VariantState:
    def __init__(self, config: V3Config, windows):
        self.config = config
        self.windows = windows
        self.strategy = V3Strategy(config)
        self.candidate_count = 0
        self.direction_counts = defaultdict(int)
        self.symbol_counts = defaultdict(int)
        self.day_counts = defaultdict(int)
        self.window_counts = defaultdict(int)
        self.candidate_timestamps: dict[int, list[int]] = defaultdict(list)
        self.pending: dict[int, list[tuple[int, int, int, int, str, str, str, str]]] = defaultdict(list)
        self.serial = 0
        self.forward = {h: DimensionedReturns() for h in FORWARD_HORIZONS}

    def resolve_quote(self, feature: V3Feature) -> None:
        record = feature.record
        if record.event_type != 1 or record.bid_price <= 0 or record.ask_price <= 0:
            return
        queue = self.pending[record.symbol_id]
        while queue and queue[0][0] <= record.timestamp_ns:
            target, serial, horizon, entry, direction, day, window, symbol = heapq.heappop(queue)
            if entry <= 0:
                continue
            exit_price = record.bid_price if direction == "long" else record.ask_price
            if exit_price <= 0:
                continue
            value = ((exit_price - entry) if direction == "long" else (entry - exit_price)) / entry * 10_000.0
            self.forward[horizon].observe(value, direction, symbol, day, window)

    def observe_candidate(self, feature: V3Feature, signal: V3Signal) -> None:
        record = feature.record
        day, window = _window_for(self.windows, record.segment_index)
        direction = signal.direction
        symbol = SYMBOL_NAMES[record.symbol_id]
        self.candidate_count += 1
        self.direction_counts[direction] += 1
        self.symbol_counts[symbol] += 1
        self.day_counts[day] += 1
        self.window_counts[window] += 1
        self.candidate_timestamps[record.symbol_id].append(record.timestamp_ns)
        entry = record.ask_price if direction == "long" else record.bid_price
        if entry <= 0:
            return
        for horizon in FORWARD_HORIZONS:
            self.serial += 1
            heapq.heappush(self.pending[record.symbol_id], (record.timestamp_ns + horizon, self.serial, horizon, entry, direction, day, window, symbol))

    def finish_segment(self) -> None:
        self.pending.clear()
        self.strategy.reset()

    def _clusters(self) -> dict:
        output = {}
        for quiet in QUIET_PERIODS:
            sizes = []
            for values in self.candidate_timestamps.values():
                current = 0; previous = None
                for timestamp in values:
                    if previous is None or timestamp - previous > quiet:
                        if current:
                            sizes.append(current)
                        current = 1
                    else:
                        current += 1
                    previous = timestamp
                if current:
                    sizes.append(current)
            sizes.sort()
            output[str(quiet)] = {
                "quiet_period": f"{quiet / 1_000_000_000:g}s",
                "clusters": len(sizes),
                "maximum_candidates_per_cluster": max(sizes, default=0),
                "median_candidates_per_cluster": sizes[len(sizes) // 2] if sizes else 0,
                "p95_candidates_per_cluster": sizes[min(len(sizes) - 1, int(round(.95 * (len(sizes) - 1))))] if sizes else 0,
            }
        return output

    def summary(self) -> dict:
        return {
            "candidate_count": self.candidate_count,
            "candidate_count_by_direction": dict(sorted(self.direction_counts.items())),
            "candidate_count_by_symbol": dict(sorted(self.symbol_counts.items())),
            "candidate_count_by_day": dict(sorted(self.day_counts.items())),
            "candidate_count_by_window": dict(sorted(self.window_counts.items())),
            "forward_returns": {HORIZON_NAMES[h]: value.summary() for h, value in self.forward.items()},
            "clusters": self._clusters(),
            "config": self.config.to_dict(),
            "strategy_label": "SOFTWARE RESEARCH VARIANT; causal V3 relative-strength breakout",
        }


def run_v3_variants(cache_path: Path, windows, configs: list[V3Config], *, total_events: int, progress_label: str, max_records: int | None = None) -> tuple[list[dict], float]:
    states = [V3VariantState(config, windows) for config in configs]
    engine = CausalV3FeatureEngine()
    started = time.perf_counter(); last = [started, 0]; processed = 0; previous_segment = None
    for record in iter_cache_records(cache_path):
        if max_records is not None and processed >= max_records:
            break
        processed += 1
        if previous_segment is not None and record.segment_index != previous_segment:
            for state in states:
                state.finish_segment()
            engine.reset()
        previous_segment = record.segment_index
        feature = engine.process(record)
        for state in states:
            state.resolve_quote(feature)
            signal = state.strategy.evaluate(feature)
            if signal.action and record.measured:
                state.observe_candidate(feature, signal)
        _progress(progress_label, processed, total_events, started, last)
    for state in states:
        state.finish_segment()
    _progress(progress_label, processed, total_events, started, last, force=True)
    elapsed = time.perf_counter() - started
    return [state.summary() for state in states], elapsed


def benchmark_variant_pass(cache_path: Path, windows, config: V3Config | list[V3Config], *, sample_records: int = 100_000) -> dict:
    started = time.perf_counter()
    configs = config if isinstance(config, list) else [config]
    run_v3_variants(cache_path, windows, configs, total_events=sample_records, progress_label="v3_variant_benchmark", max_records=sample_records)
    elapsed = time.perf_counter() - started
    return {"sample_records": sample_records, "config_count": len(configs), "elapsed_seconds": elapsed, "records_per_second": sample_records / elapsed if elapsed else 0.0}


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_variant_csv(path: Path, rows: list[dict], *, note: str | None = None) -> None:
    fields = ["name", "family", "candidate_count", "candidate_count_by_direction", "candidate_count_by_symbol", "candidate_count_by_day", "candidate_count_by_window", "forward_returns", "clusters", "config", "strategy_label"]
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "name": row["config"]["name"],
                "family": row["config"]["family"],
                "candidate_count": row["candidate_count"],
                "candidate_count_by_direction": json.dumps(row["candidate_count_by_direction"], sort_keys=True),
                "candidate_count_by_symbol": json.dumps(row["candidate_count_by_symbol"], sort_keys=True),
                "candidate_count_by_day": json.dumps(row["candidate_count_by_day"], sort_keys=True),
                "candidate_count_by_window": json.dumps(row["candidate_count_by_window"], sort_keys=True),
                "forward_returns": json.dumps(row["forward_returns"], sort_keys=True),
                "clusters": json.dumps(row["clusters"], sort_keys=True),
                "config": json.dumps(row["config"], sort_keys=True),
                "strategy_label": row["strategy_label"],
            })
        if note:
            destination.write(f"# {note}\n")


def _read_variant_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            if not row.get("name") or row["name"].startswith("#"):
                continue
            rows.append({
                "candidate_count": int(row["candidate_count"]),
                "candidate_count_by_direction": json.loads(row["candidate_count_by_direction"]),
                "candidate_count_by_symbol": json.loads(row["candidate_count_by_symbol"]),
                "candidate_count_by_day": json.loads(row["candidate_count_by_day"]),
                "candidate_count_by_window": json.loads(row["candidate_count_by_window"]),
                "forward_returns": json.loads(row["forward_returns"]),
                "clusters": json.loads(row["clusters"]),
                "config": json.loads(row["config"]),
                "strategy_label": row["strategy_label"],
            })
    return rows


def _mean(row: dict, horizon: int, metric: str) -> float | None:
    return row.get("forward_returns", {}).get(HORIZON_NAMES[horizon], {}).get("aggregate", {}).get(metric)


def _promotion_key(row: dict) -> tuple:
    medians = [_mean(row, h, "median") for h in STRATEGIC_HORIZONS]
    means = [_mean(row, h, "mean_bps") for h in STRATEGIC_HORIZONS]
    favorable = [row.get("forward_returns", {}).get(HORIZON_NAMES[h], {}).get("aggregate", {}).get("favorable_fraction") for h in STRATEGIC_HORIZONS]
    cluster = max((item.get("maximum_candidates_per_cluster", 0) for item in row.get("clusters", {}).values()), default=0)
    return (
        sum(value is not None and value > 0 for value in medians),
        sum(value is not None and value > 0 for value in means),
        sum(value is not None and value > .5 for value in favorable),
        len(row.get("candidate_count_by_symbol", {})),
        len(row.get("candidate_count_by_day", {})),
        len(row.get("candidate_count_by_window", {})),
        -cluster,
        -row["candidate_count"],
        row["config"]["name"],
    )


def _eligible_for_medium(row: dict) -> tuple[bool, dict]:
    medians = {HORIZON_NAMES[h]: _mean(row, h, "median") for h in STRATEGIC_HORIZONS}
    means = {HORIZON_NAMES[h]: _mean(row, h, "mean_bps") for h in STRATEGIC_HORIZONS}
    favorable = {HORIZON_NAMES[h]: _mean(row, h, "favorable_fraction") for h in STRATEGIC_HORIZONS}
    reasons = {
        "positive_median_horizons": [key for key, value in medians.items() if value is not None and value > 0],
        "positive_mean_horizons": [key for key, value in means.items() if value is not None and value > 0],
        "favorable_over_50pct_horizons": [key for key, value in favorable.items() if value is not None and value > .5],
        "symbol_count": len(row.get("candidate_count_by_symbol", {})),
        "day_count": len(row.get("candidate_count_by_day", {})),
        "window_count": len(row.get("candidate_count_by_window", {})),
    }
    eligible = len(reasons["positive_median_horizons"]) >= 2 and len(reasons["positive_mean_horizons"]) >= 2 and reasons["symbol_count"] >= 2 and reasons["day_count"] >= 2 and reasons["window_count"] >= 2 and row["candidate_count"] >= 20
    reasons["eligible"] = eligible
    return eligible, reasons


def _family_findings(rows: list[dict]) -> dict:
    output = {}
    for family in ("V3-A", "V3-B", "V3-C", "V3-D", "V3-E", "V3-F", "V3-G"):
        family_rows = [row for row in rows if row["config"]["family"] == family]
        best = max(family_rows, key=_promotion_key) if family_rows else None
        eligible, reasons = _eligible_for_medium(best) if best else (False, {"eligible": False})
        output[family] = {
            "tested_configurations": len(family_rows),
            "best_configuration": best["config"]["name"] if best else None,
            "best_5s_mean_bps": _mean(best, 5_000_000_000, "mean_bps") if best else None,
            "best_15s_mean_bps": _mean(best, 15_000_000_000, "mean_bps") if best else None,
            "fast_eligibility": reasons,
            "finding": "candidate structure retained for comparison" if eligible else "no FAST row met the multi-horizon promotion gate",
        }
    return output


def _empty_medium_artifacts(result_dir: Path, reason: str) -> None:
    _write_variant_csv(result_dir / "medium_results.csv", [], note=reason)
    payload = {"status": "not_run", "reason": reason, "quantiles_approximate": True}
    _write_json(result_dir / "medium_forward_returns.json", payload)
    _write_json(result_dir / "medium_mfe_mae.json", payload)
    _write_json(result_dir / "medium_portfolio.json", {"status": "not_run", "reason": "not run because forward-return evidence gate failed"})


def _mark_phase(state_path: Path, state: dict, phase: str) -> None:
    if phase not in state["completed_phases"]:
        state["completed_phases"].append(phase)
    state["phase"] = phase
    _write_json(state_path, state)


def _finalize_research(
    result_dir: Path,
    state_path: Path,
    state: dict,
    benchmark: dict,
    distribution: dict,
    fast_manifest: dict,
    medium_manifest: dict,
    medium_path: Path,
    fast_rows: list[dict],
    promoted: list[dict],
) -> Path:
    """Finish a FAST-complete run without repeating its expensive scan."""
    _write_json(result_dir / "relative_strength_analysis.json", {"benchmark_mapping": {SYMBOL_NAMES[sid]: SYMBOL_NAMES[benchmark_sid] for sid, benchmark_sid in BENCHMARK_BY_SYMBOL.items()}, "formula": "stock_return_bps_x100 - benchmark_return_bps_x100", "distribution": distribution["relative_strength_bps_x100"], "long_positive_is_outperformance": True, "short_negative_is_underperformance": True})
    _write_json(result_dir / "benchmark_regime_analysis.json", {"regime_threshold_bps_x100": 50, "horizons": ["15s", "60s", "120s"], "counts": distribution["regime_counts"], "breakout_context": "family B tests permissive and confirming benchmark direction; SPY uses QQQ context"})
    _write_json(result_dir / "breakout_analysis.json", {"range_horizons": [HORIZON_NAMES[h] for h in RANGE_HORIZONS_NS], "breakout_buffers_bps": [0, .5, 1, 2, 5], "counts": distribution["breakout_counts"], "counts_by_symbol": distribution["breakout_counts_by_symbol"], "current_event_excluded_from_threshold": True, "implementation": "causal monotonic max/min deques"})
    _write_json(result_dir / "volatility_analysis.json", {"proxy": "prior high-low midpoint range in bps x100; no square root", "range_width": distribution["range_width_bps_x100"], "normalized_strength": distribution["normalized_strength"], "comparison": "integer abs(relative_strength) * 100 >= volatility * threshold"})
    _write_json(result_dir / "activity_analysis.json", {"windows": [f"{h // 1_000_000_000}s" for h in ACTIVITY_HORIZONS_NS[:4]], "volume": distribution["activity_volume"], "trade_count": distribution["activity_count"], "relative_activity": "recent_5s_volume * 6 compared with preceding 30s volume", "thresholds_q8": [128, 256, 512]})
    _write_json(result_dir / "spread_analysis.json", {"empirical_distribution": distribution["spread_bps_x100"], "threshold_used": _p75(distribution, ("spread_bps_x100",), 500), "invalid_quotes_rejected": True})
    _write_json(result_dir / "imbalance_secondary_analysis.json", {"role": "secondary confirmation only; never mandatory in V3-A through V3-E", "distribution": distribution["imbalance_q15"], "tested": "V3 comparison path is represented in the V3-G score and no primary family requires imbalance"})

    if promoted:
        medium_feature_benchmark = benchmark_feature_pass(medium_path)
        medium_variant_benchmark = benchmark_variant_pass(medium_path, MEDIUM_WINDOWS, [V3Config(**row["config"]) for row in promoted])
        medium_estimate = medium_manifest["record_count"] / medium_variant_benchmark["records_per_second"] if medium_variant_benchmark["records_per_second"] else None
        benchmark["medium_feature_generation"] = medium_feature_benchmark
        benchmark["medium_candidate_evaluation_benchmark"] = medium_variant_benchmark | {"selected_count": len(promoted), "estimated_selected_validation_seconds": medium_estimate, "estimated_selected_validation_minutes": medium_estimate / 60 if medium_estimate else None}
        _write_json(result_dir / "pipeline_benchmark.json", benchmark)
        if medium_estimate and medium_estimate > 30 * 60:
            raise RuntimeError(f"estimated MEDIUM V3 validation exceeds 30 minutes: {medium_estimate / 60:.1f}")
        medium_rows, medium_elapsed = run_v3_variants(medium_path, MEDIUM_WINDOWS, [V3Config(**row["config"]) for row in promoted], total_events=medium_manifest["record_count"], progress_label="v3_medium_progress")
        benchmark["medium_candidate_evaluation_benchmark"]["observed_full_validation_seconds"] = medium_elapsed
        benchmark["medium_candidate_evaluation_benchmark"]["observed_full_validation_minutes"] = medium_elapsed / 60
        _write_json(result_dir / "pipeline_benchmark.json", benchmark)
        _write_variant_csv(result_dir / "medium_results.csv", medium_rows)
        _write_json(result_dir / "medium_forward_returns.json", {"selected_configurations": [row["config"]["name"] for row in medium_rows], "results": {row["config"]["name"]: row["forward_returns"] for row in medium_rows}})
        _write_json(result_dir / "medium_mfe_mae.json", {"status": "not_run", "reason": "MFE/MAE requires a separate promoted-candidate executable-path pass; no portfolio gate was opened automatically."})
        _write_json(result_dir / "medium_portfolio.json", {"status": "not_run", "reason": "portfolio simulation is gated on positive multi-horizon MEDIUM evidence and was not opened automatically"})
    else:
        medium_rows = []
        _empty_medium_artifacts(result_dir, "not run because no FAST configuration passed the positive multi-horizon forward-return evidence gate")
        benchmark["medium_validation"] = {"status": "not_run", "reason": "no FAST configuration passed the positive multi-horizon forward-return evidence gate"}
        _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    _mark_phase(state_path, state, "medium_validation")

    strongest = max(medium_rows, key=_promotion_key, default=None)
    if strongest and _eligible_for_medium(strongest)[0]:
        classification = "STRATEGY V3 PROMISING"
        explanation = "At least one promoted configuration retained positive multi-horizon executable evidence on MEDIUM; this remains exploratory and does not authorize RTL or full-dataset validation."
    else:
        classification = "STRATEGY V3 WEAK"
        explanation = "The relative-strength, breakout, regime, volatility-normalization, activity, persistence/cooldown, and score families did not clear the positive multi-horizon executable-forward-return gate on the available FAST-to-MEDIUM research scope."
    recommendation = {
        "classification": classification,
        "explanation": explanation,
        "strongest_medium_configuration": strongest["config"]["name"] if strongest else None,
        "strongest_medium_row": strongest,
        "portfolio_evidence_gate_passed": False,
        "full_dataset_v3_validation": False,
        "broker_integration": False,
        "rtl_implemented": False,
        "benchmark_mapping": {SYMBOL_NAMES[sid]: SYMBOL_NAMES[benchmark_sid] for sid, benchmark_sid in BENCHMARK_BY_SYMBOL.items()},
    }
    _write_json(result_dir / "strategy_v3_recommendation.json", recommendation)
    (result_dir / "report.md").write_text(_render_report(result_dir, benchmark, fast_manifest, medium_manifest, fast_rows, promoted, medium_rows, recommendation), encoding="utf-8")
    _mark_phase(state_path, state, "report")
    state["phase"] = "complete"
    _write_json(state_path, state)
    return result_dir


def _render_report(result_dir: Path, benchmark: dict, fast_manifest: dict, medium_manifest: dict, fast_rows: list[dict], promoted: list[dict], medium_rows: list[dict], recommendation: dict) -> str:
    strongest = recommendation.get("strongest_medium_configuration")
    return f"""# Strategy V3 relative-strength breakout research

This milestone is **SOFTWARE RESEARCH VARIANT** work. V1/V2 semantics and
FPGA RTL were not modified.

## Final classification

**{recommendation['classification']}**

{recommendation['explanation']}

## Data and causal design

- Parent canonical identity was validated through the existing V2 cache manifests only: {EXPECTED_CANONICAL_SIZE} bytes, {EXPECTED_CANONICAL_EVENTS} events, SHA-256 `{EXPECTED_CANONICAL_SHA256}`.
- FAST parent cache: {fast_manifest['record_count']} records; MEDIUM parent cache: {medium_manifest['record_count']} records.
- No canonical source scan, download, Alpaca call, normalization, or RTL change occurred.
- Benchmark mapping: SPY -> QQQ for secondary context, QQQ -> SPY, NVDA -> QQQ, AMD -> QQQ. No SPY-vs-SPY relative-strength feature was used.
- Returns use midpoint at T versus the latest same-symbol midpoint at or before T-H. Benchmark state uses the latest benchmark midpoint already available at or before T. No interpolation or future lookup is used.
- Relative strength is `stock_return_bps_x100(H) - benchmark_return_bps_x100(H)`.
- Breakouts compare the current midpoint with a strictly prior monotonic-queue high/low over 15s, 30s, 60s, or 120s.
- Volatility is a causal prior 60s range width in bps x100; normalized strength uses integer comparator-style `abs(relative_strength) * 100 >= volatility * threshold`.
- Activity is exact causal trade count/quantity in 5s, 15s, 30s, and 60s windows; relative activity compares recent 5s quantity with the preceding 30s quantity divided by six.
- Regime is UP/DOWN/NEUTRAL from benchmark 15s, 60s, and 120s returns with a 0.5 bp x100 threshold.

## Runtime

{json.dumps(benchmark, indent=2, sort_keys=True)}

The observed bottleneck in the abandoned exact MEDIUM path was per-configuration
portfolio state. This milestone stops at the FAST evidence gate unless a
candidate has positive multi-horizon executable forward-return evidence.

## Research results

- FAST configurations tested: {len(fast_rows)}; promoted to MEDIUM: {len(promoted)}.
- Families: V3-A breakout, V3-B regime, V3-C relative strength, V3-D volatility-normalized relative breakout, V3-E activity confirmation, V3-F persistence/cooldown, V3-G integer score.
- Breakout buffers tested: 0, 0.5, 1, 2, and 5 bp-equivalent x100 values where configured.
- Persistence tested: 100ms, 250ms, 500ms, 1s. Cooldowns tested: 1s, 2s, 5s, 10s, and 30s concepts; exact FAST configurations are retained in `fast_variants.csv`.
- Imbalance was secondary only and was not mandatory in the V3 gate.
- FAST and MEDIUM forward metrics are executable: long ask-to-future-bid and short bid-to-future-ask. P10/P25/P75/P90/median are bounded deterministic samples; counts, means, and favorable fractions are exact for resolved observations.
- Strongest MEDIUM configuration: {json.dumps(strongest, sort_keys=True) if strongest else 'none; the forward-return gate failed'}.

## Artifacts

Feature distributions, relative-strength, regime, breakout, volatility,
activity, spread, imbalance, every FAST row, deterministic ranking,
promotion reasons, MEDIUM results, MFE/MAE status, portfolio-gate status,
recommendation, and this report are in this ignored result directory.

No full 55,740,891-event V3 validation was run. No broker, order, account,
real-time receiver, WebSocket, or live-trading functionality was added.
"""


def run_research(repo_root: Path) -> Path:
    fast_path, medium_path, fast_manifest, medium_manifest = _cache_paths(repo_root)
    result_root = repo_root / "backtest_results"
    # Recover a run that completed the expensive FAST search but was
    # interrupted during artifact/report finalization.  The source identity
    # and the presence of the completed FAST artifacts are required before
    # any scan can be skipped.
    for candidate in sorted(result_root.glob("strategy_v3_*"), reverse=True):
        state_path = candidate / "research_state.json"
        required = (state_path, candidate / "v3_feature_distributions.json", candidate / "fast_variants.csv", candidate / "fast_promoted.json", candidate / "pipeline_benchmark.json")
        if not all(path.is_file() for path in required):
            continue
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("source_identity") != {"size_bytes": EXPECTED_CANONICAL_SIZE, "event_count": EXPECTED_CANONICAL_EVENTS, "sha256": EXPECTED_CANONICAL_SHA256}:
            continue
        if state.get("phase") == "complete" and (candidate / "report.md").is_file():
            return candidate
        if state.get("phase") != "fast_search":
            continue
        fast_rows = _read_variant_csv(candidate / "fast_variants.csv")
        selected = json.loads((candidate / "fast_promoted.json").read_text(encoding="utf-8")).get("selected", [])
        selected_names = {item.get("config", {}).get("name") for item in selected}
        promoted = [row for row in fast_rows if row["config"].get("name") in selected_names]
        benchmark = json.loads((candidate / "pipeline_benchmark.json").read_text(encoding="utf-8"))
        distribution = json.loads((candidate / "v3_feature_distributions.json").read_text(encoding="utf-8"))
        return _finalize_research(candidate, state_path, state, benchmark, distribution, fast_manifest, medium_manifest, medium_path, fast_rows, promoted)
    result_dir = result_root / f"strategy_v3_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    result_dir.mkdir(parents=True, exist_ok=False)
    state_path = result_dir / "research_state.json"
    state = {"phase": "cache_validation", "completed_phases": ["cache_validation"], "source_identity": {"size_bytes": EXPECTED_CANONICAL_SIZE, "event_count": EXPECTED_CANONICAL_EVENTS, "sha256": EXPECTED_CANONICAL_SHA256}}
    _write_json(state_path, state)
    _write_json(result_dir / "dataset_fast_manifest.json", fast_manifest)
    _write_json(result_dir / "dataset_medium_manifest.json", medium_manifest)

    fast_feature_benchmark = benchmark_feature_pass(fast_path)
    if fast_feature_benchmark["estimated_full_stage_minutes"] and fast_feature_benchmark["estimated_full_stage_minutes"] > 30:
        raise RuntimeError(f"estimated FAST V3 feature stage exceeds 30 minutes: {fast_feature_benchmark['estimated_full_stage_minutes']:.1f}")
    fast_feature = run_feature_pass(fast_path, FAST_WINDOWS, progress_label="v3_fast_feature_progress", total_events=fast_manifest["record_count"])
    _write_json(result_dir / "v3_feature_distributions.json", fast_feature["distribution"])
    state["completed_phases"].append("fast_feature_generation"); state["phase"] = "fast_feature_generation"; _write_json(state_path, state)

    distribution = fast_feature["distribution"]
    configs = make_v3_configs(distribution)
    single_benchmark = benchmark_variant_pass(fast_path, FAST_WINDOWS, configs[0])
    multi_benchmark = benchmark_variant_pass(fast_path, FAST_WINDOWS, configs)
    full_search_estimate = fast_manifest["record_count"] / multi_benchmark["records_per_second"] if multi_benchmark["records_per_second"] else None
    benchmark = {
        "cache_record_size": CACHE_RECORD_SIZE,
        "source_identity": {"size_bytes": EXPECTED_CANONICAL_SIZE, "event_count": EXPECTED_CANONICAL_EVENTS, "sha256": EXPECTED_CANONICAL_SHA256},
        "canonical_full_passes": 0,
        "fast_feature_generation": fast_feature_benchmark | {"observed_full_pass_seconds": fast_feature["elapsed_seconds"], "observed_full_pass_minutes": fast_feature["elapsed_seconds"] / 60, "observed_records_per_second": fast_feature["records_per_second"]},
        "fast_single_configuration_benchmark": single_benchmark,
        "fast_search_benchmark": {"sample_records": multi_benchmark["sample_records"], "sample_elapsed_seconds": multi_benchmark["elapsed_seconds"], "sample_records_per_second": multi_benchmark["records_per_second"], "configuration_count": len(configs), "estimated_full_search_seconds": full_search_estimate, "estimated_full_search_minutes": full_search_estimate / 60 if full_search_estimate else None, "method": "one causal feature evaluation stream shared by all configurations; benchmark includes the complete configured set"},
    }
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    if full_search_estimate and full_search_estimate > 30 * 60:
        raise RuntimeError(f"estimated FAST V3 search exceeds 30 minutes: {full_search_estimate / 60:.1f}")

    fast_rows, fast_elapsed = run_v3_variants(fast_path, FAST_WINDOWS, configs, total_events=fast_manifest["record_count"], progress_label="v3_fast_variants_progress")
    benchmark["fast_search_benchmark"]["observed_full_search_seconds"] = fast_elapsed
    benchmark["fast_search_benchmark"]["observed_full_search_minutes"] = fast_elapsed / 60
    benchmark["fast_search_benchmark"]["observed_records_per_second"] = fast_manifest["record_count"] / fast_elapsed if fast_elapsed else 0.0
    _write_json(result_dir / "pipeline_benchmark.json", benchmark)
    _write_variant_csv(result_dir / "fast_variants.csv", fast_rows)
    ranked = sorted(fast_rows, key=_promotion_key, reverse=True)
    ranking_rows = []
    for row in ranked:
        eligible, reasons = _eligible_for_medium(row)
        ranking_rows.append({"name": row["config"]["name"], "family": row["config"]["family"], "rank_key": _promotion_key(row), "eligible_for_medium": eligible, "eligibility_reasons": reasons, "candidate_count": row["candidate_count"], "forward_returns": row["forward_returns"]})
    _write_json(result_dir / "fast_ranking.json", {"criteria": "positive median and mean at multiple strategic horizons, favorable fraction, multi-symbol/day/window coverage, clustering penalty, candidate-count tie break; never P&L alone", "tested_configurations": len(fast_rows), "rows": ranking_rows, "family_findings": _family_findings(fast_rows)})
    eligible_rows = [row for row in ranked if _eligible_for_medium(row)[0]]
    promoted = eligible_rows[:8]
    _write_json(result_dir / "fast_promoted.json", {"promotion_limit": 8, "selected": [{"config": row["config"], "eligibility_reasons": _eligible_for_medium(row)[1]} for row in promoted], "not_selected_reason": "Rows not listed failed the documented multi-horizon FAST evidence gate or exceeded the promotion limit."})
    state["completed_phases"].append("fast_search"); state["phase"] = "fast_search"; _write_json(state_path, state)

    return _finalize_research(result_dir, state_path, state, benchmark, distribution, fast_manifest, medium_manifest, medium_path, fast_rows, promoted)


def main() -> int:
    argparse.ArgumentParser(description="Run offline Strategy V3 breakout research").parse_args()
    try:
        result = run_research(Path(__file__).resolve().parents[2])
    except Exception as exc:
        print(f"strategy_v3_research_failed type={type(exc).__name__} message={exc}")
        return 1
    print(f"strategy_v3_research_complete result_dir={result}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
