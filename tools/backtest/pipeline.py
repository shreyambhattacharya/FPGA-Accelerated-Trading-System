"""Historical replay pipeline connecting canonical events to paper accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_model"
if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from market_model import (  # noqa: E402
    MSG_MARKET_QUOTE,
    MarketModel,
)
from strategy_model import StrategyConfig, StrategyModel  # noqa: E402

from .canonical import NormalizedEvent, assign_replay_sequences, sort_events
from .data_quality import DataQualityReport, DataQualityValidator
from .execution_simulator import ExecutionConfig, ExecutionSimulator
from .forward_returns import ForwardReturnAnalyzer, ForwardReturnRow, QuotePoint
from .metrics import compute_metrics
from .portfolio_simulator import PortfolioConfig, PortfolioSimulator
from .session import SessionConfig
from .types import CandidateSignal, CandidateLog, CompletedTrade, EquityPoint


@dataclass(frozen=True)
class BacktestConfig:
    num_symbols: int = 4
    trade_window: int = 32
    momentum_window: int = 16
    vwap_window: int = 32
    session: SessionConfig = field(default_factory=SessionConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    strict_quality: bool = False
    gap_threshold_ns: int | None = 5 * 60 * 1_000_000_000
    seed: int = 7
    run_id: str = ""

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any], *, num_symbols: int | None = None) -> "BacktestConfig":
        session_data = dict(mapping.get("session", {}))
        if "timezone" in session_data and "timezone_name" not in session_data:
            session_data["timezone_name"] = session_data.pop("timezone")
        execution_data = dict(mapping.get("execution", {}))
        if "latency_ms" in execution_data and "latency_ns" not in execution_data:
            execution_data["latency_ns"] = int(round(float(execution_data.pop("latency_ms")) * 1_000_000))
        strategy_data = dict(mapping.get("strategy", mapping.get("strategy_config", {})))
        portfolio_data = dict(mapping.get("portfolio", {}))
        dimensions = mapping.get("model", {})
        symbols = num_symbols if num_symbols is not None else mapping.get("num_symbols", cls.num_symbols)
        strategy_data.setdefault("symbol_enable", [True] * symbols)
        return cls(
            num_symbols=symbols,
            trade_window=int(mapping.get("trade_window", dimensions.get("trade_window", cls.trade_window))),
            momentum_window=int(mapping.get("momentum_window", dimensions.get("momentum_window", cls.momentum_window))),
            vwap_window=int(mapping.get("vwap_window", dimensions.get("vwap_window", cls.vwap_window))),
            session=SessionConfig(**session_data),
            execution=ExecutionConfig(**execution_data),
            portfolio=PortfolioConfig(**portfolio_data),
            strategy=StrategyConfig(**strategy_data),
            strict_quality=bool(mapping.get("strict_quality", False)),
            gap_threshold_ns=mapping.get("gap_threshold_ns", cls.gap_threshold_ns),
            seed=int(mapping.get("seed", cls.seed)),
            run_id=str(mapping.get("run_id", "")),
        )


@dataclass
class BacktestRun:
    config: BacktestConfig
    input_events: list[NormalizedEvent]
    replay_events: list[NormalizedEvent]
    quality: DataQualityReport
    candidates: list[CandidateSignal]
    candidate_logs: list[CandidateLog]
    trades: list[CompletedTrade]
    equity_curve: list[EquityPoint]
    forward_rows: list[ForwardReturnRow]
    forward_summary: dict
    summary: dict
    unfilled_orders: list
    quote_points: list[QuotePoint]


class BacktestEngine:
    """Run one deterministic chronological replay with no network or broker."""

    def __init__(self, config: BacktestConfig | None = None, *, symbol_mapping=None, strategy_factory=None) -> None:
        self.config = config or BacktestConfig()
        self.symbol_mapping = symbol_mapping
        self.strategy_factory = strategy_factory

    def run(self, events: list[NormalizedEvent]) -> BacktestRun:
        config = self.config
        quality = DataQualityValidator(
            symbol_mapping=self.symbol_mapping,
            gap_threshold_ns=config.gap_threshold_ns,
        ).validate(events)
        if config.strict_quality and quality.has_errors:
            raise ValueError(f"strict data quality rejected replay: {quality.errors} errors")
        session_events = config.session.filter_events(events)
        replay_events = assign_replay_sequences(
            session_events,
            reset_per_session=config.session.reset_replay_sequences and config.session.reset_each_session,
            timezone=config.session.timezone,
        )
        market = MarketModel(
            num_symbols=config.num_symbols,
            trade_window=config.trade_window,
            momentum_window=config.momentum_window,
            vwap_window=config.vwap_window,
        )
        strategy = (
            self.strategy_factory(config.num_symbols, config.strategy)
            if self.strategy_factory is not None
            else StrategyModel(num_symbols=config.num_symbols, config=config.strategy)
        )
        execution = ExecutionSimulator(config.execution)
        portfolio = PortfolioSimulator(config.portfolio)
        candidates: list[CandidateSignal] = []
        quote_points: list[QuotePoint] = []
        last_features: dict[int, Any] = {}
        previous_session = None
        last_timestamp_ns = 0
        rejected = 0

        for event in replay_events:
            current_session = config.session.session_date(event.timestamp_ns)
            if previous_session is not None and current_session != previous_session:
                self._close_session(portfolio, execution, last_features, last_timestamp_ns)
                if config.session.reset_each_session:
                    market.reset_state()
                    strategy.reset_state()
                last_features.clear()
                execution.flush()
                portfolio.pending_symbols.clear()
                portfolio._pending_order_ids.clear()
            previous_session = current_session
            last_timestamp_ns = event.timestamp_ns
            outcome = market.process_event(event.to_market_event())
            if outcome.kind != "feature" or outcome.feature is None:
                rejected += 1
                continue
            feature = outcome.feature
            last_features[event.symbol_id] = feature
            is_quote = event.event_type == MSG_MARKET_QUOTE
            if is_quote:
                quote_points.append(QuotePoint(event.timestamp_ns, event.symbol_id, feature.bid_price, feature.ask_price))
                portfolio.on_quote(event.timestamp_ns, feature, execution)
            portfolio.evaluate_risk(event.timestamp_ns, feature, execution, is_quote=is_quote)
            signal = strategy.evaluate(feature)
            candidate = CandidateSignal(
                timestamp_ns=event.timestamp_ns,
                event_type=event.event_type,
                symbol_id=signal.symbol_id,
                sequence=signal.sequence,
                action=signal.action,
                score=signal.score,
                reason_bits=signal.reason_bits,
                feature=feature,
            )
            candidates.append(candidate)
            portfolio.consider_candidate(candidate, execution, is_quote=is_quote)
            portfolio.mark_to_market(event.timestamp_ns, feature)

        if last_features:
            self._close_session(portfolio, execution, last_features, last_timestamp_ns)
        unfilled = execution.flush()
        portfolio.pending_symbols.clear()
        portfolio._pending_order_ids.clear()
        summary = compute_metrics(
            portfolio.completed_trades,
            portfolio.equity_curve,
            starting_cash=config.portfolio.starting_cash,
            timezone_name=config.session.timezone_name,
        )
        summary.update({
            "input_events": len(events),
            "session_events": len(replay_events),
            "accepted_features": len(candidates),
            "rejected_events": rejected,
            "candidate_count": sum(candidate.action != 0 for candidate in candidates),
            "quality": quality.to_dict(),
            "unfilled_orders": len(unfilled),
        })
        forward_rows, forward_summary = ForwardReturnAnalyzer().analyze(candidates, quote_points)
        return BacktestRun(
            config=config,
            input_events=events,
            replay_events=replay_events,
            quality=quality,
            candidates=candidates,
            candidate_logs=portfolio.candidate_logs,
            trades=portfolio.completed_trades,
            equity_curve=portfolio.equity_curve,
            forward_rows=forward_rows,
            forward_summary=forward_summary,
            summary=summary,
            unfilled_orders=unfilled,
            quote_points=quote_points,
        )

    @staticmethod
    def _close_session(portfolio, execution, last_features, timestamp_ns):
        if not last_features:
            return
        if portfolio.config.end_of_day_flatten:
            portfolio.flatten(timestamp_ns, last_features, execution, force_immediate=True)
        # Capture the post-flatten (or post-session mark) equity, rather than
        # leaving the final report at the pre-EOD observation.
        portfolio.mark_to_market(timestamp_ns, next(iter(last_features.values())))
