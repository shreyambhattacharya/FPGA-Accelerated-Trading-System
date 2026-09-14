# Microstructure information-source discovery

This milestone is an independent information-source study. It does not create
Strategy V4, change V1/V2/V3 or edge-diagnostic semantics, simulate P&L or a
portfolio, add RTL, or touch broker/live code.

## Data boundary and runtime

The study reuses only the validated fixed-width V2 research caches:

- FAST: 3,602,998 records.
- MEDIUM: 9,053,675 records, benchmarked but not promoted for this run.
- The canonical 55,740,891-event binary was not opened; canonical full passes,
  downloads, and Alpaca calls were all zero.

The final FAST run is recorded in the ignored
`backtest_results/microstructure_discovery_<run_id>/` directory. Its full
shared pass processed FAST at approximately 12,046 records/s in 299.1 seconds.
The 100,000-record benchmark projected approximately 4.0 minutes for FAST and
10.5 minutes for MEDIUM, below the 30-minute automatic stage limit. Progress
reports contain processed events, total events, completion percentage, elapsed
time, processing rate, and estimated remaining time.

## Complete snapshot semantics

The cache has separate BID and ASK records. `SnapshotCoalescer` combines
same-timestamp, same-symbol, same-segment/session sides before evaluating a
microstructure feature. A trade appearing between the two sides is emitted
against the previous complete quote; it never sees the later ASK. Incomplete
groups are not treated as independent one-sided snapshots.

FAST produced 1,775,958 paired quote groups and 1,797,718 complete snapshots,
including 21,760 deterministic duplicate-side updates. All 1,797,718 snapshots
had bid <= ask, non-negative sizes, correct midpoint, and correct spread in the
validation counters. Segment and session state resets are explicit.

## Feature definitions

Microprice uses integer arithmetic:

```text
microprice = (ask_price * bid_size + bid_price * ask_size) / (bid_size + ask_size)
```

The result truncates toward zero. The reported displacement is
`(microprice - midpoint) * 100000000 / midpoint`, in bps x100. A zero size
denominator is invalid and produces no microprice feature.

Static imbalance is the existing cached model quantity:
`(bid_size - ask_size) * 32768 / (bid_size + ask_size)`, with the same integer
truncation. It is reported as a comparison, not as an independent discovery.

For consecutive complete snapshots, top-of-book OFI is:

```text
bid_delta = current_bid_size                 if current_bid_price > previous_bid_price
          = -previous_bid_size                if current_bid_price < previous_bid_price
          = current_bid_size - previous_bid_size otherwise

ask_delta = -previous_ask_size               if current_ask_price > previous_ask_price
          = current_ask_size                  if current_ask_price < previous_ask_price
          = current_ask_size - previous_ask_size otherwise

OFI = bid_delta - ask_delta
```

Instantaneous OFI and rolling 10ms, 100ms, 500ms, 1s, and 5s sums are computed
in the same chronological pass using bounded rolling accumulators. Liquidity
withdrawal, replenishment, spread changes, quote intensity, signed flow, and
quote/trade activity use the same complete-snapshot state.

Trades are classified using the latest complete quote at or before the trade:

```text
price >= ask       BUY
price <= bid       SELL
price > midpoint   LIKELY_BUY
price < midpoint   LIKELY_SELL
otherwise          UNKNOWN
```

UNKNOWN is retained as its own category. The FAST counts were BUY 4,094,
SELL 4,197, LIKELY_BUY 7,299, LIKELY_SELL 8,269, and UNKNOWN 5,463.

## Sampling and dependence

Event and snapshot counts are exact. Forward midpoint-return diagnostics use a
deterministic every-64th measured complete snapshot sample, with six future
horizons: 10ms, 100ms, 500ms, 1s, 5s, and 15s. Bucket edges are bounded-sample
signed empirical edges at 0/10/40/60/90/100 percentiles, yielding coarse
most-negative, negative, near-zero, positive, and most-positive buckets.
Means, minima/maxima, favorable fractions, bucket counts, and monotonicity are
reported for resolved sampled observations; medians and quantiles are explicitly
labelled approximate. Results are broken out by symbol, day, and research
window. Shock artifacts include raw sample counts and 100ms/500ms/1s/5s
quiet-period de-clustered first-event counts using bounded-sample empirical P90
thresholds. No naive formal significance claim is made.

## FAST decision

No family was promoted automatically. The strongest apparent positive families
were replenishment asymmetry and ask replenishment, but the broad discovery
set did not provide a sufficiently stable, directionally interpretable source
with positive top-minus-bottom separation across the required dimensions. The
primary microprice and OFI results were not monotonic and had negative FAST
top-minus-bottom separation at the review horizon. Therefore MEDIUM validation
was intentionally left as `not_run`; no thresholds were retuned.

Cross-symbol lead-lag was evaluated causally for QQQ->NVDA, QQQ->AMD, and
SPY->QQQ using only the latest source signal at or before the target timestamp.
It is descriptive and does not introduce V3-style long-horizon relative
strength.

AMD is reported separately in the JSON breakdown because its Level1 spread is
structurally wide. The source is IEX Level1, not NBBO/SIP; it is adequate for
descriptive single-venue microstructure discovery, but inadequate for claims
about consolidated-market queue position or executable cross-venue alpha.

Final classification for this milestone: **MICROSTRUCTURE INFORMATION WEAK**.

If future work is authorized, the one richer-data direction is a consolidated
NBBO/SIP plus multi-level depth/trade feed with venue timestamps. This is a
data-quality direction only, not a Strategy V4 recommendation; the signals
were not combined into a trading system here.
