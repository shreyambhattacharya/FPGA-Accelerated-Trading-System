"""Performance, risk, exposure, and drawdown summaries for replay results."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
from statistics import mean, pstdev


def daily_pnl_series(equity_curve, *, timezone_name: str = "America/New_York", starting_equity: float | None = None) -> list[dict]:
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(timezone_name)
    by_date: dict[str, object] = {}
    for point in equity_curve:
        day = datetime.fromtimestamp(point.timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(zone).date().isoformat()
        by_date[day] = point
    result = []
    previous = None
    for day, point in sorted(by_date.items()):
        pnl = point.equity - (previous.equity if previous is not None else (starting_equity if starting_equity is not None else point.equity))
        result.append({"date": day, "equity": point.equity, "pnl": pnl})
        previous = point
    return result


def equity_drawdown(equity_curve) -> list[dict]:
    high_water = None
    result = []
    for point in equity_curve:
        high_water = point.equity if high_water is None else max(high_water, point.equity)
        result.append({
            "timestamp_ns": point.timestamp_ns,
            "equity": point.equity,
            "high_water": high_water,
            "drawdown": point.equity - high_water,
            "drawdown_pct": (point.equity / high_water - 1.0) if high_water else 0.0,
        })
    return result


def compute_metrics(trades, equity_curve, *, starting_cash: float, timezone_name: str = "America/New_York") -> dict:
    warnings: list[str] = []
    total_net = sum(trade.net_pnl for trade in trades)
    total_gross = sum(trade.gross_pnl for trade in trades)
    total_costs = sum(trade.costs for trade in trades)
    winners = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losers = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    daily = daily_pnl_series(equity_curve, timezone_name=timezone_name, starting_equity=starting_cash)
    returns = []
    previous_equity = starting_cash
    for row in daily:
        if previous_equity:
            returns.append(row["equity"] / previous_equity - 1.0)
        previous_equity = row["equity"]
    drawdown = equity_drawdown(equity_curve)
    max_dd = min((row["drawdown"] for row in drawdown), default=0.0)
    max_dd_pct = min((row["drawdown_pct"] for row in drawdown), default=0.0)
    max_dd_duration_ns = _max_drawdown_duration(drawdown)
    exposure = [point.gross_exposure for point in equity_curve]
    open_counts = [point.open_positions for point in equity_curve]
    span_days = ((equity_curve[-1].timestamp_ns - equity_curve[0].timestamp_ns) / 86_400_000_000_000) if len(equity_curve) > 1 else 0
    if len(daily) < 20 or span_days < 5:
        warnings.append("Sharpe/Sortino/Calmar are withheld for short samples (<20 daily observations or <5 calendar days).")
    risk = _risk_metrics(returns, max_dd_pct, span_days, warnings)
    by_symbol = defaultdict(lambda: {"trades": 0, "net_pnl": 0.0, "gross_pnl": 0.0, "costs": 0.0})
    turnover = 0.0
    for trade in trades:
        item = by_symbol[trade.symbol_id]
        item["trades"] += 1
        item["net_pnl"] += trade.net_pnl
        item["gross_pnl"] += trade.gross_pnl
        item["costs"] += trade.costs
        turnover += (trade.entry_price + trade.exit_price) * trade.quantity / 1_000_000.0
    return {
        "starting_cash": starting_cash,
        "ending_equity": equity_curve[-1].equity if equity_curve else starting_cash,
        "total_return": total_net / starting_cash if starting_cash else None,
        "total_net_pnl": total_net,
        "total_gross_pnl": total_gross,
        "total_costs": total_costs,
        "trade_count": len(trades),
        "long_trades": sum(trade.direction == "long" for trade in trades),
        "short_trades": sum(trade.direction == "short" for trade in trades),
        "win_rate": len(winners) / len(trades) if trades else None,
        "average_trade_pnl": mean([trade.net_pnl for trade in trades]) if trades else None,
        "average_winner": mean(winners) if winners else None,
        "average_loser": mean(losers) if losers else None,
        "largest_win": max(winners, default=None),
        "largest_loss": min(losers, default=None),
        "profit_factor": sum(winners) / abs(sum(losers)) if losers else (None if not winners else math.inf),
        "expectancy": mean([trade.net_pnl for trade in trades]) if trades else None,
        "average_holding_seconds": mean([trade.holding_duration_ns for trade in trades]) / 1_000_000_000 if trades else None,
        "turnover": turnover,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "max_drawdown_duration_seconds": max_dd_duration_ns / 1_000_000_000,
        "average_gross_exposure": mean(exposure) if exposure else 0.0,
        "maximum_gross_exposure": max(exposure, default=0.0),
        "average_concurrent_positions": mean(open_counts) if open_counts else 0.0,
        "daily_pnl": daily,
        "pnl_by_symbol": {str(key): value for key, value in sorted(by_symbol.items())},
        "risk_adjusted": risk,
        "warnings": warnings,
    }


def _risk_metrics(returns, max_dd_pct, span_days, warnings):
    if len(returns) < 20 or span_days < 5:
        return {"sharpe": None, "sortino": None, "calmar": None}
    daily_mean = mean(returns)
    daily_std = pstdev(returns)
    downside = [min(value, 0.0) for value in returns]
    downside_dev = math.sqrt(sum(value * value for value in downside) / len(downside))
    annualized = (1.0 + daily_mean) ** 252 - 1.0
    if max_dd_pct >= 0:
        warnings.append("Calmar is unavailable because no drawdown was observed.")
    return {
        "sharpe": daily_mean / daily_std * math.sqrt(252) if daily_std else None,
        "sortino": daily_mean / downside_dev * math.sqrt(252) if downside_dev else None,
        "calmar": annualized / abs(max_dd_pct) if max_dd_pct < 0 else None,
    }


def _max_drawdown_duration(drawdown) -> int:
    start = None
    maximum = 0
    for row in drawdown:
        if row["drawdown"] < 0 and start is None:
            start = row["timestamp_ns"]
        if row["drawdown"] == 0 and start is not None:
            maximum = max(maximum, row["timestamp_ns"] - start)
            start = None
    if start is not None and drawdown:
        maximum = max(maximum, drawdown[-1]["timestamp_ns"] - start)
    return maximum
