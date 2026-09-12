"""Replay normalized CSV market events through the integer market/strategy models.

The input format is ``timestamp,event_type,symbol_id,price,quantity,side,sequence``
with an optional ``flags`` column.  The output is candidate-signal telemetry,
not orders or performance results.  ``--packet-out`` additionally writes the
exact CRC-protected 32-byte packet stream for future FPGA replay.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_model"
if str(REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(REFERENCE_DIR))

from market_model import (  # noqa: E402
    MSG_MARKET_QUOTE,
    MSG_MARKET_TRADE,
    MarketEvent,
    MarketModel,
)
from strategy_model import (  # noqa: E402
    SIGNAL_LONG_CANDIDATE,
    SIGNAL_SHORT_CANDIDATE,
    StrategyConfig,
    StrategyModel,
)


EVENT_TYPES = {
    "quote": MSG_MARKET_QUOTE,
    "market_quote": MSG_MARKET_QUOTE,
    "trade": MSG_MARKET_TRADE,
    "market_trade": MSG_MARKET_TRADE,
}


def _integer(value: str) -> int:
    return int(value.strip(), 0)


def _event_type(value: str) -> int:
    normalized = value.strip().lower()
    if normalized in EVENT_TYPES:
        return EVENT_TYPES[normalized]
    return _integer(value)


def replay(input_path: Path, output_path: Path | None, packet_path: Path | None, num_symbols: int) -> dict:
    market = MarketModel(num_symbols=num_symbols)
    strategy = StrategyModel(
        num_symbols=num_symbols,
        config=StrategyConfig(symbol_enable=[True] * num_symbols),
    )
    outputs: list[dict[str, int]] = []
    packets: list[str] = []
    accepted = 0
    rejected = 0

    with input_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(row for row in source if row.strip() and not row.lstrip().startswith("#"))
        required = {"timestamp", "event_type", "symbol_id", "price", "quantity", "side", "sequence"}
        if not required.issubset(reader.fieldnames or []):
            missing = sorted(required.difference(reader.fieldnames or []))
            raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")
        for row_number, row in enumerate(reader, start=2):
            try:
                event = MarketEvent(
                    message_type=_event_type(row["event_type"]),
                    symbol_id=_integer(row["symbol_id"]),
                    timestamp_ns=_integer(row["timestamp"]),
                    price=_integer(row["price"]),
                    quantity=_integer(row["quantity"]),
                    side=_integer(row["side"]),
                    sequence=_integer(row["sequence"]),
                    flags=_integer(row.get("flags", "0")),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid row {row_number}: {exc}") from exc

            if packet_path is not None:
                packets.append(event.packet().hex())
            outcome = market.process_event(event)
            if outcome.kind != "feature" or outcome.feature is None:
                rejected += 1
                continue
            accepted += 1
            signal = strategy.evaluate(outcome.feature)
            outputs.append(
                {
                    "symbol_id": signal.symbol_id,
                    "sequence": signal.sequence,
                    "action": signal.action,
                    "score": signal.score,
                    "reason_bits": signal.reason_bits,
                }
            )

    if output_path is not None:
        with output_path.open("w", newline="", encoding="utf-8") as destination:
            writer = csv.DictWriter(destination, fieldnames=list(outputs[0]) if outputs else
                                    ["symbol_id", "sequence", "action", "score", "reason_bits"])
            writer.writeheader()
            writer.writerows(outputs)
    if packet_path is not None:
        packet_path.write_text("\n".join(packets) + ("\n" if packets else ""), encoding="ascii")

    summary = {
        "input_events": accepted + rejected,
        "accepted_features": accepted,
        "rejected_events": rejected,
        "signals": strategy.metrics.evaluated,
        "long_candidates": strategy.metrics.long_candidates,
        "short_candidates": strategy.metrics.short_candidates,
        "no_action": strategy.metrics.no_action,
        "invalid_features": strategy.metrics.invalid,
        "suppressed": strategy.metrics.suppressed,
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="normalized market-event CSV")
    parser.add_argument("--output", type=Path, help="candidate-signal CSV output")
    parser.add_argument("--packet-out", type=Path, help="optional CRC-protected FPGA packet hex output")
    parser.add_argument("--num-symbols", type=int, default=4)
    args = parser.parse_args()
    summary = replay(args.input, args.output, args.packet_out, args.num_symbols)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
