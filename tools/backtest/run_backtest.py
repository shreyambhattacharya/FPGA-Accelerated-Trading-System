"""CLI for deterministic normalized-event replay and paper backtesting."""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    # Python puts the script directory first for ``python tools/backtest/...``.
    # Remove it because the package's ``types.py`` would otherwise shadow the
    # standard-library ``types`` module during argparse startup.
    _script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.getcwd()) != _script_dir]
    _repo_root = os.path.dirname(os.path.dirname(_script_dir))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

if __package__ in (None, ""):
    from tools.backtest.canonical import read_events
    from tools.backtest.pipeline import BacktestConfig, BacktestEngine
    from tools.backtest.reports import dataset_sha256, write_run
else:
    from .canonical import read_events
    from .pipeline import BacktestConfig, BacktestEngine
    from .reports import dataset_sha256, write_run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="canonical CSV or concatenated FPGA packet binary")
    parser.add_argument("--format", choices=("auto", "csv", "binary"), default="auto")
    parser.add_argument("--config", type=Path, help="JSON BacktestConfig override")
    parser.add_argument("--output", type=Path, help="result directory")
    parser.add_argument("--num-symbols", type=int, help="override model symbol count")
    parser.add_argument("--symbol-map", type=Path, help="JSON map of symbol IDs to provider symbols")
    parser.add_argument("--plot", action="store_true", help="write matplotlib PNG charts")
    parser.add_argument("--quality-only", action="store_true", help="validate and report without replaying")
    args = parser.parse_args()

    events = read_events(args.data, args.format)
    mapping = None
    if args.symbol_map:
        raw = json.loads(args.symbol_map.read_text(encoding="utf-8"))
        mapping = {int(key): str(value) for key, value in raw.items()}
    config_data = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    config = BacktestConfig.from_mapping(config_data, num_symbols=args.num_symbols)
    output = args.output or Path("backtest_results") / ("run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    if args.quality_only:
        from tools.backtest.data_quality import DataQualityValidator
        quality = DataQualityValidator(symbol_mapping=mapping, gap_threshold_ns=config.gap_threshold_ns).validate(events)
        output.mkdir(parents=True, exist_ok=True)
        (output / "quality.json").write_text(json.dumps(quality.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(quality.to_dict(), sort_keys=True))
        return 0

    started = time.perf_counter()
    run = BacktestEngine(config, symbol_mapping=mapping).run(events)
    elapsed = time.perf_counter() - started
    run.summary["wall_clock_seconds"] = elapsed
    run.summary["replay_events_per_second"] = len(events) / elapsed if elapsed else None
    metadata = {
        "dataset_path": str(args.data.resolve()),
        "dataset_sha256": dataset_sha256(args.data),
        "git_commit": _git_commit(),
        "real_data_downloaded": False,
        "provider": "canonical_input",
    }
    write_run(output, run, metadata=metadata, plot=args.plot)
    print(json.dumps({"output": str(output.resolve()), "summary": run.summary}, sort_keys=True, default=_json_default))
    return 0


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _json_default(value):
    return None if isinstance(value, float) and (value != value or abs(value) == float("inf")) else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
