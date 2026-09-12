"""Reproducible JSON/CSV/Markdown artifacts for a backtest run."""

from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .metrics import equity_drawdown


def dataset_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_run(output_dir: Path, run, *, metadata: dict[str, Any] | None = None, plot: bool = False) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = dict(metadata or {})
    metadata.setdefault("records_are_hypothetical", True)
    metadata.setdefault("real_data_downloaded", False)
    metadata.setdefault("broker_or_live_trading", False)
    _write_json(output_dir / "config.json", {"config": asdict(run.config), "metadata": metadata})
    _write_json(output_dir / "summary.json", run.summary)
    _write_json(output_dir / "quality.json", run.quality.to_dict())
    _write_json(output_dir / "forward_summary.json", run.forward_summary)
    _write_csv(output_dir / "trades.csv", [
        {key: value for key, value in asdict(trade).items() if key != "entry_feature"}
        for trade in run.trades
    ])
    _write_csv(output_dir / "candidates.csv", [asdict(row) for row in run.candidate_logs])
    _write_csv(output_dir / "equity_curve.csv", [asdict(row) for row in run.equity_curve])
    _write_csv(output_dir / "drawdown.csv", equity_drawdown(run.equity_curve))
    _write_csv(output_dir / "daily_pnl.csv", run.summary.get("daily_pnl", []))
    _write_csv(output_dir / "forward_returns.csv", [asdict(row) for row in run.forward_rows])
    report = _markdown_report(run, metadata)
    (output_dir / "report.md").write_text(report, encoding="utf-8")
    if plot:
        write_plots(output_dir, run)
    return output_dir


def write_plots(output_dir: Path, run) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("--plot requires matplotlib; install it separately from the core replay") from exc
    if run.equity_curve:
        x = list(range(len(run.equity_curve)))
        fig, axis = plt.subplots(figsize=(10, 4))
        axis.plot(x, [point.equity for point in run.equity_curve])
        axis.set_title("Paper equity curve")
        axis.set_xlabel("Replay observation")
        axis.set_ylabel("Equity (USD)")
        fig.tight_layout()
        fig.savefig(output_dir / "equity_curve.png", dpi=150)
        plt.close(fig)
    drawdown = equity_drawdown(run.equity_curve)
    if drawdown:
        fig, axis = plt.subplots(figsize=(10, 3))
        axis.plot(list(range(len(drawdown))), [row["drawdown"] for row in drawdown])
        axis.set_title("Paper drawdown")
        axis.set_xlabel("Replay observation")
        axis.set_ylabel("USD")
        fig.tight_layout()
        fig.savefig(output_dir / "drawdown.png", dpi=150)
        plt.close(fig)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(_json_safe(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fields)
        writer.writeheader()
        writer.writerows(_json_safe(row) for row in rows)


def _json_safe(value):
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return value


def _markdown_report(run, metadata):
    summary = run.summary
    warnings = summary.get("warnings", [])
    warning_text = "\n".join(f"- {warning}" for warning in warnings) or "- None"
    quality = run.quality
    return f"""# Historical replay and paper backtest

This report is a deterministic historical replay of the existing integer
packet → market state → normalized feature → candidate signal reference path.
The execution and portfolio sections are hypothetical research accounting;
they do not submit broker orders.

## Run summary

- Input records: {summary.get('input_events', 0)}
- Session records: {summary.get('session_events', 0)}
- Candidate signals: {summary.get('candidate_count', 0)}
- Completed trades: {summary.get('trade_count', 0)}
- Ending equity: {summary.get('ending_equity', run.config.portfolio.starting_cash):.6f} USD
- Net P&L: {summary.get('total_net_pnl', 0.0):.6f} USD
- Total costs: {summary.get('total_costs', 0.0):.6f} USD
- Replay throughput (CLI-measured): {summary.get('replay_events_per_second', 'not recorded')} events/second

## Data and quality

- Real data downloaded for this run: {metadata.get('real_data_downloaded', False)}
- Quality errors: {quality.errors}; warnings: {quality.warnings}
- Crossed quotes are retained and reported, not silently discarded.
- Dataset hash and provenance are recorded in `config.json`.

## Assumptions

- Session timezone: `{run.config.session.timezone_name}`; extended hours: `{run.config.session.extended_hours}`.
- Order latency: {run.config.execution.latency_ns / 1_000_000:.3f} ms.
- Long entries/exits use ask/bid; short entries/exits use bid/ask respectively.
- The first eligible quote at or after the latency deadline is used.
- No pyramiding, leverage, or same-event reversal is assumed unless enabled in config.
- Stop, target, maximum-hold, opposite-candidate, and end-of-day policies are recorded in `config.json`.

## Risk warnings

{warning_text}

See the CSV files for complete candidate, fill/trade, equity, drawdown, daily
P&L, and forward-return observations. Results are not a claim of profitability
and are not a substitute for live-market validation.
"""
