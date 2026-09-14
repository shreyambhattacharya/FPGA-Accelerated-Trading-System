# Strategy V3 research: relative-strength and volatility-normalized breakouts

## Final classification

**STRATEGY V3 WEAK**

The independent V3 software research families did not clear the positive,
multi-horizon, executable-forward-return gate on the available FAST scope.
No V3 configuration was promoted to MEDIUM, so no portfolio recommendation was
opened and no V4 hypothesis was started.

## Scope and integrity

This milestone started from parent commit `27169f6b42d16c0081c5c522595e9df4cc5a5648`.
It is software-only research. V1/V2 strategy semantics, FPGA RTL, broker
integration, live trading, downloads, normalization, and the canonical binary
were not modified or reopened.

The existing V2 cache manifests validated the canonical identity:

| Field | Value |
|---|---:|
| Canonical size | 1,783,708,512 bytes |
| Canonical events | 55,740,891 |
| Canonical SHA-256 | `c0947b984d0a1fe5f96d83088205629b123a8401eb28571ab393c015615fcc44` |
| FAST cache records | 3,602,998 |
| MEDIUM cache records | 9,053,675 |
| FAST measured records | 2,525,997 |
| MEDIUM measured records | 6,817,734 |

The run used `data/research/strategy_v2/c0947b984d0a1fe5/fast.bin` and
`medium.bin` only after checking their manifests, sizes, and SHA-256 hashes.
The source canonical binary was never scanned. The run result is in the
ignored directory `backtest_results/strategy_v3_20260914T000032Z/`.

## Runtime optimization and accounting

The previous workflow performed 58 full 55.7M-event canonical passes: baseline
replay, diagnostics, comparisons, ablations, parameter checks, splits, and
latency/slippage variants. V3 performed **0** full canonical passes. It used
one sequential FAST feature pass and one sequential FAST variant pass shared by
all 50 configurations. The V3 feature pass and variant pass were not repeated
per configuration.

Benchmark results recorded before the expensive stages:

| Stage | Sample | Rate / estimate | Observed full FAST |
|---|---:|---:|---:|
| Shared feature generation | 100,000 | 31,503 records/s; 1.91 min estimate | 175.04 s, 20,584 records/s |
| One configuration | 100,000 | 30,619 records/s | not used for full search |
| Shared 50-configuration loop | 100,000 | 8,107 records/s; 7.41 min estimate | 494.07 s, 7,293 records/s |

The combined FAST work completed in about 11.2 minutes including the feature
pass, benchmarks, artifact writes, and recovery from a report-finalization
exception. The exception was a label-map bug for the 35-second activity
window; the completed feature and FAST search artifacts were reused in place.
The state manifest records `cache_validation`, `fast_feature_generation`,
`fast_search`, `medium_validation`, and `report` as complete. No credentials
were loaded or printed.

## Causal V3 design

- Benchmark mapping: SPY -> QQQ, QQQ -> SPY, NVDA -> QQQ, AMD -> QQQ.
- Relative strength is integer `stock_return_bps_x100 - benchmark_return_bps_x100`.
- Stock returns use the current midpoint versus the latest same-symbol
  midpoint at or before `T-H`. Benchmark returns use only benchmark state
  already observed in the stream at or before the current event. There is no
  interpolation or future lookup.
- Breakout thresholds use the current midpoint against a strictly prior
  high/low over 15s, 30s, 60s, or 120s. Current-event inclusion is explicitly
  excluded from the threshold via causal monotonic max/min deques.
- Volatility is a prior high-low midpoint range width; normalization uses the
  integer comparator `abs(relative_strength) * 100 >= volatility * threshold`.
  No square root or floating-point volatility is required.
- Activity maintains exact causal trade quantity and count over 5s, 15s, 30s,
  and 60s windows. Relative activity compares recent 5s quantity multiplied by
  six with preceding 30s quantity.
- Regime is benchmark UP/DOWN/NEUTRAL over 15s, 60s, and 120s with a 50
  bps-x100 threshold.
- Persistence and cooldown are wall-clock and segment-aware. Long and short
  signals are evaluated separately; no blind short portfolio was run.

The 50 FAST configurations were staged across the requested families:

| Family | Configurations | Factors |
|---|---:|---|
| V3-A | 8 | breakout and range buffer |
| V3-B | 8 | breakout plus benchmark regime |
| V3-C | 8 | relative-strength breakout |
| V3-D | 8 | volatility-normalized relative strength |
| V3-E | 4 | activity-confirmed relative breakout |
| V3-F | 8 | persistence and cooldown |
| V3-G | 6 | small integer score |

## FAST feature observations

The FAST pass processed 3,602,998 records, including 2,507,110 measured quote
records and 2,507,096 valid executable quote records. Exact event/validity
counts were maintained. Distribution quantiles are deterministic bounded
samples: every 32nd observation, capped at 20,000 samples.

| Prior range | Long breakouts | Short breakouts |
|---:|---:|---:|
| 15s | 3,847 | 5,351 |
| 30s | 2,462 | 4,103 |
| 60s | 1,418 | 3,115 |
| 120s | 830 | 2,334 |

The sampled spread distribution had median 144 and P75 417 bps-x100. The
sampled activity-ratio distribution had median 105 and P75 210 in Q8 units.
Regime counts were:

| Horizon | DOWN | NEUTRAL | UP |
|---:|---:|---:|---:|
| 15s | 1,130,949 | 565,325 | 810,822 |
| 60s | 1,426,909 | 279,765 | 800,422 |
| 120s | 1,613,082 | 193,724 | 700,290 |

## FAST forward-return evidence

Forward returns are executable quote-side observations: a long enters at ask
and exits at a future bid; a short enters at bid and exits at a future ask.
Counts, means, and favorable fractions are exact for resolved observations.
P10/P25/median/P75/P90 are deterministic bounded samples: every eighth
observation, capped at 20,000 samples.

The best mean at each requested strategic horizon was still negative:

| Horizon | Configuration with best mean | Mean bps |
|---:|---|---:|
| 5s | V3-A, 30s range, zero buffer | -17.445 |
| 15s | V3-A, 30s range, zero buffer | -16.688 |
| 30s | V3-A, 30s range, zero buffer | -16.714 |
| 60s | V3-A, 120s range, zero buffer | -16.515 |

The corresponding best-by-horizon configurations were also negative at 1s
(-15.890 bps), 120s (-16.278 bps), and 300s (-19.962 bps). The ranking order
prioritized multi-horizon evidence, coverage, clustering, and candidate count;
it was not a P&L-only sort. Its first row, V3-E activity-q8-512, had only 45
candidates and remained negative at every strategic horizon.

For the broadest useful V3-A comparison, 30s range and zero buffer produced
6,350 candidates: 2,339 long and 4,011 short, covering all four symbols, both
days, and all six FAST windows.

| Direction | 5s mean / median / favorable | 15s mean | 30s mean | 60s mean |
|---|---:|---:|---:|---:|
| Long | -19.293 / -3.548 / 6.49% | -18.900 | -19.548 | -19.759 |
| Short | -16.353 / -3.617 / 7.18% | -15.372 | -14.987 | -15.248 |

Short 300s median was positive in this one broad comparison, but the strategic
5–60s means and medians were negative, so it did not satisfy the gate. The
complete per-symbol, per-day, per-window, direction, cluster, and horizon
results are retained in `fast_variants.csv` and `fast_ranking.json`.

Family-level best 5s means were:

| Family | Best configuration | Candidates | 5s mean bps |
|---|---|---:|---:|
| V3-A | 120s range, 1 bp buffer | 3,071 | -17.734 |
| V3-B | 60s confirming, 1 bp buffer | 185 | -62.121 |
| V3-C | 60s range, 15s relative | 460 | -61.578 |
| V3-D | 60s range, 30s relative, 60s vol | 450 | -62.468 |
| V3-E | activity Q8 128 | 242 | -64.298 |
| V3-F | 100ms persistence, 1s cooldown | 1 | -43.106 |
| V3-G | score 4, normalized 1 | 64 | -75.551 |

None of the 50 rows had the required combination of positive medians and
means at multiple strategic horizons, more than one symbol/day/window, and a
sensible candidate count. `fast_promoted.json` therefore contains an empty
selection.

## MEDIUM and portfolio gate

MEDIUM validation was **not run** because no FAST configuration passed the
positive multi-horizon evidence gate. `medium_results.csv`,
`medium_forward_returns.json`, and `medium_mfe_mae.json` explicitly record the
not-run status. MFE/MAE was not fabricated without a promoted candidate.

Portfolio simulation was also not run. Consequently, the standardized
$100,000 cash, $10,000 fixed-notional, no-pyramiding, one-position-per-symbol,
1ms-latency, 0.5bp-slippage, quote-aware, EOD-flatten assumptions were not
invoked. `medium_portfolio.json` explicitly says it was not run because the
forward-return gate failed.

## FPGA implementation implications

No RTL implementation or synthesis was performed. A future implementation,
if separately authorized, can keep the existing strategy semantics isolated
and add V3 as a software-defined experiment first. The FPGA-friendly state
shape is bounded per symbol:

- midpoint history/as-of return state;
- four range-deque pairs for prior high/low;
- bounded trade quantity/count windows;
- benchmark-index lookup for cross-symbol context;
- integer relative-strength, volatility, activity, persistence, cooldown, and
  score comparators.

For `NUM_SYMBOLS=32`, the architecture should use a 32-entry symbol state RAM
and a small benchmark mapping table, with no square-root unit. The dominant
logic is integer add/subtract, multiply-by-constant, comparisons, counters,
and bounded queue maintenance. This is a qualitative cost/architecture note,
not a timing, LUT, BRAM, DSP, or Fmax result.

## Tests, limitations, and reproducibility

The complete backtest suite passes **28/28 tests**, including the nine new V3
tests for integer units, causal asynchronous benchmarks, prior-only ranges,
activity windows, regime/buffer behavior, long/short signals,
persistence/cooldown, segment reset, executable forward returns, and cache
identity validation.

Important limitations are the two available trading days, six narrow FAST
windows, no promoted MEDIUM candidate, sampled quantiles, no full canonical
V3 validation, and no live/broker evaluation. Existing ignored `.env.local`,
`data`, `backtest_results`, `build`, and `.vscode` content was preserved; no
credential value was printed, persisted, or committed. The correct next step
is to retain this negative result as the V3 stopping point rather than invent
another strategy family.
