from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.backtest.canonical import MSG_MARKET_QUOTE, MSG_MARKET_TRADE
from tools.backtest.edge_diagnostic import (
    DiagnosticAccumulator,
    FutureQuote,
    MetricBook,
    Observation,
    QuotePoint,
    V3Config,
    _age_bucket,
    _book_excess,
    _cluster_book,
    _metric_values,
    _representativeness_summary,
    executable_directional_return,
    executable_quote_valid,
    first_quote_at_or_after,
    midpoint_directional_return,
    normal_quote_valid,
    run_cache_diagnostic,
    validate_forward_return_fixtures,
    zero_move_executable_baseline,
)
from tools.backtest.research_cache import (
    CACHE_RECORD_SIZE,
    VALID_FEATURE,
    VALID_IMBALANCE,
    VALID_MIDPOINT,
    VALID_SPREAD,
    VALID_SPREAD_BPS,
    CacheRecord,
    FAST_WINDOWS,
    pack_cache_record,
)


VALID = VALID_FEATURE | VALID_MIDPOINT | VALID_SPREAD | VALID_SPREAD_BPS | VALID_IMBALANCE


def quote(timestamp: int, *, symbol: int = 0, bid: int = 99_000_000, ask: int = 101_000_000, side: int = 0, segment: int = 0, measured: bool = True) -> CacheRecord:
    midpoint = (bid + ask) // 2
    return CacheRecord(timestamp, MSG_MARKET_QUOTE, symbol, measured, segment, 0, side, midpoint, 1, timestamp + 1, bid, ask, 10, 10, midpoint, ask - bid, midpoint, 1, 0, 0, 0, max(0, (ask - bid) * 100_000_000 // max(1, midpoint)), VALID)


def trade(timestamp: int, *, symbol: int = 0, segment: int = 0) -> CacheRecord:
    return CacheRecord(timestamp, MSG_MARKET_TRADE, symbol, True, segment, 0, 0, 100_000_000, 10, timestamp + 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, VALID_FEATURE)


class EdgeDiagnosticTests(unittest.TestCase):
    def test_forward_fixture_validation_passes(self):
        result = validate_forward_return_fixtures()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["cases"]), 14)

    def test_long_and_short_midpoint_return(self):
        self.assertEqual(midpoint_directional_return(100, 102, "long"), 200.0)
        self.assertEqual(midpoint_directional_return(100, 102, "short"), -200.0)

    def test_long_and_short_executable_return(self):
        self.assertAlmostEqual(executable_directional_return(99, 101, 101, 103, "long"), 0.0)
        self.assertAlmostEqual(executable_directional_return(99, 101, 97, 99, "short"), 0.0)

    def test_spread_decomposition_and_zero_move_baseline(self):
        future = FutureQuote(1, 99, 101, 100, 2)
        observation = Observation(1, "candidate", 0, 0, 0, "long", "day", "window", 99, 101, 100, 2, 0)
        values = _metric_values(observation, future, "long")
        self.assertAlmostEqual(values["midpoint_return_bps"], 0.0)
        self.assertAlmostEqual(values["executable_return_bps"], -198.01980198019803)
        self.assertAlmostEqual(values["crossing_cost_estimate_bps"], 200.0)
        self.assertAlmostEqual(zero_move_executable_baseline(100, 99, 101, 99, 101, "long"), values["executable_return_bps"])

    def test_matched_control_has_no_future_selection(self):
        acc = DiagnosticAccumulator(FAST_WINDOWS)
        self.assertEqual(acc.control_reservoir.seen, {})
        self.assertIsNone(first_quote_at_or_after((QuotePoint(999, 99, 101, 100),), 1_000))
        selected = first_quote_at_or_after((QuotePoint(1_001, 99, 101, 100),), 1_000)
        self.assertEqual(selected.timestamp_ns, 1_001)

    def test_candidate_excess_returns_are_candidate_minus_control(self):
        candidate = MetricBook()
        control = MetricBook()
        values = {"midpoint_return_bps": 2.0, "executable_return_bps": -1.0, "entry_spread_bps": 1.0, "future_spread_bps": 1.0, "crossing_cost_estimate_bps": 2.0, "midpoint_minus_executable_bps": 3.0, "zero_move_executable_bps": -2.0}
        candidate.observe("long", 1_000_000_000, values)
        control.observe("long", 1_000_000_000, {**values, "midpoint_return_bps": 1.0, "executable_return_bps": -2.0})
        result = _book_excess(candidate.summary(), control.summary())
        self.assertAlmostEqual(result["1s"]["aggregate"]["excess_midpoint_return_bps"], 1.0)
        self.assertAlmostEqual(result["1s"]["aggregate"]["excess_executable_return_bps"], 1.0)

    def test_quote_age_buckets(self):
        self.assertEqual(_age_bucket(0), "<1ms")
        self.assertEqual(_age_bucket(1_000_000), "1-10ms")
        self.assertEqual(_age_bucket(10_000_000), "10-100ms")
        self.assertEqual(_age_bucket(100_000_000), "100ms-1s")
        self.assertEqual(_age_bucket(1_000_000_000), ">1s")

    def test_paired_bid_ask_timestamp_handling_and_ordering(self):
        records = [quote(0, side=0), quote(0, side=1), trade(0)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.bin"
            path.write_bytes(b"".join(pack_cache_record(record) for record in records))
            acc, _ = run_cache_diagnostic(path, FAST_WINDOWS, total_events=len(records), progress_label="test", max_records=None)
        self.assertEqual(acc.processed_records, 3)
        self.assertEqual(acc.paired_quote_groups, 1)
        self.assertEqual(acc.bid_then_ask_pairs, 1)
        self.assertEqual(acc.trade_quote_mixed_groups, 1)
        self.assertEqual(acc.sequence_regressions, 0)

    def test_cluster_first_selection(self):
        observations = []
        for index, timestamp in enumerate((0, 100, 10_000_000_000)):
            observation = Observation(index + 1, "candidate", timestamp, 0, 0, "long", "day", "window", 99, 101, 100, 2, 0, score=index)
            observation.futures[1_000_000_000] = FutureQuote(timestamp + 1_000_000_000, 100, 102, 101, 2)
            observations.append(observation)
        book, cluster_count = _cluster_book(observations, 1_000_000_000)
        self.assertEqual(cluster_count, 2)
        self.assertEqual(book.summary()["aggregate"]["1s"]["midpoint_return_bps"]["count"], 2)

    def test_fast_medium_statistic_containers_are_independent(self):
        fast = MetricBook()
        medium = MetricBook()
        values = {"midpoint_return_bps": 1.0, "executable_return_bps": 0.0, "entry_spread_bps": 1.0, "future_spread_bps": 1.0, "crossing_cost_estimate_bps": 2.0, "midpoint_minus_executable_bps": 1.0, "zero_move_executable_bps": -2.0}
        fast.observe("long", 1_000_000_000, values)
        medium.observe("long", 1_000_000_000, {**values, "midpoint_return_bps": 3.0})
        self.assertNotEqual(fast.summary()["aggregate"]["1s"]["midpoint_return_bps"]["mean"], medium.summary()["aggregate"]["1s"]["midpoint_return_bps"]["mean"])

    def test_fast_medium_representativeness_comparison_is_descriptive(self):
        def summary(spread_mean, activity_mean, volatility_mean, midpoint_mean):
            return {
                "spread_by_symbol": {"SPY": {"mean": spread_mean}},
                "activity_ratio_q8": {"mean": activity_mean},
                "volatility_bps_x100": {"mean": volatility_mean},
                "measured_quote_records": 100,
                "valid_executable_quote_records": 99,
                "random_returns": {"aggregate": {"1s": {"midpoint_return_bps": {"mean": midpoint_mean}}}},
            }

        result = _representativeness_summary(summary(1.0, 100.0, 1_000.0, 0.0), summary(1.1, 110.0, 1_100.0, 0.5))
        self.assertTrue(result["fast_appears_representative"])
        self.assertEqual(result["interpretation"], "descriptive comparison only; MEDIUM was not used for parameter tuning or promotion")

    def test_quote_validity_distinguishes_locked_and_crossed(self):
        self.assertTrue(executable_quote_valid(100, 100))
        self.assertFalse(normal_quote_valid(100, 100))
        self.assertFalse(executable_quote_valid(101, 100))

    def test_fixture_record_size_is_fixed_width(self):
        self.assertEqual(len(pack_cache_record(quote(0))), CACHE_RECORD_SIZE)


if __name__ == "__main__":
    unittest.main()
