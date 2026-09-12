"""Generate deterministic normalized market events.

The default stream is exactly 1,000 packets and deliberately contains both
directed corner cases and seeded pseudo-random traffic. Output is one 32-byte
packet per line in hexadecimal, which makes it convenient for a simulator,
hex dump, or a later host-side replay tool.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import random
import sys

_REFERENCE_DIR = Path(__file__).resolve().parents[1] / "reference_model"
if str(_REFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(_REFERENCE_DIR))

from market_model import (  # noqa: E402
    MSG_MARKET_QUOTE,
    MSG_MARKET_TRADE,
    MarketEvent,
)

SYMBOL_IDS = {0: "SPY", 1: "QQQ", 2: "NVDA", 3: "AMD"}
BASE_PRICES = {0: 450_000_000, 1: 380_000_000, 2: 120_000_000, 3: 160_000_000}


def generate_events(count: int = 1000, seed: int = 0x5EED) -> list[MarketEvent]:
    """Return a reproducible event stream with all four starter symbols."""

    if count < 1:
        raise ValueError("count must be positive")

    events: list[MarketEvent] = []
    next_sequence = [0, 0, 0, 0]
    timestamp = 1_000_000_000

    def append(
        message_type: int,
        symbol_id: int,
        price: int,
        quantity: int,
        side: int,
        sequence: int,
    ) -> None:
        nonlocal timestamp
        events.append(
            MarketEvent(
                message_type=message_type,
                symbol_id=symbol_id,
                timestamp_ns=timestamp,
                price=price,
                quantity=quantity,
                side=side,
                sequence=sequence,
                flags=0,
            )
        )
        timestamp += 1_000

    def valid_event(message_type: int, symbol_id: int, price: int, quantity: int, side: int) -> None:
        next_sequence[symbol_id] += 1
        append(message_type, symbol_id, price, quantity, side, next_sequence[symbol_id])

    # Establish both quote sides for each symbol, including the zero-quantity
    # edge case on SPY. The IDs map to SPY, QQQ, NVDA, and AMD.
    for symbol_id in range(4):
        base = BASE_PRICES[symbol_id]
        valid_event(MSG_MARKET_QUOTE, symbol_id, base - 1_000_000, 1_000 + symbol_id, 0)
        valid_event(MSG_MARKET_QUOTE, symbol_id, base + 1_000_000, 2_000 + symbol_id, 1)
    valid_event(MSG_MARKET_TRADE, 0, BASE_PRICES[0], 0, 0)

    # Crossed quote must be visible in the feature record but must not append a
    # midpoint sample. The following ask repairs the quote and resumes it.
    valid_event(MSG_MARKET_QUOTE, 2, BASE_PRICES[2] + 3_000_000, 777, 0)
    valid_event(MSG_MARKET_QUOTE, 2, BASE_PRICES[2] + 4_000_000, 888, 1)

    # Fill and wrap the SPY trade and VWAP windows with known values.
    for index in range(48):
        valid_event(
            MSG_MARKET_TRADE,
            0,
            BASE_PRICES[0] + index * 10_000,
            10 + index,
            index & 1,
        )

    # More than one momentum window of valid midpoint observations. These are
    # quote updates, not trades, so trade/VWAP histories remain isolated.
    for index in range(20):
        valid_event(
            MSG_MARKET_QUOTE,
            1,
            BASE_PRICES[1] - 1_000_000 + (index % 5) * 100_000,
            3_000 + index,
            0,
        )

    # Maximum representable unsigned fields exercise widened products and
    # accumulators without using floating point.
    valid_event(MSG_MARKET_QUOTE, 3, (1 << 64) - 101, (1 << 32) - 1, 0)
    valid_event(MSG_MARKET_QUOTE, 3, (1 << 64) - 1, (1 << 32) - 1, 1)
    valid_event(MSG_MARKET_TRADE, 3, (1 << 64) - 1, (1 << 32) - 1, 1)

    # Invalid symbol is rejected by the dispatcher before the state engine.
    append(MSG_MARKET_QUOTE, 0xFFFF, 123_000_000, 5, 0, 1)

    # Invalid side, duplicate, and stale sequence cases are intentionally
    # left in the stream. Invalid events do not advance the per-symbol seq.
    append(MSG_MARKET_QUOTE, 0, BASE_PRICES[0], 55, 2, next_sequence[0] + 1)
    append(MSG_MARKET_QUOTE, 0, BASE_PRICES[0], 55, 0, next_sequence[0])
    append(MSG_MARKET_QUOTE, 0, BASE_PRICES[0], 55, 0, max(next_sequence[0] - 1, 0))

    rng = random.Random(seed)
    while len(events) < count:
        symbol_id = rng.randrange(4)
        base = BASE_PRICES[symbol_id]
        is_trade = rng.randrange(4) != 0
        message_type = MSG_MARKET_TRADE if is_trade else MSG_MARKET_QUOTE
        side = rng.randrange(2)
        quantity = rng.randrange(0, 100_000)
        price = max(1, base + rng.randrange(-3_000_000, 3_000_001))

        selector = rng.randrange(32)
        if selector == 0:
            # A bad side is rejected and therefore does not consume a seq.
            side = 2
            sequence = next_sequence[symbol_id] + 1
        elif selector == 1 and next_sequence[symbol_id] > 0:
            sequence = next_sequence[symbol_id]
        elif selector == 2 and next_sequence[symbol_id] > 1:
            sequence = next_sequence[symbol_id] - 1
        else:
            next_sequence[symbol_id] += 1
            sequence = next_sequence[symbol_id]
        append(message_type, symbol_id, price, quantity, side, sequence)

    return events[:count]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--seed", type=lambda value: int(value, 0), default=0x5EED)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    events = generate_events(args.count, args.seed)
    text = "".join(event.packet().hex() + "\n" for event in events)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="ascii")
    else:
        sys.stdout.write(text)
    print(f"generated_events={len(events)} seed=0x{args.seed:X}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
