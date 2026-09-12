from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from tools.backtest.canonical import (
    MSG_MARKET_QUOTE,
    NormalizedEvent,
    read_binary_events,
    read_csv_events,
    sort_events,
    write_binary_events,
    write_csv_events,
)
from tools.backtest.data_quality import DataQualityValidator
from tools.backtest.execution_simulator import ExecutionConfig, ExecutionSimulator
from tools.backtest.forward_returns import ForwardReturnAnalyzer, QuotePoint
from tools.backtest.pipeline import BacktestConfig, BacktestEngine
from tools.backtest.portfolio_simulator import PortfolioConfig, PortfolioSimulator
from tools.backtest.providers import AlpacaHistoricalAdapter, timestamp_to_ns
from tools.backtest.session import SessionConfig
from tools.backtest.splits import chronological_split, walk_forward_windows
from tools.backtest.types import CandidateSignal, OrderRequest


class BacktestTests(unittest.TestCase):
    def test_canonical_csv_binary_round_trip(self):
        events = [NormalizedEvent(10, MSG_MARKET_QUOTE, 0, 100, 2, 0, 1)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path, binary_path = root / "events.csv", root / "events.bin"
            write_csv_events(csv_path, events)
            write_binary_events(binary_path, events)
            parsed_csv = read_csv_events(csv_path)[0]
            self.assertEqual(parsed_csv.__dict__ | {"source": "", "source_index": 0}, events[0].__dict__)
            parsed_binary = read_binary_events(binary_path)[0]
            self.assertEqual(parsed_binary.__dict__ | {"source": "", "source_index": 0}, events[0].__dict__)

    def test_quality_reports_without_discarding_crosses(self):
        events = [
            NormalizedEvent(2, MSG_MARKET_QUOTE, 0, 110, 1, 1, 1),
            NormalizedEvent(1, MSG_MARKET_QUOTE, 0, 120, 1, 0, 2),
            NormalizedEvent(2, MSG_MARKET_QUOTE, 0, 110, 1, 1, 1),
        ]
        report = DataQualityValidator().validate(events)
        self.assertGreaterEqual(report.by_code["timestamp_out_of_order"], 1)
        self.assertGreaterEqual(report.by_code["duplicate_record"], 1)
        self.assertGreaterEqual(report.by_code["quote_cross"], 1)
        self.assertEqual(len(events), report.records)

    def test_provider_parser_is_scaffolding_only(self):
        payload = {"quotes": [{"t": "2024-01-02T14:30:00.000000Z", "bp": "100.00", "bs": 10, "ap": "100.02", "as": 12}], "trades": []}
        events = AlpacaHistoricalAdapter().parse(payload, 0)
        self.assertEqual([event.side for event in events], [0, 1])
        self.assertEqual(events[0].price, 100_000_000)
        self.assertEqual(timestamp_to_ns("1970-01-01T00:00:00Z"), 0)

    def test_session_regular_and_extended_hours(self):
        config = SessionConfig()
        regular = _timestamp("2024-01-02T14:30:00Z")
        premarket = _timestamp("2024-01-02T13:00:00Z")
        self.assertTrue(config.in_session(regular))
        self.assertFalse(config.in_session(premarket))
        self.assertTrue(replace(config, extended_hours=True).in_session(premarket))

    def test_execution_uses_executable_quote_side_and_latency(self):
        feature = type("Feature", (), {"symbol_id": 0, "bid_price": 100, "ask_price": 102, "bid_quantity": 10, "ask_quantity": 10})()
        execution = ExecutionSimulator(ExecutionConfig(latency_ns=5))
        order = OrderRequest(1, 0, "buy", "entry", "long", 1, 10, 15, 10)
        self.assertEqual(execution.submit(order, timestamp_ns=10, feature=feature, is_quote=True), [])
        self.assertEqual(execution.on_quote(14, feature), [])
        fill = execution.on_quote(15, feature)[0]
        self.assertEqual(fill.reference_price, 102)
        exit_order = OrderRequest(2, 0, "sell", "exit", "long", 1, 16, 21, 16)
        self.assertEqual(execution.submit(exit_order, timestamp_ns=21, feature=feature, is_quote=True)[0].reference_price, 100)

    def test_portfolio_costs_and_no_pyramiding(self):
        feature = type("Feature", (), {"symbol_id": 0, "bid_price": 100_000_000, "ask_price": 100_100_000, "bid_quantity": 100, "ask_quantity": 100})()
        config = PortfolioConfig(starting_cash=1_000, sizing_mode="fixed_shares", fixed_shares=2)
        portfolio = PortfolioSimulator(config)
        execution = ExecutionSimulator(ExecutionConfig(commission_per_trade=1.0))
        candidate = CandidateSignal(1, MSG_MARKET_QUOTE, 0, 1, 1, 5, 0, feature)
        portfolio.consider_candidate(candidate, execution, is_quote=True)
        self.assertIn(0, portfolio.positions)
        portfolio.consider_candidate(replace(candidate, timestamp_ns=2, sequence=2), execution, is_quote=True)
        self.assertEqual(len(portfolio.positions), 1)
        portfolio.flatten(3, {0: feature}, execution, force_immediate=True)
        self.assertEqual(len(portfolio.completed_trades), 1)
        self.assertEqual(portfolio.completed_trades[0].costs, 2.0)

    def test_hand_fixture_runs_exact_reference_path(self):
        fixture = Path(__file__).parents[1] / "fixtures" / "hand_check.csv"
        events = read_csv_events(fixture)
        mapping = {
            "num_symbols": 1,
            "strategy": {
                "strategy_enable": True,
                "long_enable": True,
                "short_enable": False,
                "long_min_momentum_bps_x100": 100,
                "long_min_vwap_delta_bps_x100": 100,
                "long_min_imbalance_q15": 1638,
                "max_spread_bps_x100": 500,
                "min_rolling_volume": 1,
                "symbol_enable": [True],
            },
        }
        run = BacktestEngine(BacktestConfig.from_mapping(mapping)).run(events)
        self.assertGreater(run.summary["candidate_count"], 0)
        self.assertGreaterEqual(run.summary["trade_count"], 1)
        self.assertEqual(run.quality.errors, 0)

    def test_forward_returns_use_future_quote_at_or_after_horizon(self):
        feature = type("Feature", (), {"bid_price": 100, "ask_price": 101})()
        candidate = CandidateSignal(10, MSG_MARKET_QUOTE, 0, 1, 1, 5, 0, feature)
        points = [QuotePoint(10, 0, 100, 101), QuotePoint(20, 0, 105, 106), QuotePoint(110, 0, 110, 111)]
        rows, summary = ForwardReturnAnalyzer({"10ns": 10}).analyze([candidate], points)
        self.assertEqual(rows[0].return_bps, (105 - 101) / 101 * 10_000)
        self.assertEqual(summary["rows"], 1)

    def test_date_splits_and_walk_forward_do_not_mix_dates(self):
        events = []
        for day in range(1, 7):
            events.append(NormalizedEvent(_timestamp(f"2024-01-{day:02d}T14:30:00Z"), MSG_MARKET_QUOTE, 0, 100, 1, 0, None))
        split = chronological_split(events, train_fraction=.5, validation_fraction=.25)
        self.assertEqual({_date(event.timestamp_ns) for event in split.train}, set(split.train_dates))
        windows = walk_forward_windows(events, train_days=2, validation_days=1, test_days=1)
        self.assertEqual(len(windows), 3)
        self.assertTrue(set(windows[0].train_dates).isdisjoint(windows[0].test_dates))


def _timestamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000_000)


def _date(value):
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).astimezone(ZoneInfo("America/New_York")).date()


if __name__ == "__main__":
    unittest.main()
