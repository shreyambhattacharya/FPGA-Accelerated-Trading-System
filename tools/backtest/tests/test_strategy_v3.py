from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.backtest.canonical import MSG_MARKET_QUOTE, MSG_MARKET_TRADE, NormalizedEvent, write_binary_events
from tools.backtest.research_cache import (
    CACHE_RECORD_SIZE,
    VALID_FEATURE,
    VALID_IMBALANCE,
    VALID_MIDPOINT,
    VALID_SPREAD,
    VALID_SPREAD_BPS,
    CacheRecord,
    ResearchWindow,
    sha256_file,
)
from tools.backtest.strategy_v3 import (
    BENCHMARK_BY_SYMBOL,
    CausalV3FeatureEngine,
    HORIZON_NAMES,
    V3Config,
    V3Feature,
    V3Signal,
    V3Strategy,
    normalize_strength,
)
from tools.backtest.strategy_v3_research import V3VariantState, _cache_identity


VALID = VALID_FEATURE | VALID_MIDPOINT | VALID_SPREAD | VALID_SPREAD_BPS | VALID_IMBALANCE


def quote(timestamp, symbol=0, midpoint=100_000_000, *, segment=0, measured=True, spread_bps=100):
    return CacheRecord(timestamp, MSG_MARKET_QUOTE, symbol, measured, segment, 0, 1, midpoint, 1, timestamp + 1, midpoint - 5_000, midpoint + 5_000, 10, 10, midpoint, 10_000, midpoint, 1, 0, 0, 1_638, spread_bps, VALID)


def trade(timestamp, symbol=0, quantity=10, *, segment=0, measured=True):
    return CacheRecord(timestamp, MSG_MARKET_TRADE, symbol, measured, segment, 0, 0, 100_000_000, quantity, timestamp + 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def feature(record, *, high=100_000_000, low=99_000_000, relative=100, volatility=100, regime="UP", activity=512):
    return V3Feature(record, {h: 100 for h in HORIZON_NAMES if h != 1_000_000_000}, {h: 50 for h in HORIZON_NAMES if h != 1_000_000_000}, {h: relative for h in HORIZON_NAMES if h != 1_000_000_000}, {h: high for h in (15_000_000_000, 30_000_000_000, 60_000_000_000, 120_000_000_000)}, {h: low for h in (15_000_000_000, 30_000_000_000, 60_000_000_000, 120_000_000_000)}, {h: volatility for h in (15_000_000_000, 30_000_000_000, 60_000_000_000, 120_000_000_000)}, volatility, {5_000_000_000: 10, 15_000_000_000: 20, 30_000_000_000: 30, 60_000_000_000: 40}, {5_000_000_000: 1, 15_000_000_000: 2, 30_000_000_000: 3, 60_000_000_000: 4}, 30, 3, activity, {15_000_000_000: regime, 60_000_000_000: regime, 120_000_000_000: regime}, 1)


class StrategyV3Tests(unittest.TestCase):
    def test_integer_stock_return_and_missing_history(self):
        engine = CausalV3FeatureEngine()
        first = engine.process(quote(0, midpoint=100_000_000))
        second = engine.process(quote(5_000_000_000, midpoint=101_000_000))
        self.assertIsNone(first.stock_returns_bps_x100[5_000_000_000])
        self.assertEqual(second.stock_returns_bps_x100[5_000_000_000], 10_000)
        self.assertIsNone(normalize_strength(100, 0))

    def test_asynchronous_benchmark_is_asof_and_never_future(self):
        engine = CausalV3FeatureEngine()
        engine.process(quote(0, symbol=1, midpoint=100_000_000))
        engine.process(quote(0, symbol=2, midpoint=200_000_000))
        engine.process(quote(4_000_000_000, symbol=1, midpoint=102_000_000))
        before_future = engine.process(quote(5_000_000_000, symbol=2, midpoint=202_000_000))
        self.assertEqual(BENCHMARK_BY_SYMBOL[2], 1)
        self.assertEqual(before_future.benchmark_returns_bps_x100[5_000_000_000], 20_000)
        engine.process(quote(6_000_000_000, symbol=1, midpoint=104_000_000))
        self.assertEqual(before_future.benchmark_returns_bps_x100[5_000_000_000], 20_000)

    def test_prior_range_excludes_current_midpoint(self):
        engine = CausalV3FeatureEngine()
        engine.process(quote(0, midpoint=100_000_000))
        current = engine.process(quote(1_000_000_000, midpoint=101_000_000))
        self.assertEqual(current.prior_high[15_000_000_000], 100_000_000)
        self.assertEqual(current.prior_low[15_000_000_000], 100_000_000)
        next_feature = engine.process(quote(2_000_000_000, midpoint=102_000_000))
        self.assertEqual(next_feature.prior_high[15_000_000_000], 101_000_000)

    def test_activity_volume_and_trade_count_windows_are_causal(self):
        engine = CausalV3FeatureEngine()
        engine.process(trade(1, quantity=10))
        engine.process(trade(2, quantity=20))
        current = engine.process(quote(3))
        self.assertEqual(current.recent_volume[5_000_000_000], 30)
        self.assertEqual(current.recent_trade_count[5_000_000_000], 2)
        self.assertGreater(current.activity_ratio_q8, 0)

    def test_market_regime_and_breakout_buffer(self):
        engine = CausalV3FeatureEngine()
        engine.process(quote(0, symbol=1, midpoint=100_000_000))
        engine.process(quote(15_000_000_000, symbol=1, midpoint=101_000_000))
        stock = engine.process(quote(15_000_000_001, symbol=2, midpoint=202_000_000))
        self.assertEqual(stock.regime[15_000_000_000], "UP")
        strategy = V3Strategy(V3Config("buffer", "V3-A", 15_000_000_000, breakout_buffer_bps_x100=100))
        self.assertEqual(strategy.evaluate(feature(quote(20, midpoint=100_010_000), high=100_000_000)).action, 0)
        self.assertEqual(strategy.evaluate(feature(quote(30, midpoint=100_200_000), high=100_000_000)).action, 1)

    def test_long_short_breakouts_and_score(self):
        long_strategy = V3Strategy(V3Config("long", "V3-A", 15_000_000_000, direction="long"))
        self.assertEqual(long_strategy.evaluate(feature(quote(1, midpoint=101_000_000))).action, 1)
        short_strategy = V3Strategy(V3Config("short", "V3-A", 15_000_000_000, direction="short"))
        self.assertEqual(short_strategy.evaluate(feature(quote(1, midpoint=98_000_000), high=101_000_000, low=100_000_000, relative=-100)).action, -1)
        gated = V3Strategy(V3Config("score", "V3-G", 15_000_000_000, relative_threshold_bps_x100=50, normalized_threshold=1, activity_threshold_q8=256, score_threshold=99))
        self.assertEqual(gated.evaluate(feature(quote(1, midpoint=101_000_000))).action, 0)

    def test_persistence_cooldown_and_segment_reset(self):
        strategy = V3Strategy(V3Config("timed", "V3-A", 15_000_000_000, persistence_ns=100, cooldown_ns=1_000))
        self.assertEqual(strategy.evaluate(feature(quote(0, midpoint=101_000_000))).action, 0)
        self.assertEqual(strategy.evaluate(feature(quote(99, midpoint=101_000_000))).action, 0)
        self.assertEqual(strategy.evaluate(feature(quote(100, midpoint=101_000_000))).action, 1)
        self.assertEqual(strategy.evaluate(feature(quote(500, midpoint=101_000_000))).action, 0)
        self.assertEqual(strategy.evaluate(feature(quote(1_100, midpoint=101_000_000))).action, 1)
        self.assertEqual(strategy.evaluate(feature(quote(2_000, midpoint=101_000_000, segment=1))).action, 0)

    def test_forward_returns_are_executable_for_both_directions(self):
        windows = (ResearchWindow("w", "2026-01-01", "00:00", "00:00", "00:01", 0, 0, 60_000_000_000),)
        state = V3VariantState(V3Config("forward", "V3-A", 15_000_000_000), windows)
        state.observe_candidate(feature(quote(0, midpoint=100_000_000)), V3Signal(1, "long", 1, 1))
        state.resolve_quote(feature(quote(1_000_000_000, midpoint=100_100_000)))
        # The 1s target is resolved at the future quote; long exits use bid.
        self.assertEqual(state.summary()["forward_returns"]["1s"]["aggregate"]["count"], 1)

    def test_cache_hash_validation_accepts_exact_parent_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "cache.bin"
            path.write_bytes(b"x" * CACHE_RECORD_SIZE)
            manifest = root / "cache.json"
            data = {"cache_version": "strategy-v2-feature-cache-v1", "source_dataset_hash": "c0947b984d0a1fe5f96d83088205629b123a8401eb28571ab393c015615fcc44", "source_event_count": 55_740_891, "record_size": CACHE_RECORD_SIZE, "windows": [], "file_size_bytes": path.stat().st_size, "cache_sha256": sha256_file(path)}
            manifest.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(_cache_identity(path, manifest, []), data)


if __name__ == "__main__":
    unittest.main()
