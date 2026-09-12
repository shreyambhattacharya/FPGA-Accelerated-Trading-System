"""Run a deterministic Python-model versus RTL packet-pipeline comparison."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

REFERENCE_DIR = Path(__file__).resolve().parent
REPO_ROOT = REFERENCE_DIR.parents[1]
GENERATOR_DIR = REPO_ROOT / "tools" / "market_data_generator"
if str(GENERATOR_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATOR_DIR))

from generate_events import generate_events  # noqa: E402
from market_model import MarketModel, ModelOutcome  # noqa: E402


def _signed(value: int, width: int) -> int:
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _expected_outcomes(events, model: MarketModel) -> list[tuple]:
    records: list[tuple] = []
    for event in events:
        outcome = model.process_packet(event.packet())
        if outcome is None:
            continue
        if outcome.kind == "dispatch_error":
            records.append(("DISPATCH_ERROR", outcome.reason, outcome.symbol_id, outcome.sequence))
        elif outcome.kind == "reject":
            records.append(("EVENT_REJECT", outcome.reason, outcome.symbol_id, outcome.sequence))
        else:
            assert isinstance(outcome, ModelOutcome) and outcome.feature is not None
            feature = outcome.feature
            records.append(
                (
                    "FEATURE",
                    feature.symbol_id,
                    feature.sequence,
                    feature.bid_price,
                    feature.bid_quantity,
                    feature.ask_price,
                    feature.ask_quantity,
                    feature.spread,
                    int(feature.spread_valid),
                    feature.midpoint,
                    int(feature.midpoint_valid),
                    feature.momentum,
                    int(feature.momentum_valid),
                    feature.rolling_volume,
                    feature.imbalance_numerator,
                    feature.imbalance_denominator,
                    int(feature.imbalance_valid),
                    feature.vwap_sum_price_quantity,
                    feature.vwap_sum_quantity,
                    int(feature.vwap_valid),
                    feature.vwap,
                    int(feature.vwap_quotient_valid),
                    feature.imbalance_normalized,
                    int(feature.imbalance_normalized_valid),
                    feature.spread_bps_x100,
                    int(feature.spread_bps_x100_valid),
                    feature.momentum_bps_x100,
                    int(feature.momentum_bps_x100_valid),
                    feature.midpoint_minus_vwap,
                    int(feature.midpoint_minus_vwap_valid),
                    feature.midpoint_minus_vwap_bps_x100,
                    int(feature.midpoint_minus_vwap_bps_x100_valid),
                )
            )
    return records


def _parse_rtl(stdout: str) -> list[tuple]:
    records: list[tuple] = []
    for line in stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "DISPATCH_ERROR":
            if len(fields) != 4:
                raise AssertionError(f"malformed dispatcher record: {line}")
            records.append((fields[0], int(fields[1], 16), int(fields[2], 16), int(fields[3], 16)))
        elif fields[0] == "EVENT_REJECT":
            if len(fields) != 4:
                raise AssertionError(f"malformed rejection record: {line}")
            records.append((fields[0], int(fields[1], 16), int(fields[2], 16), int(fields[3], 16)))
        elif fields[0] == "FEATURE":
            if len(fields) != 32:
                raise AssertionError(f"malformed feature record ({len(fields) - 1} fields): {line}")
            values = [int(value, 16) for value in fields[1:]]
            records.append(
                (
                    "FEATURE",
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    values[5],
                    values[6],
                    values[7],
                    values[8],
                    values[9],
                    _signed(values[10], 65),
                    values[11],
                    values[12],
                    _signed(values[13], 33),
                    values[14],
                    values[15],
                    values[16],
                    values[17],
                    values[18],
                    values[19],
                    values[20],
                    _signed(values[21], 16),
                    values[22],
                    _signed(values[23], 32),
                    values[24],
                    _signed(values[25], 32),
                    values[26],
                    _signed(values[27], 65),
                    values[28],
                    _signed(values[29], 32),
                    values[30],
                )
            )
    return records


def run(count: int = 1000, seed: int = 0x5EED, keep: bool = False) -> None:
    events = generate_events(count=count, seed=seed)
    build_dir = REPO_ROOT / "fpga" / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    event_file = build_dir / "market_differential_events.hex"
    event_file.write_text("".join(event.packet().hex() + "\n" for event in events), encoding="ascii")

    vvp_file = build_dir / "tb_market_pipeline.vvp"
    sources = [
        REPO_ROOT / "fpga" / "rtl" / "protocol" / "crc8_engine.sv",
        REPO_ROOT / "fpga" / "rtl" / "protocol" / "packet_dispatcher.sv",
        REPO_ROOT / "fpga" / "rtl" / "math" / "unsigned_divider.sv",
        REPO_ROOT / "fpga" / "rtl" / "math" / "feature_normalizer.sv",
        REPO_ROOT / "fpga" / "rtl" / "market" / "market_state_engine.sv",
        REPO_ROOT / "fpga" / "tb" / "tb_market_pipeline.sv",
    ]
    compile_command = [
        "iverilog",
        "-g2012",
        "-I",
        str(REPO_ROOT / "fpga" / "rtl" / "protocol"),
        "-s",
        "tb_market_pipeline",
        "-o",
        str(vvp_file),
        *(str(source) for source in sources),
    ]
    subprocess.run(compile_command, cwd=REPO_ROOT, check=True, text=True)
    simulation = subprocess.run(
        ["vvp", str(vvp_file), f"+EVENT_FILE={event_file.as_posix()}", f"+EVENT_COUNT={count}"],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )

    expected = _expected_outcomes(events, MarketModel())
    actual = _parse_rtl(simulation.stdout)
    if actual != expected:
        limit = min(len(actual), len(expected))
        mismatch = next((index for index in range(limit) if actual[index] != expected[index]), limit)
        raise AssertionError(
            "RTL/reference mismatch at record "
            f"{mismatch}: expected={expected[mismatch] if mismatch < len(expected) else '<none>'} "
            f"actual={actual[mismatch] if mismatch < len(actual) else '<none>'}; "
            f"expected_count={len(expected)} actual_count={len(actual)}"
        )

    accepted = sum(record[0] == "FEATURE" for record in actual)
    rejected = sum(record[0] == "EVENT_REJECT" for record in actual)
    dispatcher_errors = sum(record[0] == "DISPATCH_ERROR" for record in actual)
    print(
        f"differential_test: PASS events={len(events)} outputs={len(actual)} "
        f"accepted={accepted} rejected={rejected} "
        f"dispatcher_errors={dispatcher_errors} mismatches=0"
    )
    if not keep:
        event_file.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x5EED)
    parser.add_argument("--keep", action="store_true", help="keep the generated replay file")
    args = parser.parse_args()
    run(args.count, args.seed, args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
