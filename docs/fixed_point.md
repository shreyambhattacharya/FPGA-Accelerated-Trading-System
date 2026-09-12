# Fixed-point design notes

The market path uses deterministic unsigned integer arithmetic. No floating
point is synthesized or used by the reference model.

## Price and quantity

- Price is an unsigned 64-bit integer in micro-dollars: `price = USD * 1,000,000`.
- Quantity is an unsigned 32-bit integer with no fractional scale.
- `price * quantity` is formed as an unsigned 96-bit product (`64 x 32`).
- The packet and normalized interface preserve the raw integer values.

The representable price range is `0 .. 2^64-1` micro-dollars. Supported inputs
and window sizes are chosen so the documented intermediate widths do not
overflow: a 32-entry VWAP sum uses 101 bits for `price*quantity` terms, and a
32-entry quantity sum/rolling-volume sum uses 37 bits. The RTL derives these
widths from the window parameters; the implementation keeps one minimum guard
bit for a one-entry window.

## Features

| Feature | RTL representation | Semantics |
| --- | --- | --- |
| spread | unsigned 64-bit + valid | `ask - bid` only when both sides are valid and `ask >= bid` |
| midpoint | unsigned 64-bit + valid | widened 65-bit `bid + ask`, then floor division by two |
| momentum | signed 65-bit + valid | current valid midpoint minus the oldest sample in the full 16-entry window |
| rolling volume | unsigned derived-width | circular sum of the latest `TRADE_WINDOW` quantities |
| imbalance | signed 33-bit numerator and unsigned 33-bit denominator + valid | `bid_qty - ask_qty` and `bid_qty + ask_qty`; no divider in RTL |
| VWAP | unsigned derived-width sums + valid | rolling `sum(price*quantity)` and `sum(quantity)`; no divider in RTL |

Midpoint truncation is toward zero for these non-negative operands, equivalent
to floor. Imbalance is two's-complement signed; its numerator is only valid
with a safe two-sided quote. VWAP validity means the rolling quantity sum is
nonzero, not that a quotient has been calculated.

## Warm-up and replacement

Trade and VWAP histories use circular pointers. Until a window is full, new
terms are added without subtraction. Once full, the term at the write pointer
is subtracted before the new term is added. Momentum uses only safe midpoint
observations; crossed or incomplete quotes do not advance its pointer or
count. Momentum validity starts on the first feature calculation after the
configured number of valid samples has already been collected.

The Python model in `tools/reference_model/market_model.py` implements the
same ring replacement, integer widths, validity, reset, and warm-up behavior.
Sequence numbers are compared as unsigned values; wraparound ordering is not
implemented yet and is explicitly out of scope.

## Arithmetic sharing and scheduling

The market engine has one serialized `64 x 32` price-by-quantity operation in
its trade path. The result is registered as a 96-bit term and then applied to
the rolling VWAP accumulators. Gowin maps the tested implementation to fabric
logic with 0 DSP blocks; no multiplier or divider is replicated for each
symbol. The VWAP quotient is intentionally not calculated in this milestone,
so the numerator and denominator remain exact integer accumulators for a later
consumer.

Wide arithmetic is sequenced across `READ_HISTORY`, `CAPTURE_HISTORY`,
`MULTIPLY_TRADE`, `APPLY_EVENT`, and `FEATURE_CALC`. The selected symbol's
state and the old circular-window terms are first copied into working
registers. This adds event latency, but prevents a live symbol-indexed mux
tree from sharing a cycle with the 64/96/101-bit feature arithmetic. The
registered output and valid/ready contract preserve the exact values and
validity semantics while allowing feature backpressure.
