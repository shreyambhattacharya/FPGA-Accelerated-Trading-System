"""Bounded-memory JSON/canonical replay helpers for large historical studies."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from heapq import merge
import heapq
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_model"
if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from market_model import MSG_MARKET_QUOTE, MarketModel  # noqa: E402
from strategy_model import StrategyModel  # noqa: E402

from .canonical import NormalizedEvent
from .data_quality import DataQualityReport
from .execution_simulator import ExecutionSimulator
from .feature_analysis import StreamingCandidateAccumulator, StreamingFeatureAccumulator
from .forward_returns import ForwardReturnAnalyzer, ForwardReturnRow
from .metrics import compute_metrics
from .portfolio_simulator import PortfolioSimulator
from .types import CandidateSignal


def iter_json_array(path: Path, key: str, *, chunk_size: int = 1024 * 1024):
    """Yield objects from one top-level JSON array without loading the file."""

    decoder = json.JSONDecoder()
    with path.open(encoding="utf-8") as source:
        buffer = ""
        position = 0
        array_open = False
        eof = False
        while not array_open:
            if eof:
                raise ValueError(f"provider file lacks {key} array")
            buffer += source.read(chunk_size)
            eof = len(buffer) < chunk_size
            marker = buffer.find(f'"{key}"')
            if marker >= 0:
                opening = buffer.find("[", marker)
                if opening >= 0:
                    position = opening + 1
                    array_open = True
            if eof and not array_open:
                raise ValueError(f"provider file lacks {key} array")

        while True:
            while True:
                while position < len(buffer) and buffer[position] in " \t\r\n,":
                    position += 1
                if position < len(buffer):
                    break
                if eof:
                    raise ValueError(f"unterminated {key} array")
                buffer = buffer[position:] + source.read(chunk_size)
                position = 0
                eof = len(buffer) < chunk_size
            if buffer[position] == "]":
                return
            try:
                value, next_position = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                if eof:
                    raise ValueError(f"malformed {key} record")
                buffer += source.read(chunk_size)
                eof = len(buffer) - position < chunk_size
                continue
            if not isinstance(value, dict):
                raise ValueError(f"malformed {key} record")
            yield value
            position = next_position
            if position >= chunk_size:
                buffer = buffer[position:]
                position = 0


def iter_normalized_partition(path_map: dict, day_text: str, symbol: str, symbol_id: int):
    """Merge one day's quote and trade arrays into canonical events."""

    def source_events(kind: str):
        from .providers import AlpacaHistoricalAdapter, price_to_micro, timestamp_to_ns

        for source_index, record in enumerate(iter_json_array(path_map[(day_text, symbol, kind)], kind)):
            timestamp_ns = timestamp_to_ns(record.get("t", record.get("timestamp", record.get("time"))))
            if kind == "trades":
                yield NormalizedEvent(
                    timestamp_ns=timestamp_ns,
                    event_type=2,
                    symbol_id=symbol_id,
                    price=price_to_micro(record.get("p", record.get("price"))),
                    quantity=int(record.get("s", record.get("size", record.get("quantity")))),
                    side=AlpacaHistoricalAdapter._side(record.get("side", record.get("taker_side", 0))),
                    sequence=None,
                    flags=0,
                    source_index=source_index,
                    source=f"alpaca://iex/{day_text}/{symbol}/{kind}",
                )
                continue
            bid_price = record.get("bp", record.get("bid_price", record.get("bid")))
            bid_size = record.get("bs", record.get("bid_size", record.get("bid_quantity")))
            ask_price = record.get("ap", record.get("ask_price", record.get("ask")))
            ask_size = record.get("as", record.get("ask_size", record.get("ask_quantity")))
            yield NormalizedEvent(timestamp_ns, 1, symbol_id, price_to_micro(bid_price), int(bid_size) * 100, 0, None, source_index=source_index, source=f"alpaca://iex/{day_text}/{symbol}/{kind}")
            yield NormalizedEvent(timestamp_ns, 1, symbol_id, price_to_micro(ask_price), int(ask_size) * 100, 1, None, source_index=source_index, source=f"alpaca://iex/{day_text}/{symbol}/{kind}")

    def event_key(event):
        return event.timestamp_ns, event.symbol_id, event.source_index, event.event_type, event.side

    return merge(source_events("quotes"), source_events("trades"), key=event_key)


def iter_all_normalized(path_map: dict, dates, symbol_ids: dict[str, int]):
    partitions = [
        iter_normalized_partition(path_map, day_text, symbol, symbol_ids[symbol])
        for day_text in dates
        for symbol in symbol_ids
    ]
    return merge(*partitions, key=lambda event: (event.timestamp_ns, event.symbol_id, event.source_index, event.event_type, event.side))


def iter_replay_sequences(events, *, timezone_name="America/New_York"):
    zone = ZoneInfo(timezone_name)
    counters = {}
    for event in events:
        session = datetime.fromtimestamp(event.timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(zone).date()
        key = (session, event.symbol_id)
        counters[key] = counters.get(key, 0) + 1
        yield event.with_sequence(counters[key])


@dataclass
class StreamingReplayResult:
    config: object
    candidates: list[CandidateSignal]
    candidate_logs: list
    trades: list
    equity_curve: list
    forward_rows: list[ForwardReturnRow]
    forward_summary: dict
    summary: dict
    unfilled_orders: list
    quality: DataQualityReport
    feature_distributions: dict
    candidate_summary: dict


class _ForwardAccumulator:
    def __init__(self):
        self.analyzer = ForwardReturnAnalyzer()
        self.last_quotes = {}
        self.pending = defaultdict(list)
        self._pending_id = 0
        self.rows = []

    def observe_quote(self, timestamp_ns, feature):
        self.last_quotes[feature.symbol_id] = (timestamp_ns, feature.bid_price, feature.ask_price)
        pending = self.pending[feature.symbol_id]
        retry = []
        while pending and pending[0][0] <= timestamp_ns:
            _, _, item = heapq.heappop(pending)
            if item["entry_price"] is None:
                self.rows.append(ForwardReturnRow(item["timestamp_ns"], item["symbol_id"], item["direction"], item["score"], item["reason_bits"], item["horizon"], item["horizon_ns"], None))
                continue
            exit_price = feature.bid_price if item["direction"] == "long" else feature.ask_price
            if exit_price <= 0:
                retry.append(item)
                continue
            entry = item["entry_price"]
            result = ((exit_price - entry) if item["direction"] == "long" else (entry - exit_price)) / entry * 10_000.0
            self.rows.append(ForwardReturnRow(item["timestamp_ns"], item["symbol_id"], item["direction"], item["score"], item["reason_bits"], item["horizon"], item["horizon_ns"], result))
        for item in retry:
            self._pending_id += 1
            heapq.heappush(pending, (item["target_ns"], self._pending_id, item))

    def observe_candidate(self, candidate):
        direction = "long" if candidate.action == 1 else "short"
        quote = self.last_quotes.get(candidate.symbol_id)
        entry_price = None
        if quote is not None and quote[0] <= candidate.timestamp_ns:
            entry_price = quote[2] if direction == "long" else quote[1]
            if entry_price <= 0:
                entry_price = None
        for horizon, horizon_ns in self.analyzer.horizons.items():
            item = {
                "timestamp_ns": candidate.timestamp_ns,
                "symbol_id": candidate.symbol_id,
                "direction": direction,
                "score": candidate.score,
                "reason_bits": candidate.reason_bits,
                "horizon": horizon,
                "horizon_ns": horizon_ns,
                "target_ns": candidate.timestamp_ns + horizon_ns,
                "entry_price": entry_price,
            }
            self._pending_id += 1
            heapq.heappush(self.pending[candidate.symbol_id], (item["target_ns"], self._pending_id, item))

    def finish(self):
        for pending in self.pending.values():
            while pending:
                _, _, item = heapq.heappop(pending)
                self.rows.append(ForwardReturnRow(item["timestamp_ns"], item["symbol_id"], item["direction"], item["score"], item["reason_bits"], item["horizon"], item["horizon_ns"], None))
        self.pending.clear()
        return self.rows, self.analyzer._summarize(self.rows)


def run_streaming_replay(
    events,
    config,
    *,
    strategy_factory=None,
    presequenced=False,
    progress_callback=None,
    progress_total_events=None,
    progress_event_interval=1_000_000,
    progress_time_seconds=30.0,
) -> StreamingReplayResult:
    """Replay the exact models while retaining only actionable candidates.

    ``presequenced`` is used by the real-study binary path after the
    normalizer has assigned per-session sequences. It avoids recomputing a
    timezone conversion and creating a replacement event for every record;
    the session boundary is still detected from the deterministic sequence
    reset in the normalized stream.
    """

    market = MarketModel(num_symbols=config.num_symbols, trade_window=config.trade_window, momentum_window=config.momentum_window, vwap_window=config.vwap_window)
    strategy = strategy_factory(config.num_symbols, config.strategy) if strategy_factory is not None else StrategyModel(num_symbols=config.num_symbols, config=config.strategy)
    execution = ExecutionSimulator(config.execution)
    portfolio = PortfolioSimulator(config.portfolio, record_candidate_logs=False)
    feature_accumulator = StreamingFeatureAccumulator()
    candidate_accumulator = StreamingCandidateAccumulator(timezone_name=config.session.timezone_name)
    forward = _ForwardAccumulator()
    last_features = {}
    previous_session = None
    last_timestamp_ns = 0
    input_events = 0
    accepted_features = 0
    rejected = 0
    unfilled_count = 0
    sequence_counters = {}
    seen_symbols = set()
    session_index = 0
    progress_started = time.perf_counter()
    progress_last_time = progress_started
    progress_last_events = 0

    def report_progress(force=False):
        nonlocal progress_last_time, progress_last_events
        if progress_callback is None:
            return
        now = time.perf_counter()
        if not force and input_events - progress_last_events < progress_event_interval and now - progress_last_time < progress_time_seconds:
            return
        elapsed = now - progress_started
        rate = input_events / elapsed if elapsed else 0.0
        remaining = ((progress_total_events - input_events) / rate) if progress_total_events is not None and rate > 0 else None
        progress_callback({
            "processed_events": input_events,
            "total_events": progress_total_events,
            "percent_complete": (input_events / progress_total_events * 100.0) if progress_total_events else None,
            "elapsed_time": elapsed,
            "processing_rate_events_per_second": rate,
            "estimated_remaining_time": remaining,
        })
        progress_last_events = input_events
        progress_last_time = now

    def close_session(timestamp_ns):
        nonlocal unfilled_count
        if last_features:
            if portfolio.config.end_of_day_flatten:
                portfolio.flatten(timestamp_ns, last_features, execution, force_immediate=True)
            portfolio.mark_to_market(timestamp_ns, next(iter(last_features.values())), record=True)
        unfilled_count += len(execution.flush())
        portfolio.pending_symbols.clear()
        portfolio._pending_order_ids.clear()

    for raw_event in events:
        input_events += 1
        report_progress()
        if presequenced and raw_event.sequence is not None:
            if previous_session is not None and raw_event.sequence == 1 and raw_event.symbol_id in seen_symbols:
                session_index += 1
                seen_symbols.clear()
            session = session_index
            seen_symbols.add(raw_event.symbol_id)
        else:
            session = config.session.session_date(raw_event.timestamp_ns)
        if previous_session is not None and session != previous_session:
            close_session(last_timestamp_ns)
            market.reset_state()
            strategy.reset_state()
            last_features.clear()
        previous_session = session
        last_timestamp_ns = raw_event.timestamp_ns
        if presequenced and raw_event.sequence is not None:
            event = raw_event
        else:
            sequence_key = (session, raw_event.symbol_id)
            sequence_counters[sequence_key] = sequence_counters.get(sequence_key, 0) + 1
            event = raw_event.with_sequence(sequence_counters[sequence_key])
        # NormalizedEvent exposes the reference model's ``message_type``
        # alias, so the hot path can avoid constructing a second dataclass.
        outcome = market.process_event(event)
        if outcome.kind != "feature" or outcome.feature is None:
            rejected += 1
            continue
        accepted_features += 1
        feature = outcome.feature
        last_features[event.symbol_id] = feature
        is_quote = event.event_type == MSG_MARKET_QUOTE
        if is_quote:
            forward.observe_quote(event.timestamp_ns, feature)
            portfolio.on_quote(event.timestamp_ns, feature, execution)
        portfolio.evaluate_risk(event.timestamp_ns, feature, execution, is_quote=is_quote)
        signal = strategy.evaluate(feature)
        candidate = CandidateSignal(event.timestamp_ns, event.event_type, signal.symbol_id, signal.sequence, signal.action, signal.score, signal.reason_bits, feature)
        feature_accumulator.observe(candidate)
        candidate_accumulator.observe(candidate)
        if candidate.action != 0:
            forward.observe_candidate(candidate)
        portfolio.consider_candidate(candidate, execution, is_quote=is_quote)
        portfolio.mark_to_market(event.timestamp_ns, feature, record=False)

    if last_features:
        close_session(last_timestamp_ns)
    else:
        unfilled_count += len(execution.flush())
    report_progress(force=True)
    forward_rows, forward_summary = forward.finish()
    summary = compute_metrics(portfolio.completed_trades, portfolio.equity_curve, starting_cash=config.portfolio.starting_cash, timezone_name=config.session.timezone_name)
    summary.update({
        "input_events": input_events,
        "session_events": input_events,
        "accepted_features": accepted_features,
        "rejected_events": rejected,
        "candidate_count": candidate_accumulator.to_dict()["candidate_count"],
        "quality": {"records": input_events, "errors": 0, "warnings": 0, "by_code": {}, "issues": []},
        "unfilled_orders": unfilled_count,
        "streaming_history": True,
    })
    summary["max_drawdown"] = min(summary.get("max_drawdown", 0.0), portfolio._maximum_drawdown)
    return StreamingReplayResult(
        config=config,
        candidates=[],
        candidate_logs=portfolio.candidate_logs,
        trades=portfolio.completed_trades,
        equity_curve=portfolio.equity_curve,
        forward_rows=forward_rows,
        forward_summary=forward_summary,
        summary=summary,
        unfilled_orders=execution.unfilled_orders,
        quality=DataQualityReport(records=input_events),
        feature_distributions=feature_accumulator.to_dict(),
        candidate_summary=candidate_accumulator.to_dict(),
    )
