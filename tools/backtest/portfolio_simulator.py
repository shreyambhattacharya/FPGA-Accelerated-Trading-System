"""Portfolio and position accounting for the quote-aware paper simulator."""

from __future__ import annotations

from dataclasses import dataclass

from .execution_simulator import ExecutionSimulator
from .types import CandidateLog, CandidateSignal, CompletedTrade, EquityPoint, Fill, OrderRequest, Position

SIGNAL_NONE = 0
SIGNAL_LONG_CANDIDATE = 1
SIGNAL_SHORT_CANDIDATE = 2


@dataclass(frozen=True)
class PortfolioConfig:
    starting_cash: float = 100_000.0
    sizing_mode: str = "fixed_notional"
    fixed_shares: int = 1
    fixed_notional: float = 10_000.0
    fixed_cash_pct: float = 0.10
    max_concurrent_positions: int | None = None
    enable_short: bool = False
    no_pyramiding: bool = True
    reverse_same_event: bool = False
    opposite_candidate_exit: bool = True
    stop_loss_bps: float | None = None
    take_profit_bps: float | None = None
    max_holding_ns: int | None = None
    end_of_day_flatten: bool = True
    max_gross_exposure: float | None = None

    def __post_init__(self) -> None:
        if self.starting_cash < 0:
            raise ValueError("starting_cash must be non-negative")
        if self.sizing_mode not in {"fixed_shares", "fixed_notional", "cash_pct"}:
            raise ValueError("sizing_mode must be fixed_shares, fixed_notional, or cash_pct")
        if self.fixed_shares < 1 or self.fixed_notional < 0 or not 0 <= self.fixed_cash_pct <= 1:
            raise ValueError("invalid position sizing settings")
        if self.max_concurrent_positions is not None and self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be positive")


class PortfolioSimulator:
    """Maintain one flat/long/short position per symbol and cash accounting."""

    def __init__(self, config: PortfolioConfig | None = None, *, record_candidate_logs: bool = True) -> None:
        self.config = config or PortfolioConfig()
        self.cash = self.config.starting_cash
        self.positions: dict[int, Position] = {}
        self.pending_symbols: set[int] = set()
        self._pending_order_ids: dict[int, int] = {}
        self.completed_trades: list[CompletedTrade] = []
        self.candidate_logs: list[CandidateLog] = []
        self.equity_curve: list[EquityPoint] = []
        self.record_candidate_logs = record_candidate_logs
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self._high_water_equity = None
        self._maximum_drawdown = 0.0

    def consider_candidate(
        self,
        candidate: CandidateSignal,
        execution: ExecutionSimulator,
        *,
        is_quote: bool,
    ) -> list[Fill]:
        """Log every candidate and optionally create a paper order."""

        status = "no_action"
        order_id: int | None = None
        symbol_id = candidate.symbol_id
        current = self.positions.get(symbol_id)

        if candidate.action == SIGNAL_NONE:
            self._log_candidate(candidate, status, order_id)
            return []
        if symbol_id in self.pending_symbols:
            status = "pending_order"
            self._log_candidate(candidate, status, order_id)
            return []

        desired = "long" if candidate.action == SIGNAL_LONG_CANDIDATE else "short"
        if desired == "short" and not self.config.enable_short:
            status = "short_disabled"
            self._log_candidate(candidate, status, order_id)
            return []

        if current is not None:
            if current.direction == desired:
                status = "already_in_position"
                self._log_candidate(candidate, status, order_id)
                return []
            if not self.config.opposite_candidate_exit:
                status = "opposite_candidate_ignored"
                self._log_candidate(candidate, status, order_id)
                return []
            fills, order_id = self._submit_exit(
                current,
                candidate.timestamp_ns,
                candidate.feature,
                execution,
                reason="opposite_candidate",
                candidate=candidate,
                is_quote=is_quote,
            )
            status = "exit_submitted" if order_id is not None else "exit_unavailable"
            self._log_candidate(candidate, status, order_id)
            if self.config.reverse_same_event and fills and symbol_id not in self.positions:
                extra = self._submit_entry(candidate, desired, execution, is_quote=is_quote)
                fills.extend(extra)
            return fills

        order_id, order = self._entry_order(candidate, desired, execution)
        if order is None:
            status = "entry_suppressed"
            self._log_candidate(candidate, status, order_id)
            return []
        self.pending_symbols.add(symbol_id)
        self._pending_order_ids[symbol_id] = order_id
        fills = execution.submit(order, timestamp_ns=candidate.timestamp_ns, feature=candidate.feature, is_quote=is_quote)
        status = "entry_filled" if fills else "entry_submitted"
        self._log_candidate(candidate, status, order_id)
        if fills:
            self.apply_fills(fills)
        return fills

    def on_quote(self, timestamp_ns: int, feature, execution: ExecutionSimulator) -> list[Fill]:
        fills = execution.on_quote(timestamp_ns, feature)
        self.apply_fills(fills)
        return fills

    def evaluate_risk(self, timestamp_ns: int, feature, execution: ExecutionSimulator, *, is_quote: bool) -> list[Fill]:
        """Create exits for stop, target, and maximum holding-time policies."""

        fills: list[Fill] = []
        for position in list(self.positions.values()):
            if position.symbol_id in self.pending_symbols:
                continue
            reason = self._risk_reason(timestamp_ns, feature, position, is_quote=is_quote)
            if reason is None:
                continue
            submitted, _ = self._submit_exit(
                position,
                timestamp_ns,
                feature,
                execution,
                reason=reason,
                candidate=None,
                is_quote=is_quote and feature.symbol_id == position.symbol_id,
            )
            fills.extend(submitted)
        return fills

    def flatten(self, timestamp_ns: int, feature, execution: ExecutionSimulator, *, force_immediate: bool = False) -> list[Fill]:
        """Request EOD exits using the last available quote for each symbol."""

        fills: list[Fill] = []
        for position in list(self.positions.values()):
            if position.symbol_id in self.pending_symbols:
                continue
            position_feature = feature.get(position.symbol_id, feature) if isinstance(feature, dict) else feature
            order_id, order = self._exit_order(
                position,
                timestamp_ns,
                reason="end_of_day",
                candidate_timestamp_ns=timestamp_ns,
                candidate_reason_bits=0,
                candidate_score=0,
                execution=execution,
            )
            self.pending_symbols.add(position.symbol_id)
            self._pending_order_ids[position.symbol_id] = order_id
            fills.extend(
                execution.submit(
                    order,
                    timestamp_ns=timestamp_ns,
                    feature=position_feature,
                    is_quote=True,
                    force_immediate=force_immediate,
                )
            )
        self.apply_fills(fills)
        return fills

    def apply_fills(self, fills: list[Fill]) -> None:
        for fill in fills:
            symbol_id = fill.symbol_id
            self.pending_symbols.discard(symbol_id)
            self._pending_order_ids.pop(symbol_id, None)
            dollars = fill.fill_price * fill.quantity / 1_000_000.0
            if fill.purpose == "entry":
                if symbol_id in self.positions:
                    continue
                if fill.position_direction == "long":
                    self.cash -= dollars + fill.commission
                else:
                    self.cash -= fill.commission
                self.positions[symbol_id] = Position(
                    symbol_id=symbol_id,
                    direction=fill.position_direction,
                    quantity=fill.quantity,
                    entry_order_timestamp_ns=fill.candidate_timestamp_ns,
                    entry_fill_timestamp_ns=fill.timestamp_ns,
                    entry_price=fill.fill_price,
                    entry_candidate_timestamp_ns=fill.candidate_timestamp_ns,
                    entry_reason_bits=fill.candidate_reason_bits,
                    entry_score=fill.candidate_score,
                    entry_feature=fill.candidate_feature,
                    entry_costs=fill.commission,
                    entry_sequence=fill.candidate_sequence,
                    entry_eligible_timestamp_ns=fill.eligible_timestamp_ns,
                    entry_side=fill.side,
                    entry_reference_price=fill.reference_price,
                )
                continue
            position = self.positions.pop(symbol_id, None)
            if position is None:
                continue
            if position.direction == "long":
                self.cash += dollars - fill.commission
                gross = (fill.fill_price - position.entry_price) * position.quantity / 1_000_000.0
            else:
                self.cash += (position.entry_price - fill.fill_price) * position.quantity / 1_000_000.0 - fill.commission
                gross = (position.entry_price - fill.fill_price) * position.quantity / 1_000_000.0
            costs = position.entry_costs + fill.commission
            net = gross - costs
            slippage_cost = (
                abs(position.entry_price - position.entry_reference_price) * position.quantity
                + abs(fill.fill_price - fill.reference_price) * fill.quantity
            ) / 1_000_000.0
            trade = CompletedTrade(
                symbol_id=symbol_id,
                direction=position.direction,
                candidate_timestamp_ns=position.entry_candidate_timestamp_ns,
                entry_order_timestamp_ns=position.entry_order_timestamp_ns,
                entry_fill_timestamp_ns=position.entry_fill_timestamp_ns,
                entry_price=position.entry_price,
                exit_timestamp_ns=fill.timestamp_ns,
                exit_reason=fill.exit_reason,
                exit_price=fill.fill_price,
                quantity=position.quantity,
                notional=position.entry_price * position.quantity / 1_000_000.0,
                gross_pnl=gross,
                costs=costs,
                net_pnl=net,
                holding_duration_ns=fill.timestamp_ns - position.entry_fill_timestamp_ns,
                entry_reason_bits=position.entry_reason_bits,
                entry_score=position.entry_score,
                entry_feature=position.entry_feature,
                entry_sequence=position.entry_sequence,
                entry_eligible_timestamp_ns=position.entry_eligible_timestamp_ns,
                entry_side=position.entry_side,
                entry_reference_price=position.entry_reference_price,
                slippage_cost=slippage_cost,
                commission=costs,
            )
            self.completed_trades.append(trade)
            self.realized_pnl += net

    def mark_to_market(self, timestamp_ns: int, feature, *, record: bool = True) -> EquityPoint:
        unrealized = 0.0
        gross = 0.0
        net = 0.0
        for position in self.positions.values():
            if position.direction == "long":
                mark = feature.bid_price if feature.symbol_id == position.symbol_id and feature.bid_price > 0 else position.entry_price
                unrealized += (mark - position.entry_price) * position.quantity / 1_000_000.0
                gross += mark * position.quantity / 1_000_000.0
                net += mark * position.quantity / 1_000_000.0
            else:
                mark = feature.ask_price if feature.symbol_id == position.symbol_id and feature.ask_price > 0 else position.entry_price
                unrealized += (position.entry_price - mark) * position.quantity / 1_000_000.0
                gross += mark * position.quantity / 1_000_000.0
                net -= mark * position.quantity / 1_000_000.0
        self.unrealized_pnl = unrealized
        point = EquityPoint(
            timestamp_ns=timestamp_ns,
            cash=self.cash,
            equity=self.cash + unrealized + sum(
                p.entry_price * p.quantity / 1_000_000.0
                for p in self.positions.values()
                if p.direction == "long"
            ),
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            gross_exposure=gross,
            net_exposure=net,
            open_positions=len(self.positions),
        )
        if self._high_water_equity is None:
            self._high_water_equity = point.equity
        else:
            self._high_water_equity = max(self._high_water_equity, point.equity)
        self._maximum_drawdown = min(self._maximum_drawdown, point.equity - self._high_water_equity)
        if record:
            self.equity_curve.append(point)
        return point

    def _entry_order(self, candidate: CandidateSignal, direction: str, execution: ExecutionSimulator):
        feature = candidate.feature
        price = feature.ask_price if direction == "long" else feature.bid_price
        if price <= 0:
            return None, None
        quantity = self._size(price)
        if quantity < 1:
            return None, None
        notional = price * quantity / 1_000_000.0
        estimated_cost = execution._commission(quantity)
        if direction == "long" and self.cash < notional + estimated_cost:
            return None, None
        if direction == "short" and self.cash < estimated_cost:
            return None, None
        if self.config.max_concurrent_positions is not None and len(self.positions) >= self.config.max_concurrent_positions:
            return None, None
        if self.config.no_pyramiding and candidate.symbol_id in self.positions:
            return None, None
        exposure_limit = self.config.max_gross_exposure if self.config.max_gross_exposure is not None else self.config.starting_cash
        if self._gross_exposure() + notional > exposure_limit:
            return None, None
        order_id = execution.new_order_id()
        order = OrderRequest(
            order_id=order_id,
            symbol_id=candidate.symbol_id,
            side="buy" if direction == "long" else "sell",
            purpose="entry",
            position_direction=direction,
            quantity=quantity,
            order_timestamp_ns=candidate.timestamp_ns,
            eligible_timestamp_ns=candidate.timestamp_ns + execution.config.latency_ns,
            candidate_timestamp_ns=candidate.timestamp_ns,
            candidate_reason_bits=candidate.reason_bits,
            candidate_score=candidate.score,
            candidate_sequence=candidate.sequence,
            candidate_feature=candidate.feature,
        )
        return order_id, order

    def _submit_entry(self, candidate: CandidateSignal, direction: str, execution: ExecutionSimulator, *, is_quote: bool) -> list[Fill]:
        order_id, order = self._entry_order(candidate, direction, execution)
        if order is None:
            return []
        self.pending_symbols.add(candidate.symbol_id)
        self._pending_order_ids[candidate.symbol_id] = order_id
        fills = execution.submit(order, timestamp_ns=candidate.timestamp_ns, feature=candidate.feature, is_quote=is_quote)
        self.apply_fills(fills)
        return fills

    def _submit_exit(self, position, timestamp_ns, feature, execution, *, reason, candidate, is_quote):
        order_id, order = self._exit_order(
            position,
            timestamp_ns,
            reason=reason,
            candidate_timestamp_ns=candidate.timestamp_ns if candidate else position.entry_candidate_timestamp_ns,
            candidate_reason_bits=candidate.reason_bits if candidate else 0,
            candidate_score=candidate.score if candidate else 0,
            candidate_sequence=candidate.sequence if candidate else 0,
            candidate_feature=candidate.feature if candidate else None,
            execution=execution,
        )
        self.pending_symbols.add(position.symbol_id)
        self._pending_order_ids[position.symbol_id] = order_id
        fills = execution.submit(order, timestamp_ns=timestamp_ns, feature=feature, is_quote=is_quote)
        self.apply_fills(fills)
        return fills, order_id

    @staticmethod
    def _exit_order(position, timestamp_ns, *, reason, candidate_timestamp_ns, candidate_reason_bits, candidate_score, candidate_sequence=0, candidate_feature=None, execution):
        order_id = execution.new_order_id()
        return order_id, OrderRequest(
            order_id=order_id,
            symbol_id=position.symbol_id,
            side="sell" if position.direction == "long" else "buy",
            purpose="exit",
            position_direction=position.direction,
            quantity=position.quantity,
            order_timestamp_ns=timestamp_ns,
            eligible_timestamp_ns=timestamp_ns + execution.config.latency_ns,
            candidate_timestamp_ns=candidate_timestamp_ns,
            candidate_reason_bits=candidate_reason_bits,
            candidate_score=candidate_score,
            exit_reason=reason,
            candidate_sequence=candidate_sequence,
            candidate_feature=candidate_feature,
        )

    def _risk_reason(self, timestamp_ns, feature, position, *, is_quote):
        if not is_quote or feature.symbol_id != position.symbol_id:
            return "max_holding" if self.config.max_holding_ns is not None and timestamp_ns - position.entry_fill_timestamp_ns >= self.config.max_holding_ns else None
        if position.direction == "long":
            mark = feature.bid_price
            if mark <= 0:
                return None
            if self.config.stop_loss_bps is not None and mark <= position.entry_price * (1 - self.config.stop_loss_bps / 10_000):
                return "stop_loss"
            if self.config.take_profit_bps is not None and mark >= position.entry_price * (1 + self.config.take_profit_bps / 10_000):
                return "take_profit"
        else:
            mark = feature.ask_price
            if mark <= 0:
                return None
            if self.config.stop_loss_bps is not None and mark >= position.entry_price * (1 + self.config.stop_loss_bps / 10_000):
                return "stop_loss"
            if self.config.take_profit_bps is not None and mark <= position.entry_price * (1 - self.config.take_profit_bps / 10_000):
                return "take_profit"
        if self.config.max_holding_ns is not None and timestamp_ns - position.entry_fill_timestamp_ns >= self.config.max_holding_ns:
            return "max_holding"
        return None

    def _size(self, price: int) -> int:
        if self.config.sizing_mode == "fixed_shares":
            return self.config.fixed_shares
        if self.config.sizing_mode == "cash_pct":
            return max(1, int((self.cash * self.config.fixed_cash_pct) * 1_000_000 // price))
        return max(1, int(self.config.fixed_notional * 1_000_000 // price))

    def _gross_exposure(self) -> float:
        return sum(p.entry_price * p.quantity / 1_000_000.0 for p in self.positions.values())

    def _log_candidate(self, candidate, status, order_id):
        if not self.record_candidate_logs:
            return
        self.candidate_logs.append(
            CandidateLog(
                timestamp_ns=candidate.timestamp_ns,
                symbol_id=candidate.symbol_id,
                sequence=candidate.sequence,
                action=candidate.action,
                score=candidate.score,
                reason_bits=candidate.reason_bits,
                portfolio_status=status,
                order_id=order_id,
            )
        )
