"""Run the first real Alpaca IEX Level-1 historical sanity study.

This command uses Alpaca for historical data only. It never imports a trading
or account API and never submits orders. Raw and normalized data are written
only below ignored ``data/`` paths; reports are written below ignored
``backtest_results/``.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from datetime import date, datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import time as time_module
import sys
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools.backtest.arrival import combined_timestamp_analysis
    from tools.backtest.canonical import NormalizedEvent, iter_binary_events
    from tools.backtest.experiments import ResearchSignalPolicy, score_calibration
    from tools.backtest.feature_analysis import suggest_search_ranges, threshold_realism
    from tools.backtest.pipeline import BacktestConfig, BacktestEngine
    from tools.backtest.providers import AlpacaDownloadError, AlpacaHistoricalAdapter, AlpacaHistoricalDownloader, load_provider_json
    from tools.backtest.reports import write_run
    from tools.backtest.streaming import iter_all_normalized, iter_json_array, iter_replay_sequences, run_streaming_replay
else:
    from .arrival import combined_timestamp_analysis
    from .canonical import NormalizedEvent, iter_binary_events
    from .experiments import ResearchSignalPolicy, score_calibration
    from .feature_analysis import suggest_search_ranges, threshold_realism
    from .pipeline import BacktestConfig, BacktestEngine
    from .providers import AlpacaDownloadError, AlpacaHistoricalAdapter, AlpacaHistoricalDownloader, load_provider_json
    from .reports import write_run
    from .streaming import iter_all_normalized, iter_json_array, iter_replay_sequences, run_streaming_replay


SYMBOLS = ("SPY", "QQQ", "NVDA", "AMD")
SYMBOL_IDS = {symbol: index for index, symbol in enumerate(SYMBOLS)}
DEFAULT_DATES = ("2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08")
NORMALIZER_VERSION = "alpaca-iex-l1-v2"


def load_env_file(path: Path) -> tuple[bool, bool]:
    """Load only the two expected values into this process without logging."""

    key_value = None
    secret_value = None
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if name.strip() == "ALPACA_API_KEY":
                key_value = value
            elif name.strip() == "ALPACA_API_SECRET":
                secret_value = value
    if key_value:
        os.environ["ALPACA_API_KEY"] = key_value
    if secret_value:
        os.environ["ALPACA_API_SECRET"] = secret_value
    return bool(os.environ.get("ALPACA_API_KEY")), bool(os.environ.get("ALPACA_API_SECRET"))


def run_real_study(
    repo_root: Path,
    *,
    dates: tuple[str, ...] = DEFAULT_DATES,
    extend_ten: bool = False,
    raw_root: Path | None = None,
    normalized_root: Path | None = None,
    results_root: Path | None = None,
) -> Path:
    raw_root = raw_root or repo_root / "data" / "raw" / "alpaca" / "iex"
    normalized_root = normalized_root or repo_root / "data" / "normalized"
    results_root = results_root or repo_root / "backtest_results"
    if extend_ten:
        raise ValueError("ten-session extension is not available in the preferred 2026-09-01..11 range; complete the core five-session study first")
    if len(dates) != 5:
        raise ValueError("the first sanity study must contain exactly five sessions")
    if any(datetime.strptime(value, "%Y-%m-%d").weekday() >= 5 for value in dates) or "2026-09-07" in dates:
        raise ValueError("dates include a weekend or the 2026-09-07 Labor Day holiday")

    dataset_label = "alpaca_iex_" + dates[0].replace("-", "") + "_" + dates[-1].replace("-", "") + "_5d"
    downloader = None
    summaries = []
    raw_paths: dict[tuple[str, str, str], Path] = {}
    for day_text in dates:
        start_utc, end_utc = _session_utc_range(day_text)
        day_path = raw_root / day_text
        for symbol in SYMBOLS:
            symbol_path = day_path / symbol
            for kind in ("quotes", "trades"):
                output_path = symbol_path / f"{kind}.json"
                metadata_path = symbol_path / f"{kind}.meta.json"
                if output_path.exists() and metadata_path.exists():
                    summary = json.loads(metadata_path.read_text(encoding="utf-8"))
                else:
                    if downloader is None:
                        downloader = AlpacaHistoricalDownloader(feed="iex")
                    summary_object = downloader.download_to(output_path, symbol=symbol, kind=kind, start=start_utc, end=end_utc)
                    summary = summary_object.to_dict()
                    summary["download_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
                    _write_json_atomic(metadata_path, summary)
                raw_paths[(day_text, symbol, kind)] = output_path
                summaries.append(summary)
                print(f"historical {day_text} {symbol} {kind} records={summary.get('records_downloaded', 0)} pages={summary.get('pages_completed', 0)}", flush=True)

    normalized_path = normalized_root / f"{dataset_label}.bin"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = repo_root / "data" / "manifests" / f"{dataset_label}.json"
    cached_manifest = _valid_cached_dataset(manifest_path, normalized_path, dataset_label)
    if cached_manifest is not None:
        canonical_counts = {
            "total_events": int(cached_manifest["canonical_event_count"]),
            "quote_events": int(cached_manifest["canonical_quote_count"]),
            "trade_events": int(cached_manifest["canonical_trade_count"]),
            "bid_events": int(cached_manifest["canonical_bid_count"]),
            "ask_events": int(cached_manifest["canonical_ask_count"]),
        }
        canonical_hash = str(cached_manifest["canonical_sha256"])
        normalization_elapsed = 0.0
        print(f"normalized_cache_reused events={canonical_counts['total_events']} bytes={normalized_path.stat().st_size} sha256={canonical_hash}", flush=True)
    else:
        normalization_started = time_module.perf_counter()
        canonical_counts = _write_normalized_stream(
            normalized_path,
            iter_replay_sequences(iter_all_normalized(raw_paths, dates, SYMBOL_IDS)),
        )
        normalization_elapsed = time_module.perf_counter() - normalization_started
        canonical_hash = _sha256(normalized_path)
        print(f"normalized_generated events={canonical_counts['total_events']} elapsed_seconds={normalization_elapsed:.3f}", flush=True)

    result_dir = _find_valid_result_dir(results_root, dataset_label, canonical_hash, canonical_counts["total_events"])
    if result_dir is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        result_dir = results_root / f"real_{dataset_label}_{run_id}"
        result_dir.mkdir(parents=True, exist_ok=False)
    else:
        result_dir.mkdir(parents=True, exist_ok=True)

    if cached_manifest is not None:
        manifest = cached_manifest
    else:
        manifest = _build_manifest(dataset_label, dates, summaries, canonical_counts, normalized_path, canonical_hash, result_dir.name.rsplit("_", 1)[-1], repo_root)
        _write_json_atomic(manifest_path, manifest)
    _write_json_atomic(result_dir / "dataset_manifest.json", manifest)
    download_summary = {"files": summaries, "total_bytes": sum(item.get("raw_file_size") or 0 for item in summaries)}
    if not (result_dir / "download_summary.json").exists():
        _write_json_atomic(result_dir / "download_summary.json", download_summary)
    else:
        download_summary = _read_json(result_dir / "download_summary.json") or download_summary

    identity = {
        "dataset_id": dataset_label,
        "sha256": canonical_hash,
        "size_bytes": normalized_path.stat().st_size,
        "event_count": canonical_counts["total_events"],
    }
    state_path = result_dir / "analysis_state.json"
    _mark_phase(state_path, "downloads", "completed", identity, artifact=str(result_dir / "download_summary.json"))
    _mark_phase(state_path, "normalization", "completed", identity, artifact=str(normalized_path))

    # The existing manifest was emitted only after the prior source-quality
    # phase returned. Reuse that provenance instead of rescanning 40 raw files
    # and the 55.7M-event canonical stream. A later run may replace this marker
    # with the richer quality artifact if one was already persisted.
    quality_path = result_dir / "quality.json"
    if quality_path.exists():
        detailed_quality = _read_json(quality_path) or {"status": "completed", "source": "cached quality.json"}
    elif cached_manifest is not None:
        detailed_quality = {
            "status": "completed_before_refactor",
            "source": "dataset manifest provenance",
            "note": "The interrupted run completed source-quality validation before writing its manifest, but did not persist quality.json; no source-quality rescan was performed.",
        }
        _write_json_atomic(quality_path, detailed_quality)
    else:
        detailed_quality = _detailed_quality(normalized_path, summaries, dates, raw_paths)
        _write_json_atomic(quality_path, detailed_quality)
    _mark_phase(state_path, "source_quality", "completed", identity, artifact=str(quality_path), note=detailed_quality.get("source", "computed"))

    config = _real_config(repo_root)
    baseline_cache_path = result_dir / "baseline_replay_summary.json"
    baseline_cache = _read_json(baseline_cache_path)
    run = None
    if _cache_matches(baseline_cache, identity):
        run_summary = baseline_cache["summary"]
        feature_stats = baseline_cache["feature_distributions"]
        summary_candidates = baseline_cache["candidate_summary"]
        forward_summary = baseline_cache["forward_summary"]
        print("baseline_replay_cache_reused", flush=True)
        _mark_phase(state_path, "baseline_replay", "completed", identity, artifact=str(baseline_cache_path))
    else:
        _mark_phase(state_path, "baseline_replay", "in_progress", identity, artifact=str(baseline_cache_path))
        started = time_module.perf_counter()
        run = run_streaming_replay(
            iter_binary_events(normalized_path, validate_crc=False),
            config,
            presequenced=True,
            progress_callback=_print_replay_progress,
            progress_total_events=canonical_counts["total_events"],
        )
        replay_elapsed = time_module.perf_counter() - started
        run.summary["strategy_replay_events_per_second"] = canonical_counts["total_events"] / replay_elapsed if replay_elapsed else None
        run.summary["full_execution_events_per_second"] = run.summary["strategy_replay_events_per_second"]
        run.summary["normalization_elapsed_seconds"] = normalization_elapsed
        run.summary["normalization_events_per_second"] = canonical_counts["total_events"] / normalization_elapsed if normalization_elapsed else None
        run_summary = run.summary
        feature_stats = run.feature_distributions
        summary_candidates = run.candidate_summary
        forward_summary = _forward_summary_by_day(run)
        baseline_cache = {
            "input_identity": identity,
            "summary": run_summary,
            "feature_distributions": feature_stats,
            "candidate_summary": summary_candidates,
            "forward_summary": forward_summary,
            "score_calibration": score_calibration(run),
        }
        _write_json_atomic(baseline_cache_path, baseline_cache)
        _write_json_atomic(result_dir / "summary.json", run_summary)
        _write_json_atomic(result_dir / "feature_distributions.json", feature_stats)
        _write_json_atomic(result_dir / "candidate_summary.json", summary_candidates)
        _write_json_atomic(result_dir / "score_calibration.json", baseline_cache["score_calibration"])
        _write_json_atomic(result_dir / "forward_summary.json", forward_summary)
        _mark_phase(state_path, "baseline_replay", "completed", identity, artifact=str(baseline_cache_path))

    combined = _read_cached_combined(result_dir / "diagnostics_combined.json", state_path, identity)
    if combined is not None:
        print("combined_diagnostics_cache_reused", flush=True)
    else:
        _mark_phase(state_path, "combined_timestamp_analysis", "in_progress", identity, artifact=str(result_dir / "diagnostics_combined.json"))
        combined = combined_timestamp_analysis(
            lambda start_index: iter_binary_events(normalized_path, validate_crc=False, start_index=start_index),
            total_events=canonical_counts["total_events"],
            checkpoint_path=result_dir / "diagnostics_checkpoint.pkl",
            state_manifest_path=state_path,
            input_identity=identity,
            progress_callback=_print_diagnostic_progress,
        )
        combined["input_identity"] = identity
        _write_json_atomic(result_dir / "diagnostics_combined.json", combined)
        _write_json_atomic(result_dir / "event_rates.json", combined["event_rates"])
        _write_json_atomic(result_dir / "interarrival.json", combined["interarrival"])
        _write_json_atomic(result_dir / "pipeline_capacity.json", combined["pipeline_capacity"])
        _mark_phase(state_path, "combined_timestamp_analysis", "completed", identity, artifact=str(result_dir / "diagnostics_combined.json"))

    combined = _ensure_rate_field_names(combined)
    combined["input_identity"] = identity
    _write_json_atomic(result_dir / "diagnostics_combined.json", combined)
    # Timestamp-only diagnostics are deliberately materialized from the one
    # combined result. No helper below is allowed to rescan the canonical file.
    _write_json_atomic(result_dir / "event_rates.json", combined["event_rates"])
    _write_json_atomic(result_dir / "interarrival.json", combined["interarrival"])
    _write_json_atomic(result_dir / "pipeline_capacity.json", combined["pipeline_capacity"])

    threshold_path = result_dir / "threshold_realism.json"
    if threshold_path.exists():
        threshold_result = _read_json(threshold_path)
    else:
        threshold_result = threshold_realism(feature_stats, config.strategy, run.candidates if run is not None else None)
        _write_json_atomic(threshold_path, threshold_result)
    ranges_path = result_dir / "threshold_search_ranges.json"
    if not ranges_path.exists():
        _write_json_atomic(ranges_path, suggest_search_ranges(feature_stats))

    parameter_path = result_dir / "parameter_sanity.json"
    if parameter_path.exists():
        parameter_results = _read_json(parameter_path)
    else:
        _mark_phase(state_path, "parameter_sanity", "in_progress", identity, artifact=str(parameter_path))
        parameter_results = _parameter_sanity(normalized_path, config, feature_stats)
        _write_json_atomic(parameter_path, parameter_results)
        _write_csv(result_dir / "parameter_sanity.csv", parameter_results["results"])
        _mark_phase(state_path, "parameter_sanity", "completed", identity, artifact=str(parameter_path))
    if not (result_dir / "parameter_sanity.csv").exists():
        _write_csv(result_dir / "parameter_sanity.csv", parameter_results["results"])

    cached_variants = {
        "baseline_comparison": (result_dir / "baseline_comparison.json", lambda: _baseline_comparison(normalized_path, config)),
        "factor_ablation": (result_dir / "factor_ablation.json", lambda: _factor_ablation(normalized_path, config)),
        "symbol_split_robustness": (result_dir / "symbol_split_robustness.json", lambda: _symbol_split(normalized_path, config, [3])),
    }
    for phase_name, (artifact_path, producer) in cached_variants.items():
        if artifact_path.exists():
            continue
        _mark_phase(state_path, phase_name, "in_progress", identity, artifact=str(artifact_path))
        _write_json_atomic(artifact_path, producer())
        _mark_phase(state_path, phase_name, "completed", identity, artifact=str(artifact_path))
    execution_path = result_dir / "execution_matrix.csv"
    if not execution_path.exists():
        _mark_phase(state_path, "execution_matrix", "in_progress", identity, artifact=str(execution_path))
        _write_csv(execution_path, _execution_matrix(normalized_path, config))
        _mark_phase(state_path, "execution_matrix", "completed", identity, artifact=str(execution_path))

    if run is not None and not (result_dir / "config.json").exists():
        write_run(result_dir, run, metadata={
            "study_type": "REAL HISTORICAL DATA SANITY STUDY",
            "provider": "Alpaca",
            "feed": "IEX",
            "symbols": list(SYMBOLS),
            "dates": list(dates),
            "dataset_id": dataset_label,
            "dataset_sha256": canonical_hash,
            "real_data_downloaded": True,
            "no_broker_orders": True,
        })
        # write_run owns generic filenames; restore the richer study-specific
        # artifacts after it emits its baseline report files.
        _write_json_atomic(result_dir / "quality.json", detailed_quality)
        _write_json_atomic(result_dir / "forward_summary.json", forward_summary)
        _write_json_atomic(result_dir / "feature_distributions.json", feature_stats)
        _write_json_atomic(result_dir / "candidate_summary.json", summary_candidates)
        _write_json_atomic(result_dir / "score_calibration.json", baseline_cache["score_calibration"])
    _write_real_report(result_dir, manifest, detailed_quality, summary_candidates, feature_stats, forward_summary, config, run_summary, summaries, parameter_results)
    _mark_phase(state_path, "report", "completed", identity, artifact=str(result_dir / "report.md"))
    return result_dir


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def _valid_cached_dataset(manifest_path: Path, normalized_path: Path, dataset_id: str):
    """Return a manifest only when the canonical cache proves its identity."""

    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("dataset_id") != dataset_id:
        return None
    try:
        event_count = int(manifest["canonical_event_count"])
        expected_hash = str(manifest["canonical_sha256"])
        if event_count <= 0 or normalized_path.stat().st_size != event_count * 32:
            return None
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if _sha256(normalized_path) != expected_hash:
        return None
    return manifest


def _find_valid_result_dir(results_root: Path, dataset_id: str, canonical_hash: str, event_count: int):
    candidates = []
    for path in results_root.glob(f"real_{dataset_id}_*"):
        if not path.is_dir():
            continue
        manifest = _read_json(path / "dataset_manifest.json")
        if not isinstance(manifest, dict):
            continue
        if manifest.get("dataset_id") != dataset_id or manifest.get("canonical_sha256") != canonical_hash:
            continue
        try:
            manifest_event_count = int(manifest.get("canonical_event_count", -1))
        except (TypeError, ValueError):
            continue
        if manifest_event_count != event_count:
            continue
        candidates.append(path)
    candidates.sort(key=lambda path: ((path / "download_summary.json").exists(), path.name), reverse=True)
    return candidates[0] if candidates else None


def _cache_matches(value, identity: dict) -> bool:
    return isinstance(value, dict) and value.get("input_identity") == identity


def _read_cached_combined(path: Path, state_path: Path, identity: dict):
    value = _read_json(path)
    state = _read_json(state_path)
    phase = state.get("phases", {}).get("combined_timestamp_analysis", {}) if isinstance(state, dict) else {}
    complete = phase.get("status") == "completed" or (isinstance(state, dict) and state.get("phase") == "combined_timestamp_analysis_complete")
    if not complete or not _cache_matches(value, identity):
        return None
    if value.get("processed_events") != identity["event_count"]:
        return None
    return value


def _ensure_rate_field_names(combined: dict) -> dict:
    """Keep combined rate artifacts compatible with the report schema."""

    for groups in combined.get("event_rates", {}).values():
        for stats in groups.values():
            stats.setdefault("mean_events_per_second", stats.get("mean"))
            stats.setdefault("median_events_per_second", stats.get("median"))
            stats.setdefault("p95_events_per_second", stats.get("p95"))
            stats.setdefault("p99_events_per_second", stats.get("p99"))
            stats.setdefault("maximum_events_per_second", stats.get("max"))
    return combined


def _mark_phase(state_path: Path, name: str, status: str, identity: dict, *, artifact: str | None = None, note: str | None = None) -> None:
    state = _read_json(state_path) or {}
    state["dataset_id"] = identity["dataset_id"]
    state["input_identity"] = identity
    phases = state.setdefault("phases", {})
    phase = {"status": status, "updated_at_utc": datetime.now(timezone.utc).isoformat()}
    if artifact is not None:
        phase["artifact"] = artifact
    if note is not None:
        phase["note"] = note
    phases[name] = phase
    if name == "combined_timestamp_analysis" and status == "in_progress":
        state["phase"] = "combined_timestamp_analysis_in_progress"
    else:
        state["phase"] = name if status != "completed" else f"{name}_complete"
    _write_json_atomic(state_path, state)


def _print_diagnostic_progress(payload: dict) -> None:
    print("diagnostic_progress " + json.dumps(payload, sort_keys=True), flush=True)


def _print_replay_progress(payload: dict) -> None:
    print("baseline_progress " + json.dumps(payload, sort_keys=True), flush=True)


def _real_config(repo_root: Path) -> BacktestConfig:
    config_path = repo_root / "configs" / "backtest" / "base.json"
    mapping = json.loads(config_path.read_text(encoding="utf-8"))
    mapping["num_symbols"] = 4
    mapping.setdefault("strategy", {})["symbol_enable"] = [True] * 4
    return BacktestConfig.from_mapping(mapping)


def _session_utc_range(day_text: str) -> tuple[str, str]:
    zone = ZoneInfo("America/New_York")
    local_date = datetime.strptime(day_text, "%Y-%m-%d").date()
    start = datetime.combine(local_date, time(9, 30), tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(local_date, time(16, 0), tzinfo=zone).astimezone(timezone.utc)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def _write_normalized_stream(path: Path, events) -> dict[str, int]:
    partial = path.with_name(path.name + ".part")
    counts = {"total_events": 0, "quote_events": 0, "trade_events": 0, "bid_events": 0, "ask_events": 0}
    try:
        with partial.open("wb") as destination:
            for event in events:
                destination.write(event.to_market_event().packet())
                counts["total_events"] += 1
                if event.event_type == 1:
                    counts["quote_events"] += 1
                    counts["bid_events" if event.side == 0 else "ask_events"] += 1
                elif event.event_type == 2:
                    counts["trade_events"] += 1
        os.replace(partial, path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return counts


def _build_manifest(dataset_id, dates, summaries, canonical_counts, normalized_path, canonical_hash, run_id, repo_root):
    return {
        "dataset_id": dataset_id,
        "provider": "alpaca",
        "feed": "iex",
        "symbols": list(SYMBOLS),
        "symbol_ids": SYMBOL_IDS,
        "trading_dates": list(dates),
        "requested_utc_ranges": {day: _session_utc_range(day) for day in dates},
        "session": {"timezone": "America/New_York", "regular_start": "09:30:00", "regular_end": "16:00:00", "extended_hours": False},
        "raw_files": summaries,
        "raw_file_count": len(summaries),
        "raw_record_count": sum(item.get("records_downloaded", 0) for item in summaries),
        "canonical_data_path": str(normalized_path),
        "canonical_sha256": canonical_hash,
        "canonical_event_count": canonical_counts["total_events"],
        "canonical_quote_count": canonical_counts["quote_events"],
        "canonical_trade_count": canonical_counts["trade_events"],
        "canonical_bid_count": canonical_counts["bid_events"],
        "canonical_ask_count": canonical_counts["ask_events"],
        "normalizer_version": NORMALIZER_VERSION,
        "git_commit": _git_commit(repo_root),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_label": "REAL HISTORICAL DATA SANITY STUDY",
    }


def _detailed_quality(normalized_path, summaries, dates, raw_paths):
    by_symbol = {
        symbol: {
            "symbol_id": symbol_id,
            "raw_quote_records": sum(item.get("records_downloaded", 0) for item in summaries if item.get("symbol") == symbol and item.get("kind") == "quotes"),
            "raw_trade_records": sum(item.get("records_downloaded", 0) for item in summaries if item.get("symbol") == symbol and item.get("kind") == "trades"),
            "canonical_bid_events": 0, "canonical_ask_events": 0, "canonical_trade_events": 0, "total_canonical_events": 0,
            "out_of_order_source_records": 0, "duplicate_source_records": 0, "duplicate_timestamps": 0,
            "locked_quotes": 0, "crossed_quotes": 0, "zero_bid_prices": 0, "zero_ask_prices": 0, "zero_trade_prices": 0,
            "zero_bid_sizes": 0, "zero_ask_sizes": 0, "zero_trade_sizes": 0,
            "first_timestamp_ns": None, "last_timestamp_ns": None,
        }
        for symbol, symbol_id in SYMBOL_IDS.items()
    }
    books = {symbol_id: {"bid": None, "ask": None} for symbol_id in SYMBOL_IDS.values()}
    previous_timestamp = None
    previous_by_symbol = {}
    largest_gap = 0
    negative_values = 0
    invalid_values = 0
    total_events = 0
    source_quality = {
        (day, symbol, kind): _source_quality(raw_paths[(day, symbol, kind)], kind)
        for day in dates
        for symbol in SYMBOL_IDS
        for kind in ("quotes", "trades")
    }
    for event in iter_binary_events(normalized_path, validate_crc=False):
        total_events += 1
        symbol = SYMBOLS[event.symbol_id]
        row = by_symbol[symbol]
        row["total_canonical_events"] += 1
        row["first_timestamp_ns"] = event.timestamp_ns if row["first_timestamp_ns"] is None else row["first_timestamp_ns"]
        row["last_timestamp_ns"] = event.timestamp_ns
        if previous_timestamp is not None:
            largest_gap = max(largest_gap, event.timestamp_ns - previous_timestamp)
            if event.timestamp_ns == previous_timestamp:
                aggregate_duplicate_timestamp = True
            else:
                aggregate_duplicate_timestamp = False
        else:
            aggregate_duplicate_timestamp = False
        if previous_by_symbol.get(event.symbol_id) == event.timestamp_ns:
            row["duplicate_timestamps"] += 1
        previous_by_symbol[event.symbol_id] = event.timestamp_ns
        previous_timestamp = event.timestamp_ns
        negative_values += event.price < 0 or event.quantity < 0
        invalid_values += event.price <= 0 or event.quantity <= 0
        if event.event_type == 1:
            if event.side == 0:
                row["canonical_bid_events"] += 1
                row["zero_bid_prices"] += event.price == 0
                row["zero_bid_sizes"] += event.quantity == 0
                books[event.symbol_id]["bid"] = event.price
            else:
                row["canonical_ask_events"] += 1
                row["zero_ask_prices"] += event.price == 0
                row["zero_ask_sizes"] += event.quantity == 0
                books[event.symbol_id]["ask"] = event.price
            bid, ask = books[event.symbol_id]["bid"], books[event.symbol_id]["ask"]
            if bid is not None and ask is not None:
                row["locked_quotes"] += bid == ask
                row["crossed_quotes"] += ask < bid
        else:
            row["canonical_trade_events"] += 1
            row["zero_trade_prices"] += event.price == 0
            row["zero_trade_sizes"] += event.quantity == 0
    for symbol, row in by_symbol.items():
        row["out_of_order_source_records"] = sum(source_quality[(day, symbol, kind)]["out_of_order_records"] for day in dates for kind in ("quotes", "trades"))
        row["duplicate_source_records"] = sum(source_quality[(day, symbol, kind)]["duplicate_records"] for day in dates for kind in ("quotes", "trades"))
    return {
        "dates": list(dates),
        "by_symbol": by_symbol,
        "aggregate": {
            "raw_quote_records": sum(item.get("records_downloaded", 0) for item in summaries if item.get("kind") == "quotes"),
            "raw_trade_records": sum(item.get("records_downloaded", 0) for item in summaries if item.get("kind") == "trades"),
            "canonical_bid_events": sum(item["canonical_bid_events"] for item in by_symbol.values()),
            "canonical_ask_events": sum(item["canonical_ask_events"] for item in by_symbol.values()),
            "canonical_trade_events": sum(item["canonical_trade_events"] for item in by_symbol.values()),
            "total_canonical_events": total_events,
            "out_of_order_source_records": sum(item["out_of_order_source_records"] for item in by_symbol.values()),
            "duplicate_source_records": sum(item["duplicate_source_records"] for item in by_symbol.values()),
            "duplicate_timestamps": sum(item["duplicate_timestamps"] for item in by_symbol.values()),
            "largest_event_gap_ns": largest_gap,
            "first_event_timestamp_ns": min((item["first_timestamp_ns"] for item in by_symbol.values() if item["first_timestamp_ns"] is not None), default=None),
            "last_event_timestamp_ns": max((item["last_timestamp_ns"] for item in by_symbol.values() if item["last_timestamp_ns"] is not None), default=None),
            "negative_values": negative_values,
            "invalid_values": invalid_values,
            "malformed_records": sum(1 for item in summaries if "malformed_record" in item.get("warnings", [])),
        },
    }


def _source_quality(path: Path, kind: str) -> dict[str, int]:
    try:
        from .providers import timestamp_to_ns
    except ImportError:
        from tools.backtest.providers import timestamp_to_ns
    previous_timestamp = None
    previous_fingerprint = None
    out_of_order_records = 0
    duplicate_records = 0
    for record in iter_json_array(path, kind):
        fingerprint = repr(record)
        if fingerprint == previous_fingerprint:
            duplicate_records += 1
        try:
            timestamp = timestamp_to_ns(record.get("t", record.get("timestamp")))
            if previous_timestamp is not None and timestamp < previous_timestamp:
                out_of_order_records += 1
            previous_timestamp = timestamp
        except (TypeError, ValueError):
            continue
    return {
        "out_of_order_records": out_of_order_records,
        "duplicate_records": duplicate_records,
    }


def _forward_summary_by_day(run):
    by_day = defaultdict(list)
    zone = ZoneInfo("America/New_York")
    for row in run.forward_rows:
        day = datetime.fromtimestamp(row.candidate_timestamp_ns / 1_000_000_000, tz=timezone.utc).astimezone(zone).date().isoformat()
        by_day[day].append(row)
    return {"all": run.forward_summary, "by_day": {day: _summarize_rows(rows) for day, rows in sorted(by_day.items())}}


def _summarize_rows(rows):
    from statistics import mean, median
    groups = defaultdict(list)
    for row in rows:
        if row.return_bps is not None:
            groups[(row.direction, row.symbol_id, row.score, row.horizon)].append(row.return_bps)
    result = []
    for key, values in sorted(groups.items(), key=str):
        direction, symbol_id, score, horizon = key
        ordered = sorted(values)
        def q(fraction):
            return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))]
        result.append({"direction": direction, "symbol_id": symbol_id, "score": score, "horizon": horizon, "count": len(values), "mean_bps": mean(values), "median_bps": median(values), "p5_bps": q(.05), "p10_bps": q(.10), "p25_bps": q(.25), "p75_bps": q(.75), "p90_bps": q(.90), "p95_bps": q(.95), "favorable_fraction": sum(value > 0 for value in values) / len(values)})
    return result


VARIANT_EVENT_STRIDE = 128


def _stream_run(normalized_path, config, *, strategy_factory=None, symbol_filter=None, sample_stride=VARIANT_EVENT_STRIDE, progress_label=None):
    """Run a bounded exploratory variant over the full five-session span.

    The exact baseline uses every canonical event. Variant probes retain the
    complete date/symbol span but use a deterministic event stride so the
    large study remains a practical sanity workflow rather than an
    overnight parameter sweep. The sampling metadata is attached to every
    variant summary.
    """
    events = iter_binary_events(normalized_path, validate_crc=False, step=sample_stride)
    if symbol_filter is not None:
        events = (event for event in events if event.symbol_id in symbol_filter)
    progress_callback = None
    progress_total_events = None
    if progress_label:
        total_input_events = normalized_path.stat().st_size // 32
        progress_total_events = (total_input_events + sample_stride - 1) // sample_stride

        def progress_callback(payload):
            labeled = dict(payload)
            labeled["operation"] = progress_label
            print("variant_progress " + json.dumps(labeled, sort_keys=True), flush=True)

    run = run_streaming_replay(
        events,
        config,
        strategy_factory=strategy_factory,
        presequenced=sample_stride == 1,
        progress_callback=progress_callback,
        progress_total_events=progress_total_events,
    )
    if sample_stride > 1:
        run.summary["variant_replay_sampling"] = {"mode": "deterministic_event_stride", "stride": sample_stride, "full_span_retained": True}
    return run


def _baseline_comparison(normalized_path, config):
    result = []
    no_trade = replace(config, strategy=replace(config.strategy, strategy_enable=False))
    no_trade_run = _stream_run(normalized_path, no_trade, progress_label="baseline_comparison_no_trade")
    result.append({"name": "no_trade", "summary": no_trade_run.summary, "forward_summary": no_trade_run.forward_summary})
    for name, excluded in (("momentum_only", {"vwap_delta", "imbalance", "spread", "volume"}), ("vwap_only", {"momentum", "imbalance", "spread", "volume"})):
        factory = lambda n, cfg, excluded=excluded: ResearchSignalPolicy(n, cfg, excluded)
        run = _stream_run(normalized_path, config, strategy_factory=factory, progress_label=f"baseline_comparison_{name}")
        result.append({"name": name, "summary": run.summary, "forward_summary": run.forward_summary})
    import random
    rng = random.Random(config.seed)
    class RandomPolicy:
        def __init__(self, num_symbols, policy_config):
            self.num_symbols = num_symbols
            self.config = policy_config
        def reset_state(self, **kwargs):
            return None
        def evaluate(self, feature):
            action = rng.choice((0, 1, 2)) if self.config.strategy_enable else 0
            return type("RandomSignal", (), {"symbol_id": feature.symbol_id, "sequence": feature.sequence, "action": action, "score": 0, "reason_bits": 0})()
    random_run = _stream_run(normalized_path, config, strategy_factory=lambda n, cfg: RandomPolicy(n, cfg), progress_label="baseline_comparison_random_seeded")
    result.append({"name": "random_seeded", "summary": random_run.summary, "forward_summary": random_run.forward_summary})
    return result


def _factor_ablation(normalized_path, config):
    results = []
    for factor in (None, "momentum", "vwap_delta", "imbalance", "spread", "volume"):
        excluded = set() if factor is None else {factor}
        factory = None if factor is None else lambda n, cfg, excluded=excluded: ResearchSignalPolicy(n, cfg, excluded)
        run = _stream_run(normalized_path, config, strategy_factory=factory, progress_label=f"factor_ablation_{factor or 'none'}")
        results.append({"excluded_factor": factor or "none", "summary": run.summary, "forward_summary": run.forward_summary})
    return results


def _execution_matrix(normalized_path, config):
    rows = []
    for latency_ms in (0, 1, 5, 10, 25, 50, 100):
        for slippage_bps in (0.0, 0.5, 1.0, 2.0):
            execution = replace(config.execution, latency_ns=latency_ms * 1_000_000, slippage_bps=slippage_bps)
            run = _stream_run(normalized_path, replace(config, execution=execution), progress_label=f"execution_matrix_latency_{latency_ms}_slippage_{slippage_bps}")
            summary = run.summary
            rows.append({"latency_ms": latency_ms, "slippage_bps": slippage_bps, "completed_trades": summary["trade_count"], "unfilled_orders": summary["unfilled_orders"], "gross_pnl": summary["total_gross_pnl"], "slippage_cost": summary["total_slippage_cost"], "commission": summary["total_commission"], "net_pnl": summary["total_net_pnl"], "return": summary["total_return"], "win_rate": summary["win_rate"], "profit_factor": summary["profit_factor"], "expectancy": summary["expectancy"], "max_drawdown": summary["max_drawdown"], "average_hold_seconds": summary["average_holding_seconds"], "turnover": summary["turnover"]})
    return rows


def _parameter_sanity(normalized_path, config, feature_stats):
    ranges = suggest_search_ranges(feature_stats)
    momentum = [value for value in (ranges["momentum_bps_x100"].get("p25"), ranges["momentum_bps_x100"].get("p75")) if value is not None]
    vwap = [value for value in (ranges["midpoint_minus_vwap_bps_x100"].get("p25"), ranges["midpoint_minus_vwap_bps_x100"].get("p75")) if value is not None]
    grid = {
        "long_min_momentum_bps_x100": momentum or [config.strategy.long_min_momentum_bps_x100],
        "long_min_vwap_delta_bps_x100": vwap or [config.strategy.long_min_vwap_delta_bps_x100],
        "long_min_imbalance_q15": [config.strategy.long_min_imbalance_q15, 0],
        "max_spread_bps_x100": [config.strategy.max_spread_bps_x100, max(1, config.strategy.max_spread_bps_x100 * 2)],
    }
    results = []
    from itertools import product
    keys = sorted(grid)
    for values in product(*(grid[key] for key in keys)):
        overrides = dict(zip(keys, values))
        strategy = replace(config.strategy, **overrides)
        run = _stream_run(normalized_path, replace(config, strategy=strategy), progress_label="parameter_sanity")
        results.append({"parameters": overrides, "trade_count": run.summary["trade_count"], "candidate_count": run.summary["candidate_count"], "net_pnl": run.summary["total_net_pnl"], "expectancy": run.summary["expectancy"], "max_drawdown": run.summary["max_drawdown"], "risk_adjusted": run.summary["risk_adjusted"]})
    return {"purpose": "exploratory threshold-scale sanity only; not final optimization", "max_combinations": 500, "ranking_note": "not ranked solely by profit; inspect trade count, expectancy, drawdown, and stability artifacts", "results": sorted(results, key=lambda row: (row["expectancy"] is not None, row["expectancy"] or float("-inf")), reverse=True)}


def _symbol_split(normalized_path, config, holdout_ids):
    holdout = set(holdout_ids)
    train = _stream_run(normalized_path, config, symbol_filter=set(SYMBOL_IDS.values()) - holdout, progress_label="symbol_split_train")
    test = _stream_run(normalized_path, config, symbol_filter=holdout, progress_label="symbol_split_holdout")
    return {"train_symbols": sorted(set(SYMBOL_IDS.values()) - holdout), "holdout_symbols": sorted(holdout), "train": train.summary, "holdout": test.summary}


def _write_real_report(result_dir, manifest, quality, candidates, feature_stats, forward_summary, config, summary, download_summaries, parameter_results):
    rate = json.loads((result_dir / "event_rates.json").read_text(encoding="utf-8"))
    capacity = json.loads((result_dir / "pipeline_capacity.json").read_text(encoding="utf-8"))
    lines = [
        "# REAL HISTORICAL DATA SANITY STUDY",
        "",
        "Provider: Alpaca  ",
        "Feed: IEX  ",
        "Symbols: SPY, QQQ, NVDA, AMD  ",
        f"Dates: {', '.join(manifest['trading_dates'])}",
        "",
        "> IEX is a limited single-exchange sample and is not equivalent to consolidated SIP data.",
        "> This five-session study is exploratory/sanity validation, not evidence of durable profitability.",
        "> No broker orders were submitted; all execution results are offline simulations.",
        "> Existing FPGA market/feature/strategy semantics were preserved.",
        "",
        "## Data and pipeline",
        "",
        f"Canonical events: {manifest['canonical_event_count']} (bid {manifest['canonical_bid_count']}, ask {manifest['canonical_ask_count']}, trades {manifest['canonical_trade_count']}).",
        f"Raw downloaded bytes: {sum(item.get('raw_file_size') or 0 for item in download_summaries)}.",
        f"Maximum event rates: 1s={rate['1s']['combined']['maximum_events_per_second']:.3f}/s, 100ms={rate['100ms']['combined']['maximum_events_per_second']:.3f}/s, 10ms={rate['10ms']['combined']['maximum_events_per_second']:.3f}/s, 1ms={rate['1ms']['combined']['maximum_events_per_second']:.3f}/s.",
        f"Conservative queue simulation: infinite maximum occupancy {capacity['infinite_queue']['max_occupancy']}; tested depths with zero overflow: {', '.join(depth for depth, item in capacity['finite_depths'].items() if item['overflow_events'] == 0) or 'none'}.",
        "",
        "## Baseline and interpretation",
        "",
        f"Features: {candidates['total_feature_records']}; SIGNAL_NONE={candidates['action_counts'].get('SIGNAL_NONE', 0)}; LONG={candidates['action_counts'].get('LONG_CANDIDATE', 0)}; SHORT={candidates['action_counts'].get('SHORT_CANDIDATE', 0)}.",
        f"Completed trades: {summary.get('trade_count', 0)}; gross P&L={summary.get('total_gross_pnl', 0.0):.6f}; net P&L={summary.get('total_net_pnl', 0.0):.6f}; max drawdown={summary.get('max_drawdown', 0.0):.6f}.",
        f"Replay throughput: {summary.get('strategy_replay_events_per_second', 0.0):.3f} events/s.",
        "",
        "Feature distributions, threshold realism, forward returns, latency/slippage matrix, ablations, and parameter sanity results are stored in the adjacent JSON/CSV artifacts.",
        "",
        "## Limitations",
        "",
        "IEX coverage is not consolidated US-market coverage. Five sessions are too short for durable statistical conclusions; no final out-of-sample claim or optimal parameter selection is made. Historical quote size was converted from round lots to shares and trade size was retained as shares; trade aggressor side is defaulted because the historical trade response does not provide reliable buyer/seller initiation.",
    ]
    (result_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json_atomic(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    partial.write_text(json.dumps(value, indent=2, sort_keys=True, default=_json_default) + "\n", encoding="utf-8")
    os.replace(partial, path)


def _write_csv(path: Path, rows):
    import csv
    rows = list(rows)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_commit(repo_root: Path) -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _json_default(value):
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        return None
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--extend-ten", action="store_true")
    args = parser.parse_args()
    key_present, secret_present = load_env_file(args.repo_root / ".env.local")
    print(f"ALPACA_API_KEY_PRESENT={key_present}")
    print(f"ALPACA_API_SECRET_PRESENT={secret_present}")
    if not key_present or not secret_present:
        raise SystemExit("Alpaca credentials unavailable; real-data download not attempted")
    result = run_real_study(args.repo_root, extend_ten=args.extend_ten)
    print(json.dumps({"result_dir": str(result.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
