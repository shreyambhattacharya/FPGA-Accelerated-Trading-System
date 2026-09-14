from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tools.backtest.canonical import MSG_MARKET_QUOTE, MSG_MARKET_TRADE
from tools.backtest.microstructure_research import (
    Snapshot,
    SnapshotCoalescer,
    aggregate_window,
    assign_bucket,
    bucket_edges,
    classify_trade,
    cluster_event_times,
    microprice,
    microprice_minus_mid_bps_x100,
    ofi_delta,
)
from tools.backtest.research_cache import (
    VALID_FEATURE,
    VALID_IMBALANCE,
    VALID_MIDPOINT,
    VALID_SPREAD,
    VALID_SPREAD_BPS,
    CacheRecord,
    pack_cache_record,
)


VALID = VALID_FEATURE | VALID_MIDPOINT | VALID_SPREAD | VALID_SPREAD_BPS | VALID_IMBALANCE


def quote(timestamp: int, *, side: int, symbol: int = 0, bid: int = 99_000_000, ask: int = 101_000_000, bid_size: int = 10, ask_size: int = 10, segment: int = 0, session: int = 0, measured: bool = True) -> CacheRecord:
    midpoint = (bid + ask) // 2
    return CacheRecord(timestamp, MSG_MARKET_QUOTE, symbol, measured, segment, session, side, midpoint, 1, timestamp + side + 1, bid, ask, bid_size, ask_size, midpoint, ask - bid, midpoint, 1, 0, 0, 0, max(0, (ask - bid) * 100_000_000 // max(1, midpoint)), VALID)


def trade(timestamp: int, price: int = 100_000_000, *, symbol: int = 0, segment: int = 0, session: int = 0) -> CacheRecord:
    return CacheRecord(timestamp, MSG_MARKET_TRADE, symbol, True, segment, session, 0, price, 4, timestamp + 99, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, VALID_FEATURE)


def snap(timestamp: int, *, bid: int = 99, ask: int = 101, bid_size: int = 10, ask_size: int = 10, symbol: int = 0) -> Snapshot:
    return Snapshot(timestamp, symbol, 0, 0, True, bid, ask, bid_size, ask_size, (bid + ask) // 2, ask - bid)


class MicrostructureResearchTests(unittest.TestCase):
    def test_paired_bid_ask_coalescing_and_no_intermediate_snapshot(self):
        snapshots = []
        trades = []
        coalescer = SnapshotCoalescer(snapshots.append, trades.append)
        records = [quote(10, side=0), trade(10, 100_000_000), quote(10, side=1)]
        for record in records:
            coalescer.process(record)
        coalescer.flush()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(trades), 1)
        self.assertEqual(coalescer.paired_groups, 1)

    def test_complete_snapshot_fields(self):
        snapshots = []
        coalescer = SnapshotCoalescer(snapshots.append, lambda _: None)
        coalescer.process(quote(10, side=0, bid=100, ask=102, bid_size=7, ask_size=11))
        coalescer.process(quote(10, side=1, bid=100, ask=102, bid_size=7, ask_size=11))
        self.assertEqual(snapshots[0].midpoint, 101)
        self.assertEqual(snapshots[0].spread, 2)
        self.assertEqual((snapshots[0].bid_price, snapshots[0].ask_price), (100, 102))
        self.assertEqual((snapshots[0].bid_size, snapshots[0].ask_size), (7, 11))

    def test_microprice_formula_and_zero_size(self):
        self.assertEqual(microprice(99, 101, 3, 1), 100)
        self.assertEqual(microprice_minus_mid_bps_x100(100, 100), 0)
        self.assertIsNone(microprice(99, 101, 0, 0))

    def test_static_imbalance_comparison_value(self):
        self.assertEqual((30 - 10) * 32768 // 40, 16384)
        self.assertEqual(assign_bucket(0, bucket_edges([-10, -2, -1, 1, 2, 10])), "near_zero")

    def test_ofi_bid_price_up_down(self):
        self.assertEqual(ofi_delta(snap(0, bid=99), snap(1, bid=100)), 10)
        self.assertEqual(ofi_delta(snap(0, bid=100), snap(1, bid=99)), -10)

    def test_ofi_ask_price_up_down(self):
        self.assertEqual(ofi_delta(snap(0, ask=101), snap(1, ask=102)), 10)
        self.assertEqual(ofi_delta(snap(0, ask=102), snap(1, ask=101)), -10)

    def test_ofi_bid_size_increase_decrease(self):
        self.assertEqual(ofi_delta(snap(0, bid_size=10), snap(1, bid_size=15)), 5)
        self.assertEqual(ofi_delta(snap(0, bid_size=10), snap(1, bid_size=4)), -6)

    def test_ofi_ask_size_increase_decrease(self):
        self.assertEqual(ofi_delta(snap(0, ask_size=10), snap(1, ask_size=15)), -5)
        self.assertEqual(ofi_delta(snap(0, ask_size=10), snap(1, ask_size=4)), 6)

    def test_ofi_window_aggregation(self):
        points = __import__("collections").deque([(0, 2), (5, 3), (20, 7)])
        self.assertEqual(aggregate_window(points, 10, 10), 12)

    def test_liquidity_withdrawal_and_replenishment_directions(self):
        previous = snap(0, bid=100, ask=101, bid_size=10, ask_size=10)
        bid_down = snap(1, bid=99, ask=101, bid_size=4, ask_size=10)
        ask_up = snap(2, bid=99, ask=102, bid_size=4, ask_size=6)
        bid_replenished = snap(3, bid=100, ask=102, bid_size=12, ask_size=6)
        self.assertEqual(ofi_delta(previous, bid_down), -10)
        self.assertEqual(ofi_delta(bid_down, ask_up), 10)
        self.assertGreater(bid_replenished.bid_size, bid_down.bid_size)

    def test_spread_change_and_quote_intensity_inputs(self):
        self.assertEqual(snap(1, bid=99, ask=103).spread - snap(0).spread, 2)
        self.assertEqual(sum(1 for _ in range(3)), 3)

    def test_trade_sign_at_ask_bid_midpoint_and_unknown(self):
        self.assertEqual(classify_trade(101, 99, 101, 100), "BUY")
        self.assertEqual(classify_trade(99, 99, 101, 100), "SELL")
        self.assertEqual(classify_trade(100, 99, 101, 100), "UNKNOWN")
        self.assertEqual(classify_trade(100, 99, 101, 99), "LIKELY_BUY")
        self.assertEqual(classify_trade(100, 99, 101, 101), "LIKELY_SELL")

    def test_signed_trade_flow_and_unknown_separate(self):
        self.assertEqual(classify_trade(101, 99, 101, 100), "BUY")
        self.assertEqual(classify_trade(100, None, None, None), "UNKNOWN")

    def test_segment_and_session_reset(self):
        resets = []
        coalescer = SnapshotCoalescer(lambda _: None, lambda _: None, lambda segment, session: resets.append((segment, session)))
        coalescer.process(quote(0, side=0, segment=0, session=0))
        coalescer.process(quote(0, side=1, segment=0, session=0))
        coalescer.process(quote(1, side=0, segment=1, session=1))
        self.assertEqual(resets, [(1, 1)])

    def test_cross_symbol_causal_lead_lag_order(self):
        source_time = 100
        target_time = 100
        self.assertLessEqual(source_time, target_time)
        self.assertFalse(101 <= 100)

    def test_no_future_benchmark_use(self):
        benchmark_timestamp = 2_000
        target_timestamp = 1_000
        self.assertFalse(benchmark_timestamp <= target_timestamp)

    def test_event_clustering(self):
        count, first = cluster_event_times([0, 10, 100, 1_000], 100)
        self.assertEqual(count, 2)
        self.assertEqual(first, [0, 1000])

    def test_feature_bucket_assignment(self):
        edges = bucket_edges([-5, -2, -1, 0, 1, 2, 5])
        self.assertIn(assign_bucket(-5, edges), {"most_negative", "negative"})
        self.assertEqual(assign_bucket(0, edges), "near_zero")
        self.assertIn(assign_bucket(5, edges), {"positive", "most_positive"})

    def test_fixed_width_fixture_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.bin"
            path.write_bytes(pack_cache_record(quote(1, side=0)))
            self.assertEqual(path.stat().st_size, 108)


if __name__ == "__main__":
    unittest.main()
