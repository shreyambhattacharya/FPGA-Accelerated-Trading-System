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
| imbalance | signed 33-bit numerator and unsigned 33-bit denominator + valid | `bid_qty - ask_qty` and `bid_qty + ask_qty` |
| VWAP | unsigned derived-width sums + valid, plus unsigned 64-bit quotient + quotient-valid | rolling sums and `sum(price*quantity) // sum(quantity)` |
| normalized imbalance | signed 16-bit Q1.15 + valid | `(bid_qty - ask_qty) * 2^IMBALANCE_FRAC_BITS // (bid_qty + ask_qty)`, saturated to `[-32768, 32767]` |
| spread bps | signed 32-bit scaled by `BPS_OUTPUT_SCALE` + valid | `spread * BPS_SCALE * BPS_OUTPUT_SCALE // midpoint` |
| momentum bps | signed 32-bit scaled by `BPS_OUTPUT_SCALE` + valid | signed `momentum * BPS_SCALE * BPS_OUTPUT_SCALE / reference_midpoint`, truncated toward zero |
| midpoint-minus-VWAP | signed 65-bit raw delta + valid, plus signed 32-bit bps output + valid | `midpoint - vwap`; normalized ratio uses VWAP as denominator |

Midpoint truncation is toward zero for these non-negative operands, equivalent
to floor. Imbalance is two's-complement signed; its numerator is only valid
with a safe two-sided quote. `feature_vwap_valid` means the rolling quantity
sum is nonzero. `feature_vwap_quotient_valid` means the shared divider has
completed the exact unsigned quotient for that record; with the current
nonzero-denominator policy the two flags agree at the output boundary.

The default scales are `PRICE_SCALE=1,000,000` input micro-dollars,
`BPS_SCALE=10,000`, `BPS_OUTPUT_SCALE=100`, and
`IMBALANCE_FRAC_BITS=15`. Thus `100` in a bps output means `1.00 bp`, and
the normalized imbalance is signed Q1.15. The scales are RTL parameters and
matching Python `MarketModel` constructor parameters; changing them changes
the integer contract deliberately.

Examples with the default scales:

- bid quantity 30 and ask quantity 20 gives `10 * 32768 // 50 = 6553`;
- spread 2 at midpoint 101 gives `2 * 1,000,000 // 101 = 19,801`, or
  `198.01 bp x 100`;
- trades `(100, 2)` and `(110, 1)` give `sumPQ=310`, `sumQ=3`, and
  `vwap=103`; midpoint 101 then gives raw delta `-2` and
  `-2 * 1,000,000 // 103 = -19,417` bps x 100;
- momentum `+2` against reference 100 gives `20,000`, while `-2` gives
  `-20,000`;
- a one-sided quantity imbalance saturates at `32767` or `-32768`.

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
the rolling VWAP accumulators. A single bit-serial unsigned divider is shared
by VWAP, normalized imbalance, spread bps, momentum bps, and
midpoint-minus-VWAP bps. Its default numerator is 101 bits and denominator is
64 bits; a nonzero divide takes 101 iteration cycles and has no floating-point
or per-symbol arithmetic replication. The normalizer stages bps magnitude
scaling before the divider and saturates signed outputs at their format limits.

Wide arithmetic is sequenced across `READ_HISTORY`, `CAPTURE_HISTORY`,
`MULTIPLY_TRADE`, `APPLY_EVENT`, `FEATURE_CALC`, and the normalizer's
`N_START/N_SCALE/N_DIVIDE/N_WAIT` schedule. The selected symbol's packed state
word and old circular-window terms are first copied into working registers.
This adds event latency, but prevents a live symbol-indexed mux tree from
sharing a cycle with the 64/96/101-bit feature arithmetic. The registered
output and valid/ready contract preserve exact values and validity semantics
while allowing feature backpressure. Invalid or zero-denominator operations
are skipped and their value is held at zero with valid deasserted.
