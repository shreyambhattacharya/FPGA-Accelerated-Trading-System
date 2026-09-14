from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

from tools.backtest.canonical import (
    MSG_MARKET_QUOTE,
    MSG_MARKET_TRADE,
    NormalizedEvent,
    iter_binary_field_tuples,
    read_binary_events,
    read_csv_events,
    sort_events,
    write_binary_events,
    write_csv_events,
)
from tools.backtest.data_quality import DataQualityValidator
from tools.backtest.execution_simulator import ExecutionConfig, ExecutionSimulator
from tools.backtest.forward_returns import ForwardReturnAnalyzer, QuotePoint
from tools.backtest.arrival import combined_timestamp_analysis, event_rate_analysis, interarrival_analysis, percentile, simulate_serialized_capacity
from tools.backtest.pipeline import BacktestConfig, BacktestEngine
from tools.backtest.portfolio_simulator import PortfolioConfig, PortfolioSimulator
from tools.backtest.providers import AlpacaDownloadError, AlpacaHistoricalAdapter, AlpacaHistoricalDownloader, timestamp_to_ns
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

    def test_buffered_canonical_field_reader_matches_packet_fields(self):
        events = [
            NormalizedEvent(10, MSG_MARKET_QUOTE, 2, 100_000_000, 7, 1, 9, flags=3),
            NormalizedEvent(20, MSG_MARKET_TRADE, 1, 100_100_000, 5, 0, 8, flags=4),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.bin"
            write_binary_events(path, events)
            fields = list(iter_binary_field_tuples(path, chunk_packets=1))
            self.assertEqual(fields[0][0], 0)
            self.assertEqual(fields[0][1][3:9], (10, 100_000_000, 7, 1, 9, 3))
            self.assertEqual(fields[1][0], 1)
            self.assertEqual(fields[1][1][1], MSG_MARKET_TRADE)

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
        self.assertEqual(events[0].quantity, 1000)
        trades = AlpacaHistoricalAdapter().parse({"trades": [{"t": "2024-01-02T14:30:00.000000Z", "p": "100.00", "s": 7}]}, 0)
        self.assertEqual(trades[0].quantity, 7)
        self.assertEqual(timestamp_to_ns("1970-01-01T00:00:00Z"), 0)
        self.assertEqual(timestamp_to_ns("2024-01-02T14:30:00.123456789Z"), 1704205800123456789)

    def test_alpaca_pagination_and_repeated_token_detection(self):
        responses = [
            _FakeResponse({"quotes": [{"t": "2024-01-02T14:30:00Z"}, {"t": "2024-01-02T14:30:01Z"}], "next_page_token": "page-1"}),
            _FakeResponse({"quotes": [{"t": "2024-01-02T14:30:02Z"}]})
        ]
        downloader = AlpacaHistoricalDownloader(feed="iex", api_key="unit-key", api_secret="unit-secret", page_limit=2, opener=lambda request, timeout: responses.pop(0), sleep_fn=lambda _: None)
        records, summary = downloader.fetch_pages(symbol="SPY", kind="quotes", start="2024-01-02T14:30:00Z", end="2024-01-02T20:00:00Z")
        self.assertEqual(len(records), 3)
        self.assertEqual(summary.pages_completed, 2)
        self.assertEqual(summary.status_summary, {"200": 2})

        repeated = [_FakeResponse({"quotes": [{"t": "2024-01-02T14:30:00Z"}], "next_page_token": "same"}), _FakeResponse({"quotes": [{"t": "2024-01-02T14:30:01Z"}], "next_page_token": "same"})]
        downloader = AlpacaHistoricalDownloader(api_key="unit-key", api_secret="unit-secret", opener=lambda request, timeout: repeated.pop(0), sleep_fn=lambda _: None)
        with self.assertRaises(AlpacaDownloadError):
            downloader.fetch_pages(symbol="SPY", kind="quotes", start="a", end="b")

    def test_alpaca_retry_and_permanent_http_errors_are_bounded(self):
        transient = [HTTPError("https://example.invalid", 429, "rate", {}, None), _FakeResponse({"trades": []})]
        downloader = AlpacaHistoricalDownloader(api_key="unit-key", api_secret="unit-secret", opener=lambda request, timeout: (_raise(transient.pop(0)) if isinstance(transient[0], HTTPError) else transient.pop(0)), sleep_fn=lambda _: None)
        records, summary = downloader.fetch_pages(symbol="SPY", kind="trades", start="a", end="b")
        self.assertEqual(records, [])
        self.assertEqual(summary.retry_count, 1)
        permanent = AlpacaHistoricalDownloader(api_key="unit-key", api_secret="unit-secret", opener=lambda request, timeout: _raise(HTTPError("https://example.invalid", 401, "auth", {}, None)), sleep_fn=lambda _: None)
        with self.assertRaisesRegex(AlpacaDownloadError, "status 401"):
            permanent.fetch_pages(symbol="SPY", kind="trades", start="a", end="b")

    def test_atomic_download_does_not_leave_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "quotes.json"
            downloader = AlpacaHistoricalDownloader(api_key="unit-key", api_secret="unit-secret", opener=lambda request, timeout: _raise(OSError("network")), max_retries=0, sleep_fn=lambda _: None)
            with self.assertRaises(AlpacaDownloadError):
                downloader.download_to(output, symbol="SPY", kind="quotes", start="a", end="b")
            self.assertFalse(output.exists())
            self.assertFalse((Path(directory) / "quotes.json.part").exists())

    def test_session_regular_and_extended_hours(self):
        config = SessionConfig()
        regular = _timestamp("2024-01-02T14:30:00Z")
        premarket = _timestamp("2024-01-02T13:00:00Z")
        labor_day = _timestamp("2026-09-07T14:30:00Z")
        self.assertTrue(config.in_session(regular))
        self.assertFalse(config.in_session(premarket))
        self.assertFalse(config.in_session(labor_day))
        self.assertTrue(replace(config, extended_hours=True).in_session(premarket))

    def test_event_rates_interarrival_capacity_and_percentiles(self):
        events = [
            NormalizedEvent(0, MSG_MARKET_QUOTE, 0, 100, 1, 0, 1, source_index=0),
            NormalizedEvent(1_000_000, MSG_MARKET_QUOTE, 1, 100, 1, 0, 1, source_index=0),
            NormalizedEvent(2_000_000, MSG_MARKET_QUOTE, 0, 100, 1, 1, 2, source_index=1),
            NormalizedEvent(2_000_000, MSG_MARKET_TRADE, 1, 100, 1, 0, 2, source_index=1),
        ]
        rates = event_rate_analysis(events)
        self.assertEqual(rates["1ms"]["combined"]["maximum_events_per_second"], 2000.0)
        arrival = interarrival_analysis(events, service_cycles=1, clock_hz=1_000)
        self.assertEqual(arrival["groups"]["combined"]["min"], 0)
        self.assertEqual(arrival["groups"]["combined"]["fraction_within_service_interval"], 1.0)
        capacity = simulate_serialized_capacity(events, service_cycles=1, clock_hz=1_000, depths=(1,))
        self.assertEqual(capacity["finite_depths"]["1"]["overflow_events"], 1)
        self.assertEqual(percentile([1, 2, 3, 4], .75), 3)

    def test_combined_timestamp_analysis_resumes_from_checkpoint(self):
        events = [
            NormalizedEvent(0, MSG_MARKET_QUOTE, 0, 100, 1, 0, 1),
            NormalizedEvent(1_000_000, MSG_MARKET_QUOTE, 0, 100, 1, 1, 2),
            NormalizedEvent(2_000_000, MSG_MARKET_TRADE, 1, 100, 1, 0, 1),
            NormalizedEvent(3_000_000, MSG_MARKET_QUOTE, 1, 100, 1, 1, 2),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "diagnostics.pkl"
            state = root / "analysis_state.json"
            identity = {"dataset_id": "fixture", "sha256": "fixture", "size_bytes": 128, "event_count": len(events)}

            def interrupted_factory(start):
                for index in range(start, len(events)):
                    if start == 0 and index == 3:
                        raise RuntimeError("intentional interruption")
                    yield events[index]

            with self.assertRaisesRegex(RuntimeError, "intentional interruption"):
                combined_timestamp_analysis(
                    interrupted_factory,
                    total_events=len(events),
                    checkpoint_path=checkpoint,
                    state_manifest_path=state,
                    input_identity=identity,
                    checkpoint_event_interval=2,
                )
            self.assertTrue(checkpoint.exists())
            self.assertEqual(json.loads(state.read_text())["processed_events"], 3)

            result = combined_timestamp_analysis(
                lambda start: iter(events[start:]),
                total_events=len(events),
                checkpoint_path=checkpoint,
                state_manifest_path=state,
                input_identity=identity,
                checkpoint_event_interval=2,
            )
            self.assertEqual(result["processed_events"], len(events))
            self.assertEqual(result["aggregate"]["total_events"], len(events))
            self.assertEqual(result["by_symbol"]["0"]["total_events"], 2)
            self.assertEqual(json.loads(state.read_text())["phase"], "combined_timestamp_analysis_complete")

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


class _FakeResponse:
    status = 200
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload
    def getcode(self):
        return self.status


def _raise(error):
    raise error


def _date(value):
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).astimezone(ZoneInfo("America/New_York")).date()


if __name__ == "__main__":
    unittest.main()
