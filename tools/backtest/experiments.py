"""Research-only ablations, baselines, sweeps, and walk-forward helpers.

The exact FPGA-compatible StrategyModel remains the primary path.  The
ablation policies below intentionally live outside RTL and are labeled as
software research variants.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import product
import random

from .pipeline import BacktestConfig, BacktestEngine
from .portfolio_simulator import SIGNAL_LONG_CANDIDATE, SIGNAL_NONE, SIGNAL_SHORT_CANDIDATE
from .splits import chronological_split, walk_forward_windows


class ResearchSignalPolicy:
    """Candidate policy with selected factors disabled for ablation only."""

    def __init__(self, num_symbols, config, excluded: set[str] | None = None):
        self.num_symbols = num_symbols
        self.config = config
        self.excluded = excluded or set()
        self.previous = [0] * num_symbols
        self.last = [0] * num_symbols
        self.cooldown = [0] * num_symbols

    def reset_state(self, **kwargs):
        self.previous = [0] * self.num_symbols
        self.last = [0] * self.num_symbols
        self.cooldown = [0] * self.num_symbols

    def evaluate(self, feature):
        cfg = self.config
        sid = feature.symbol_id
        def enabled(name):
            return name not in self.excluded
        valid = feature.spread_bps_x100_valid and feature.momentum_bps_x100_valid and feature.imbalance_normalized_valid and feature.vwap_quotient_valid and feature.midpoint_minus_vwap_bps_x100_valid
        long_pass = (
            not enabled("momentum") or feature.momentum_bps_x100 >= cfg.long_min_momentum_bps_x100,
            not enabled("vwap_delta") or feature.midpoint_minus_vwap_bps_x100 >= cfg.long_min_vwap_delta_bps_x100,
            not enabled("imbalance") or feature.imbalance_normalized >= cfg.long_min_imbalance_q15,
            not enabled("spread") or feature.spread_bps_x100 <= cfg.max_spread_bps_x100,
            not enabled("volume") or feature.rolling_volume >= cfg.min_rolling_volume,
        )
        short_pass = (
            not enabled("momentum") or feature.momentum_bps_x100 <= cfg.short_max_momentum_bps_x100,
            not enabled("vwap_delta") or feature.midpoint_minus_vwap_bps_x100 <= cfg.short_max_vwap_delta_bps_x100,
            not enabled("imbalance") or feature.imbalance_normalized <= cfg.short_max_imbalance_q15,
            not enabled("spread") or feature.spread_bps_x100 <= cfg.max_spread_bps_x100,
            not enabled("volume") or feature.rolling_volume >= cfg.min_rolling_volume,
        )
        raw = SIGNAL_LONG_CANDIDATE if all(long_pass) else SIGNAL_SHORT_CANDIDATE if all(short_pass) else SIGNAL_NONE
        previous = self.previous[sid]
        edge = raw != SIGNAL_NONE and raw != previous
        fire = raw != SIGNAL_NONE and raw != self.last[sid] and self.cooldown[sid] == 0 and edge
        action = raw if fire and valid and cfg.strategy_enable else SIGNAL_NONE
        if raw != SIGNAL_NONE and not fire and self.cooldown[sid] > 0:
            self.cooldown[sid] -= 1
        if fire:
            self.last[sid] = raw
            self.cooldown[sid] = cfg.signal_cooldown_events
        self.previous[sid] = raw if cfg.strategy_enable and valid else SIGNAL_NONE
        score = sum(long_pass) if raw == SIGNAL_LONG_CANDIDATE else sum(short_pass) if raw == SIGNAL_SHORT_CANDIDATE else sum(a or b for a, b in zip(long_pass, short_pass))
        reason = 0
        reason |= 0x0001 if (long_pass[0] or short_pass[0]) else 0
        reason |= 0x0002 if (long_pass[1] or short_pass[1]) else 0
        reason |= 0x0004 if (long_pass[2] or short_pass[2]) else 0
        reason |= 0x0008 if (long_pass[3] or short_pass[3]) else 0
        reason |= 0x0010 if (long_pass[4] or short_pass[4]) else 0
        reason |= 0x0020 if raw == SIGNAL_LONG_CANDIDATE else 0x0040 if raw == SIGNAL_SHORT_CANDIDATE else 0
        return type("ResearchSignal", (), {"symbol_id": sid, "sequence": feature.sequence, "action": action, "score": score, "reason_bits": reason})()


def run_factor_ablation(events, base_config: BacktestConfig, factors=("momentum", "vwap_delta", "imbalance", "spread", "volume")):
    results = []
    for factor in (None, *factors):
        excluded = set() if factor is None else {factor}
        factory = None if factor is None else lambda n, cfg, excluded=excluded: ResearchSignalPolicy(n, cfg, excluded)
        run = BacktestEngine(base_config, strategy_factory=factory).run(events)
        results.append({"excluded_factor": factor or "none", "summary": run.summary, "forward_summary": run.forward_summary})
    return results


def run_parameter_grid(events, base_config: BacktestConfig, grid: dict[str, list], *, max_combinations=256):
    keys = sorted(grid)
    values = [grid[key] for key in keys]
    combinations = list(product(*values))
    if len(combinations) > max_combinations:
        raise ValueError(f"grid has {len(combinations)} combinations; max is {max_combinations}")
    split = chronological_split(events, session=base_config.session)
    results = []
    for values_tuple in combinations:
        overrides = dict(zip(keys, values_tuple))
        strategy = replace(base_config.strategy, **overrides)
        train_run = BacktestEngine(replace(base_config, strategy=strategy)).run(split.train)
        validation_run = BacktestEngine(replace(base_config, strategy=strategy)).run(split.validation)
        results.append({
            "parameters": overrides,
            "train_net_pnl": train_run.summary["total_net_pnl"],
            "validation_net_pnl": validation_run.summary["total_net_pnl"],
            "validation_sharpe": validation_run.summary["risk_adjusted"]["sharpe"],
        })
    return sorted(results, key=lambda row: (row["validation_net_pnl"], row["train_net_pnl"]), reverse=True)


def run_baselines(events, base_config: BacktestConfig, *, seed=7):
    """Return no-trade, momentum-only, VWAP-only, and seeded random baselines."""
    no_trade = BacktestEngine(replace(base_config, strategy=replace(base_config.strategy, strategy_enable=False))).run(events)
    results = [{"name": "no_trade", "summary": no_trade.summary, "forward_summary": no_trade.forward_summary}]
    for name, excluded in (("momentum_only", {"vwap_delta", "imbalance", "spread", "volume"}), ("vwap_only", {"momentum", "imbalance", "spread", "volume"})):
        policy = lambda n, cfg, excluded=excluded: ResearchSignalPolicy(n, cfg, excluded)
        run = BacktestEngine(base_config, strategy_factory=policy).run(events)
        results.append({"name": name, "summary": run.summary, "forward_summary": run.forward_summary})
    rng = random.Random(seed)
    # A seeded random baseline emits candidate-shaped events at the same
    # feature timestamps. It is intentionally independent of future prices.
    class RandomPolicy:
        def __init__(self, num_symbols, config):
            self.num_symbols, self.config = num_symbols, config
        def reset_state(self, **kwargs):
            return None
        def evaluate(self, feature):
            action = rng.choice((0, 1, 2)) if self.config.strategy_enable else 0
            return type("RandomSignal", (), {"symbol_id": feature.symbol_id, "sequence": feature.sequence, "action": action, "score": 0, "reason_bits": 0})()
    random_run = BacktestEngine(base_config, strategy_factory=lambda n, cfg: RandomPolicy(n, cfg)).run(events)
    results.append({"name": "random_seeded", "summary": random_run.summary, "forward_summary": random_run.forward_summary})
    return results


def run_walk_forward(events, base_config: BacktestConfig, *, train_days=20, validation_days=0, test_days=5, step_days=None):
    results = []
    for window in walk_forward_windows(events, session=base_config.session, train_days=train_days, validation_days=validation_days, test_days=test_days, step_days=step_days):
        results.append({
            "window": window.index,
            "train_dates": [str(day) for day in window.train_dates],
            "validation_dates": [str(day) for day in window.validation_dates],
            "test_dates": [str(day) for day in window.test_dates],
            "train": BacktestEngine(base_config).run(window.train).summary,
            "validation": BacktestEngine(base_config).run(window.validation).summary if window.validation else None,
            "test": BacktestEngine(base_config).run(window.test).summary,
        })
    return results


def run_sensitivity(events, base_config, parameter, values):
    results = []
    for value in values:
        strategy = replace(base_config.strategy, **{parameter: value})
        run = BacktestEngine(replace(base_config, strategy=strategy)).run(events)
        results.append({"parameter": parameter, "value": value, "summary": run.summary})
    return results


def score_calibration(run):
    """Aggregate realized forward returns by candidate score and horizon."""
    groups = {}
    for row in run.forward_summary.get("groups", []):
        key = (row["direction"], row["score"], row["horizon"])
        groups[key] = {
            "direction": row["direction"],
            "score": row["score"],
            "horizon": row["horizon"],
            "count": row["count"],
            "mean_bps": row["mean_bps"],
            "median_bps": row["median_bps"],
            "win_fraction": row["win_fraction"],
        }
    return list(groups.values())


def run_symbol_split(events, base_config: BacktestConfig, holdout_symbol_ids):
    """Run train/holdout-style symbol robustness checks without re-fitting."""
    holdout = set(holdout_symbol_ids)
    train_events = [event for event in events if event.symbol_id not in holdout]
    holdout_events = [event for event in events if event.symbol_id in holdout]
    return {
        "train_symbols": sorted({event.symbol_id for event in train_events}),
        "holdout_symbols": sorted(holdout),
        "train": BacktestEngine(base_config).run(train_events).summary,
        "holdout": BacktestEngine(base_config).run(holdout_events).summary,
    }
