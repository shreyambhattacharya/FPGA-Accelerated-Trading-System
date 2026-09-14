# Edge attribution and market-data diagnostic study

This milestone is complete for the existing V3-A broad configuration. It is a
diagnostic study, not a strategy search. It did not create Strategy V4, alter
V1/V2/V3 semantics, scan the canonical event binary, access Alpaca, download
data, change RTL, or add broker/live-trading behavior.

## Inputs and preservation

The study reused only the validated fixed-width caches:

| Cache | Records | Measured records |
| --- | ---: | ---: |
| FAST | 3,602,998 | 2,507,110 measured quote records |
| MEDIUM | 9,053,675 | 6,775,018 measured quote records |

The canonical identity was preserved as 55,740,891 events,
1,783,708,512 bytes, SHA-256
`c0947b984d0a1fe5f96d83088205629b123a8401eb28571ab393c015615fcc44`.
The canonical binary was not opened. The completed result directory is
`backtest_results/edge_diagnostic_20260914T004132Z/` and is intentionally
ignored by Git.

The prior research workflow scheduled 58 full canonical passes. The optimized
diagnostic schedules zero full canonical passes and one shared sequential pass
per cache. The first complete FAST+MEDIUM scan pair took approximately 366.7 s
plus 1,011.3 s, or 22.97 minutes. The 100,000-record pre-run benchmark
reported 23,401 FAST records/s, 23,109 MEDIUM records/s, and a projected
9.10-minute pair; the observed full scans were slower at approximately 9,827
FAST records/s and 8,953 MEDIUM records/s. A later FAST-only artifact refresh
took 576.7 s and did not rescan MEDIUM or canonical data.

The previous runtime was caused by repeated independent O(N) research scans
over the canonical stream. This milestone combines candidate generation,
forward resolution, controls, random reservoirs, quote quality, quote ages,
same-timestamp semantics, spread summaries, and clustering into one stream
pass per cache. Counts, means, extrema, and favorable fractions are exact;
quantiles use deterministic bounded samples and are labeled approximate.

Progress reports contain `processed_events`, `total_events`,
`percent_complete`, `elapsed_time`, `processing_rate_events_per_second`, and
`estimated_remaining_time`. The analysis state manifest records completed
phases and supports reuse of completed cache summaries.

## Return methodology

The fixture artifact passed 14/14 hand-checks, including unchanged, rising,
falling, wide-spread, narrow-spread, locked, and asynchronous quote cases.
Long executable return is `(future_bid - entry_ask) / entry_ask`; short is
`(entry_bid - future_ask) / entry_bid`. Midpoint returns use directional
midpoint-to-midpoint arithmetic and bps are return multiplied by 10,000.

The candidate set is the existing V3-A 30-second range, zero-buffer path: 6,350
raw observations, 2,339 long and 4,011 short. Matched controls are non-
candidates selected from symbol/day/window/spread/activity/volatility strata
without future fields. Random controls use fixed seed `20260913` and bounded
reservoirs of 32 observations per symbol/day/window/direction stratum.

## FAST candidate, matched-control, and random results

All values below are bps. Means are exact for resolved observations; medians
are deterministic bounded-sample quantiles where the artifact marks them
approximate.

| Horizon | Candidate midpoint mean / median | Candidate executable mean / median | Control midpoint mean / median | Candidate-minus-control midpoint | Candidate-minus-control executable |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1s | -5.028 / -0.985 | -16.533 / -3.477 | -0.086 / 0.000 | -4.942 | -6.318 |
| 5s | -5.820 / -1.115 | -17.445 / -3.470 | -0.018 / 0.000 | -5.802 | -6.663 |
| 15s | -5.281 / -1.183 | -16.688 / -3.610 | 0.068 / 0.000 | -5.349 | -6.050 |
| 30s | -5.152 / -1.173 | -16.714 / -3.257 | 0.132 / -0.070 | -5.284 | -5.709 |
| 60s | -5.692 / -1.512 | -16.973 / -3.418 | -0.121 / 0.139 | -5.571 | -5.842 |
| 120s | -5.378 / -1.131 | -16.631 / -3.795 | -0.022 / 0.195 | -5.356 | -5.469 |
| 300s | -7.557 / -2.299 | -20.142 / -5.090 | -0.218 / 0.196 | -7.339 | -7.282 |

The unconditional random valid-quote aggregate midpoint means/medians were:
`1s 0.231/0.000`, `5s 0.238/0.000`, `15s 0.499/0.065`, `30s
0.384/-0.065`, `60s 0.301/-0.217`, `120s 1.150/-0.065`, and `300s
1.406/-0.434` bps.

The random executable baseline, including exact favorable fractions, was:

| Horizon | Random long mean / median; favorable fraction | Random short mean / median; favorable fraction |
| --- | ---: | ---: |
| 1s | -14.197 / -1.331; 5.86% | -16.293 / -1.133; 4.57% |
| 5s | -13.638 / -1.556; 13.99% | -16.920 / -1.328; 17.50% |
| 15s | -14.707 / -2.224; 17.95% | -16.615 / -1.380; 26.90% |
| 30s | -15.072 / -1.832; 21.05% | -16.661 / -1.173; 30.64% |
| 60s | -16.267 / -3.619; 21.54% | -16.181 / -0.785; 38.64% |
| 120s | -18.190 / -4.092; 19.64% | -13.748 / 0.912; 50.42% |
| 300s | -22.693 / -7.958; 17.60% | -8.554 / 2.211; 57.44% |

## Cost, zero movement, and adverse selection

The mean midpoint-minus-executable gap ranged from 11.25 to 12.59 bps over
the strategic horizons. Mean mechanical zero-movement cost shares of negative
candidate executable returns were 69.57% at 1s, 66.63% at 5s, 68.35% at 15s,
69.17% at 30s, 66.47% at 60s, 67.68% at 120s, and 62.51% at 300s. This is a
mechanical attribution ratio, not a causal estimator. It does not dominate the
classification because the candidate midpoint returns and midpoint excess
returns are already negative.

Immediate candidate midpoint results (mean / median / favorable fraction) were:

| Horizon | Result |
| --- | ---: |
| 1ms | -3.354 / -0.065 / 15.04% |
| 10ms | -3.950 / -0.197 / 18.64% |
| 100ms | -4.411 / -0.562 / 19.40% |
| 250ms | -4.724 / -0.697 / 20.09% |
| 500ms | -4.878 / -0.782 / 19.44% |
| 1s | -5.028 / -0.985 / 20.26% |
| 5s | -5.820 / -1.115 / 25.40% |

The immediate movement is adverse on average, but the adverse-selection rule
does not dominate the classification because the intermediate-state fraction
is 45.43% and the candidate/control midpoint comparison already fails.

## Quote age and state quality

Candidate-only ages were tracked separately from all valid measured quote ages.
Candidate bid age was 0 ns for all 6,350 candidates because the candidate
record is the current bid update. Candidate ask age had mean 33.85 ms, median
0, P95 36.07 ms, P99 1.9817 s; buckets were `<1ms` 5,296, `1-10ms` 563,
`10-100ms` 294, `100ms-1s` 135, and `>1s` 62. Candidate benchmark age had
mean 14.27 ms, median 3.145 ms, P95 112.62 ms, and P99 248.70 ms. Candidate
last-trade age had mean 1.816 s, median 897.5 ms, P95 4.805 s, and P99
10.612 s. Returns by max bid/ask age bucket are in `quote_age.json`.

Among 2,507,110 measured FAST quote records there was 1 crossed quote, 13
locked quotes, no invalid non-positive quote, and 123,374 quotes wider than
10 bps. No V3 candidates were generated in crossed, locked, or invalid states.

## Same-timestamp semantics

FAST contained 1,776,376 equal-timestamp groups and 1,775,958 paired quote
groups, a 99.9765% pair frequency. Every observed pair was BID then ASK; no
ASK-then-BID pairs occurred. There were 1,775,958 multiple-quote groups, zero
trade/quote-mixed groups, one multiple-symbol group, and 305,905 sequence
regressions reported by the cache ordering audit.

The strategy evaluated after every fixed-width cache record, so 2,885 of 6,350
candidate observations (45.43%) were emitted on an intermediate paired-quote
state. Evaluating only after both sides produced 3,352 snapshot candidates.
At 1s, intermediate candidates averaged -3.454 bps midpoint return versus
-6.454 bps for after-both-sides snapshots; the snapshot result remained
negative, so semantics changed incidence but did not materially change the
primary failure conclusion.

## Spread distribution

FAST spread distributions in bps were:

| Symbol | Mean | Median | P75 | P90 | P95 | P99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| SPY | 0.84 | 0.39 | 0.65 | 1.56 | 4.06 | 7.08 |
| QQQ | 1.57 | 0.84 | 2.23 | 5.14 | 5.95 | 8.35 |
| NVDA | 6.65 | 1.85 | 3.02 | 19.42 | 70.00 | 100.54 |
| AMD | 54.76 | 37.01 | 96.74 | 137.72 | 162.88 | 186.85 |

Across symbols, opening/midday/late-session means were 5.80/1.26/2.35 bps;
corresponding medians were 1.85/0.84/0.70 bps. The wide AMD/NVDA tails are
visible in the symbol-specific artifacts, but the midpoint edge is negative
before crossing costs.

## FAST versus MEDIUM

FAST appears representative for the diagnostic conclusion. Mean spreads were
FAST versus MEDIUM: SPY 0.838/0.939, QQQ 1.573/1.511, NVDA 6.652/6.937,
and AMD 54.757/56.987 bps. Mean activity was 223.25 versus 244.13 q8 units;
mean volatility was 2,298.78 versus 2,125.66 bps-x100. Benchmark quote-age
means were 22.96 ms versus 26.44 ms, with medians 5.985 ms versus 5.953 ms.

FAST versus MEDIUM random aggregate midpoint means by horizon were:

| Horizon | FAST | MEDIUM |
| --- | ---: | ---: |
| 1s | 0.231 | 0.105 |
| 5s | 0.238 | 0.271 |
| 15s | 0.499 | 0.479 |
| 30s | 0.384 | 0.458 |
| 60s | 0.301 | 0.531 |
| 120s | 1.150 | 0.394 |
| 300s | 1.406 | 0.793 |

These are descriptive unconditional comparisons only; MEDIUM was not used to
tune or promote a strategy. The differences do not support
`FAST SAMPLE NOT REPRESENTATIVE`.

## Clustering and spot checks

The 6,350 raw candidates reduced to 1,511 1-second clusters, 741 5-second
clusters, 471 10-second clusters, and 119 30-second clusters. First-signal
cluster midpoint means at 1s were -5.992, -6.099, -6.167, and -6.760 bps;
at 30s they were -6.247, -6.532, -7.218, and -7.684 bps respectively for
those quiet periods. Strongest-score-per-cluster results were similarly
negative; clustering does not create a positive edge.

`v3_candidate_spot_checks.csv` contains 50 deterministic rows, 25 long and 25
short. Across those rows, mean midpoint/executable returns were -5.115/-19.533
bps at 1s, -4.314/-21.530 at 5s, 0.957/-21.260 at 15s, 2.986/-21.624 at 30s,
and -1.785/-22.891 at 60s. Each row includes timestamp, symbol, direction,
bid/ask/midpoint, entry spread, future bid/ask/midpoint, and return arithmetic.
The approximately -16 to -20 bps aggregate result is therefore arithmetically
explained by negative midpoint selection plus a roughly 11–13 bps crossing
gap; it is not a hidden bps conversion error.

## Classification and limits

Primary classification: **SIGNAL FAILURE**.

The evidence is quantitative and consistent: candidate midpoint means were
negative at all seven strategic horizons, candidate midpoint excess versus
matched controls was negative from -4.942 to -7.339 bps, and no horizon had
positive candidate midpoint or positive midpoint excess. Execution costs are
substantial but not primary because they cannot explain the negative midpoint
signal. Immediate adverse movement is present, but the control comparison and
snapshot-only diagnostic do not show it as the sole dominant mechanism. FAST
and MEDIUM are sufficiently similar for this relative conclusion. The local
IEX data is adequate for pipeline testing and relative comparison, but its
single-venue, non-NBBO scope limits final execution-realism claims; that
limitation was not material to this classification.

Exactly one conceptual next direction is recommended: abandon this
directional-indicator search on these features and define and validate a
fundamentally different information source before any new strategy family.
That direction was not implemented.

All required JSON/CSV artifacts are in the ignored result directory, including
`analysis_state.json` and `pipeline_benchmark.json`. The tracked implementation
is [tools/backtest/edge_diagnostic.py](../tools/backtest/edge_diagnostic.py),
with deterministic tests in
[tools/backtest/tests/test_edge_diagnostic.py](../tools/backtest/tests/test_edge_diagnostic.py).
