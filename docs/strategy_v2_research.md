# Strategy V2 offline research

This document records the software-only Strategy V2 milestone. It does not
change the V1 RTL, packet protocol, fixed-point equations, reference strategy
semantics, or live-trading behavior.

## Dataset and scope

The source was reused and verified before analysis:

- canonical size: `1,783,708,512` bytes;
- canonical events: `55,740,891`;
- canonical SHA-256: `c0947b984d0a1fe5f96d83088205629b123a8401eb28571ab393c015615fcc44`;
- symbols: SPY, QQQ, NVDA, and AMD;
- no market-data download, Alpaca API call, normalization, or source-quality rescan was performed.

FAST contains 2026-09-01 and 2026-09-08 with 09:35-09:45,
12:00-12:10, and 15:45-15:55 measured windows. MEDIUM contains
2026-09-01, 2026-09-03, and 2026-09-08 with 09:35-09:50,
10:45-11:00, 12:45-13:00, and 15:35-15:50 measured windows.
Every measured window is preceded by a five-minute warm-up. Windows are
contiguous in the cache; warm-up events update state but are excluded from
research counts, candidates, and trades.

## Fixed-width research caches

`tools/backtest/research_cache.py` creates and validates gitignored caches
under `data/research/strategy_v2/<canonical-sha-prefix>/`. Each record is
108 bytes, big-endian, with this field order:

```text
timestamp_ns,event_type,symbol_id,measured,segment_index,session_index,side,
price,quantity,sequence,bid_price,ask_price,bid_quantity,ask_quantity,
midpoint,spread,vwap,rolling_volume,momentum_bps_x100,vwap_delta_bps_x100,
imbalance_q15,spread_bps_x100,validity
```

The manifest binds each cache to the canonical hash and event count, records
the record count, measured/warm-up counts, segment counts, file size, cache
hash, windows, and generation semantics. Valid manifests are reused. Atomic
`.part` files prevent an interrupted extraction from being mistaken for a
completed cache.

The current caches contain 3,602,998 FAST records (2,525,997 measured) and
9,053,675 MEDIUM records (6,817,734 measured). Their hashes and complete
manifests are in the generated result directory.

## Causal features and metrics

`tools/backtest/strategy_v2.py` calculates time-based momentum at 100 ms,
250 ms, 500 ms, 1 s, 2 s, 5 s, and 15 s, and time VWAP at 500 ms, 1 s,
2 s, 5 s, 15 s, 30 s, and 60 s. The feature engine only looks backward in
wall-clock time. Current observations are added after the reference lookup,
so they cannot satisfy their own positive horizon.

The research path preserves exact counts, minima/maxima where applicable,
means, queue/occupancy quantities, and overflow counts. Large quantiles use
deterministic bounded samples and are labeled `quantiles_approximate` in the
JSON artifacts. FAST screening uses causal executable forward returns as a
bounded research proxy; it does not claim portfolio P&L.

The required outputs are written to an ignored
`backtest_results/strategy_v2_fast_<run_id>/` directory:

`dataset_fast_manifest.json`, `dataset_medium_manifest.json`,
`pipeline_benchmark.json`, the six V1 diagnostic files,
`feature_predictiveness.json`, `time_momentum.json`, `time_vwap.json`,
`cooldown_research.json`, `persistence_research.json`, `score_research.json`,
`fast_variants.csv`, `fast_ranking.json`, `medium_selected.json`,
`medium_results.csv`, `strategy_v2_recommendation.json`, and `report.md`.

The analysis state manifest records completed phases and is updated after
each artifact group. On resume, completed V1, feature, and FAST artifacts are
loaded instead of replaying the cache.

## V1 sanity and diagnosis

The correctness artifact passed the requested 10 losing and 5 winning trade
checks. It confirms integer micro-dollar handling, 1 bp = 0.01%, 25 bp =
0.25%, 50 bp = 0.50%, fixed $10,000 notional, long entry at the ask, and long
exit at the bid. No P&L or unit bug was found.

The FAST V1 replay produced 298 trades and net P&L of -$6,264.53 under the
existing exact software/reference path. There were 272 stop exits, 14 EOD
exits, 12 max-hold exits, and no target or opposite-candidate exits. The
adjacent artifacts contain per-symbol window spans, candidate spacing and
clusters, executable MFE/MAE, exit diagnosis, and forward returns.

V1 forward returns were negative at the principal horizons: -3.8425 bp at
100 ms, -4.0117 bp at 1 s, -4.0098 bp at 5 s, and -4.1137 bp at 15 s.
Mean reversion was screened only because the direct continuation means at 1 s
and 5 s were both negative.

## V2 families and selection

The screen tested 23 base configurations across:

- V2-A: time momentum;
- V2-B: time momentum plus time VWAP and existing Q1.15 imbalance;
- V2-C: spread/liquidity filters and wall-clock cooldown;
- V2-D: continuous persistence;
- V2-E: integer quality score;
- V2-MR: three mean-reversion probes, enabled only by the direct negative continuation evidence.

The resulting FAST file contains 26 configurations. Selection is deterministic
and not P&L-only: eligibility, symbol coverage, forward-return level,
favorable fraction, drawdown penalty where available, and candidate-count tie
break are applied before promoting at most 10 rows to MEDIUM.

The strongest promoted MEDIUM row was `V2-D-persist-250ms`. Its bounded
executable forward-statistics proxy was -1.3610 bp at 1 s and -1.2295 bp at
5 s, with 634 candidates. The complete ranked rows are in
`medium_results.csv` and `strategy_v2_recommendation.json`.

## Runtime gate and observed bottleneck

The first extraction attempt used per-event dataclass construction and was
observed near 10.7k canonical events/s, projecting roughly 87 minutes. It was
stopped. The reader was then changed to large buffered reads plus
`struct.iter_unpack`, constructing event objects only for selected windows.
Observed canonical extraction rates were approximately 112.9k events/s for
FAST and 78.3k events/s for MEDIUM; the generated cache elapsed times were
493.6 s and 712.2 s respectively.

The 100,000-record FAST multi-configuration benchmark was 12.58k records/s,
projecting 4.77 minutes for the FAST screen. The exact MEDIUM portfolio path
was launched only after a subset benchmark; its live throughput degraded and
projected above the requested 30-minute gate, so it was stopped at 300 s and
15.7% progress. The resumed bounded MEDIUM forward-statistics screen ran at
10.45k records/s and completed in 866.3 s (14m26s), with progress fields:
`processed_events`, `total_events`, `percent_complete`, `elapsed_time`,
`processing_rate_events_per_second`, and `estimated_remaining_time`.

The prior real-study workflow scheduled 58 full 55.7M-event source passes:
1 baseline replay, 1 combined diagnostic pass, 4 baseline-comparison runs,
6 factor-ablation runs, 16 parameter-sanity runs, 2 symbol-split runs, and
28 latency/slippage runs. This resumed V2 run performed 0 new full source
passes because both validated caches were already present. A cold V2 cache
build requires two isolated source passes to materialize FAST and MEDIUM;
all subsequent V2 research runs on the fixed-width subsets rather than
replaying the 55.7M-event canonical stream.

No full 55.7-million-event V2 validation was run. The MEDIUM result is a
software research proxy and is not a production portfolio validation. A
future exact portfolio pass should be run only after a separately profiled
optimization makes its runtime and memory behavior acceptable.

## Final classification

**STRATEGY FAMILY STILL WEAK**

The V1 correctness checks passed, but the tested time-based momentum, VWAP,
imbalance, spread/liquidity, cooldown, persistence, quality-score, and
conditional mean-reversion families did not produce positive robust evidence
in the requested FAST-to-MEDIUM research scope. No RTL or live integration is
recommended from this milestone.
