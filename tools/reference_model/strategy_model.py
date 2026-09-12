"""Integer reference model for the FPGA candidate-signal engine.

The model deliberately emits candidate decisions only.  It has no position,
cash, order, broker, or profitability model.  Every comparison, reason bit,
score, edge, cooldown, and symbol reset mirrors ``signal_engine.sv``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market_model import FeatureSnapshot

SIGNAL_NONE = 0
SIGNAL_LONG_CANDIDATE = 1
SIGNAL_SHORT_CANDIDATE = 2

REASON_MOMENTUM_PASS = 0x0001
REASON_VWAP_DELTA_PASS = 0x0002
REASON_IMBALANCE_PASS = 0x0004
REASON_SPREAD_PASS = 0x0008
REASON_VOLUME_PASS = 0x0010
REASON_LONG_DIRECTION = 0x0020
REASON_SHORT_DIRECTION = 0x0040
REASON_COOLDOWN_ACTIVE = 0x0080
REASON_INVALID_FEATURE = 0x0100
REASON_SYMBOL_DISABLED = 0x0200
REASON_STRATEGY_DISABLED = 0x0400
REASON_LONG_DISABLED = 0x0800
REASON_SHORT_DISABLED = 0x1000
REASON_SUPPRESSED = 0x2000


@dataclass
class StrategyConfig:
    """Runtime candidate-signal configuration in normalized feature units."""

    strategy_enable: bool = False
    long_enable: bool = True
    short_enable: bool = True
    long_min_momentum_bps_x100: int = 100
    short_max_momentum_bps_x100: int = -100
    long_min_vwap_delta_bps_x100: int = 100
    short_max_vwap_delta_bps_x100: int = -100
    long_min_imbalance_q15: int = 1638
    short_max_imbalance_q15: int = -1638
    max_spread_bps_x100: int = 500
    min_rolling_volume: int = 1
    signal_cooldown_events: int = 4
    symbol_enable: list[bool] = field(default_factory=list)


@dataclass(frozen=True)
class SignalSnapshot:
    symbol_id: int
    sequence: int
    action: int
    score: int
    reason_bits: int


@dataclass
class StrategyMetrics:
    """Counters for future replay/backtest integration; no P&L is fabricated."""

    evaluated: int = 0
    no_action: int = 0
    long_candidates: int = 0
    short_candidates: int = 0
    invalid: int = 0
    suppressed: int = 0


class StrategyModel:
    """Reference implementation of the registered signal-engine decision."""

    def __init__(self, num_symbols: int = 4, config: StrategyConfig | None = None) -> None:
        if num_symbols < 1:
            raise ValueError("num_symbols must be positive")
        self.num_symbols = num_symbols
        self.config = config or StrategyConfig()
        if not self.config.symbol_enable:
            self.config.symbol_enable = [True] * num_symbols
        if len(self.config.symbol_enable) != num_symbols:
            raise ValueError("symbol_enable length must match num_symbols")
        self.previous_condition = [SIGNAL_NONE] * num_symbols
        self.last_emitted_action = [SIGNAL_NONE] * num_symbols
        self.cooldown_remaining = [0] * num_symbols
        self.metrics = StrategyMetrics()

    def reset_symbol(self, symbol_id: int) -> None:
        self._check_symbol(symbol_id)
        self.previous_condition[symbol_id] = SIGNAL_NONE
        self.last_emitted_action[symbol_id] = SIGNAL_NONE
        self.cooldown_remaining[symbol_id] = 0

    def set_symbol_enabled(self, symbol_id: int, enabled: bool) -> None:
        self._check_symbol(symbol_id)
        self.config.symbol_enable[symbol_id] = enabled

    def evaluate(self, feature: FeatureSnapshot) -> SignalSnapshot:
        symbol_id = feature.symbol_id
        self._check_symbol(symbol_id)
        cfg = self.config
        previous = self.previous_condition[symbol_id]
        last_emitted = self.last_emitted_action[symbol_id]
        cooldown = self.cooldown_remaining[symbol_id]
        symbol_enabled = cfg.symbol_enable[symbol_id]

        feature_set_valid = (
            feature.spread_bps_x100_valid
            and feature.momentum_bps_x100_valid
            and feature.imbalance_normalized_valid
            and feature.vwap_quotient_valid
            and feature.midpoint_minus_vwap_bps_x100_valid
        )

        long_pass = (
            feature.momentum_bps_x100_valid
            and feature.momentum_bps_x100 >= cfg.long_min_momentum_bps_x100,
            feature.midpoint_minus_vwap_bps_x100_valid
            and feature.midpoint_minus_vwap_bps_x100 >= cfg.long_min_vwap_delta_bps_x100,
            feature.imbalance_normalized_valid
            and feature.imbalance_normalized >= cfg.long_min_imbalance_q15,
            feature.spread_bps_x100_valid
            and feature.spread_bps_x100 <= cfg.max_spread_bps_x100,
            feature.rolling_volume >= cfg.min_rolling_volume,
        )
        short_pass = (
            feature.momentum_bps_x100_valid
            and feature.momentum_bps_x100 <= cfg.short_max_momentum_bps_x100,
            feature.midpoint_minus_vwap_bps_x100_valid
            and feature.midpoint_minus_vwap_bps_x100 <= cfg.short_max_vwap_delta_bps_x100,
            feature.imbalance_normalized_valid
            and feature.imbalance_normalized <= cfg.short_max_imbalance_q15,
            feature.spread_bps_x100_valid
            and feature.spread_bps_x100 <= cfg.max_spread_bps_x100,
            feature.rolling_volume >= cfg.min_rolling_volume,
        )

        raw_direction = SIGNAL_NONE
        if all(long_pass):
            raw_direction = SIGNAL_LONG_CANDIDATE
        elif all(short_pass):
            raw_direction = SIGNAL_SHORT_CANDIDATE

        eligible_direction = SIGNAL_NONE
        if feature_set_valid and symbol_enabled and cfg.strategy_enable:
            if raw_direction == SIGNAL_LONG_CANDIDATE and cfg.long_enable:
                eligible_direction = SIGNAL_LONG_CANDIDATE
            elif raw_direction == SIGNAL_SHORT_CANDIDATE and cfg.short_enable:
                eligible_direction = SIGNAL_SHORT_CANDIDATE

        direction_edge = raw_direction != SIGNAL_NONE and raw_direction != previous
        cooldown_expiry_repeat = (
            cfg.signal_cooldown_events != 0
            and cooldown == 1
            and raw_direction == last_emitted
        )
        candidate_fire = eligible_direction != SIGNAL_NONE and (
            (direction_edge and cooldown == 0)
            or (eligible_direction == last_emitted and cooldown_expiry_repeat)
        )

        reason = self._reason_bits(
            raw_direction,
            long_pass,
            short_pass,
            feature_set_valid,
            symbol_enabled,
            cfg,
            cooldown,
            candidate_fire,
        )
        if raw_direction == SIGNAL_LONG_CANDIDATE:
            score = sum(long_pass)
        elif raw_direction == SIGNAL_SHORT_CANDIDATE:
            score = sum(short_pass)
        else:
            score = sum(any(pair) for pair in zip(long_pass, short_pass))

        action = eligible_direction if candidate_fire else SIGNAL_NONE
        if (
            not cfg.strategy_enable
            or not symbol_enabled
            or (raw_direction == SIGNAL_LONG_CANDIDATE and not cfg.long_enable)
            or (raw_direction == SIGNAL_SHORT_CANDIDATE and not cfg.short_enable)
        ):
            self.previous_condition[symbol_id] = SIGNAL_NONE
        else:
            self.previous_condition[symbol_id] = raw_direction
        if candidate_fire:
            self.last_emitted_action[symbol_id] = eligible_direction
            self.cooldown_remaining[symbol_id] = cfg.signal_cooldown_events
        elif cooldown:
            self.cooldown_remaining[symbol_id] = cooldown - 1

        self.metrics.evaluated += 1
        if action == SIGNAL_LONG_CANDIDATE:
            self.metrics.long_candidates += 1
        elif action == SIGNAL_SHORT_CANDIDATE:
            self.metrics.short_candidates += 1
        else:
            self.metrics.no_action += 1
        if not feature_set_valid:
            self.metrics.invalid += 1
        if raw_direction != SIGNAL_NONE and not candidate_fire:
            self.metrics.suppressed += 1

        return SignalSnapshot(symbol_id, feature.sequence, action, score, reason)

    @staticmethod
    def _reason_bits(
        raw_direction: int,
        long_pass: tuple[bool, ...],
        short_pass: tuple[bool, ...],
        feature_set_valid: bool,
        symbol_enabled: bool,
        cfg: StrategyConfig,
        cooldown: int,
        candidate_fire: bool,
    ) -> int:
        reason = 0
        passes = long_pass if raw_direction == SIGNAL_LONG_CANDIDATE else short_pass
        if raw_direction == SIGNAL_NONE:
            passes = tuple(a or b for a, b in zip(long_pass, short_pass))
        if passes[0]:
            reason |= REASON_MOMENTUM_PASS
        if passes[1]:
            reason |= REASON_VWAP_DELTA_PASS
        if passes[2]:
            reason |= REASON_IMBALANCE_PASS
        if passes[3]:
            reason |= REASON_SPREAD_PASS
        if passes[4]:
            reason |= REASON_VOLUME_PASS
        if raw_direction == SIGNAL_LONG_CANDIDATE:
            reason |= REASON_LONG_DIRECTION
        elif raw_direction == SIGNAL_SHORT_CANDIDATE:
            reason |= REASON_SHORT_DIRECTION
        if not feature_set_valid:
            reason |= REASON_INVALID_FEATURE
        if not symbol_enabled:
            reason |= REASON_SYMBOL_DISABLED
        if not cfg.strategy_enable:
            reason |= REASON_STRATEGY_DISABLED
        if not cfg.long_enable and raw_direction == SIGNAL_LONG_CANDIDATE:
            reason |= REASON_LONG_DISABLED
        if not cfg.short_enable and raw_direction == SIGNAL_SHORT_CANDIDATE:
            reason |= REASON_SHORT_DISABLED
        if raw_direction != SIGNAL_NONE and cooldown and not candidate_fire:
            reason |= REASON_COOLDOWN_ACTIVE
        if raw_direction != SIGNAL_NONE and not candidate_fire:
            reason |= REASON_SUPPRESSED
        return reason

    def _check_symbol(self, symbol_id: int) -> None:
        if not 0 <= symbol_id < self.num_symbols:
            raise ValueError("symbol_id outside strategy model")
