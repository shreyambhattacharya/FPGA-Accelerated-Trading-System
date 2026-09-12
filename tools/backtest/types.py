"""Shared dataclasses for replay, execution, portfolio, and reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateSignal:
    timestamp_ns: int
    event_type: int
    symbol_id: int
    sequence: int
    action: int
    score: int
    reason_bits: int
    feature: Any


@dataclass(frozen=True)
class OrderRequest:
    order_id: int
    symbol_id: int
    side: str
    purpose: str
    position_direction: str
    quantity: int
    order_timestamp_ns: int
    eligible_timestamp_ns: int
    candidate_timestamp_ns: int
    candidate_reason_bits: int = 0
    candidate_score: int = 0
    exit_reason: str = ""


@dataclass(frozen=True)
class Fill:
    order_id: int
    symbol_id: int
    side: str
    purpose: str
    position_direction: str
    quantity: int
    timestamp_ns: int
    reference_price: int
    fill_price: int
    commission: float
    candidate_timestamp_ns: int
    candidate_reason_bits: int = 0
    candidate_score: int = 0
    exit_reason: str = ""


@dataclass
class Position:
    symbol_id: int
    direction: str
    quantity: int
    entry_order_timestamp_ns: int
    entry_fill_timestamp_ns: int
    entry_price: int
    entry_candidate_timestamp_ns: int
    entry_reason_bits: int
    entry_score: int
    entry_feature: Any
    entry_costs: float


@dataclass(frozen=True)
class CompletedTrade:
    symbol_id: int
    direction: str
    candidate_timestamp_ns: int
    entry_order_timestamp_ns: int
    entry_fill_timestamp_ns: int
    entry_price: int
    exit_timestamp_ns: int
    exit_reason: str
    exit_price: int
    quantity: int
    notional: float
    gross_pnl: float
    costs: float
    net_pnl: float
    holding_duration_ns: int
    entry_reason_bits: int
    entry_score: int
    entry_feature: Any


@dataclass(frozen=True)
class EquityPoint:
    timestamp_ns: int
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    gross_exposure: float
    net_exposure: float
    open_positions: int


@dataclass(frozen=True)
class CandidateLog:
    timestamp_ns: int
    symbol_id: int
    sequence: int
    action: int
    score: int
    reason_bits: int
    portfolio_status: str
    order_id: int | None
