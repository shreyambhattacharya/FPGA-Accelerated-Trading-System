# Verification plan

## Automated checks

Run the complete local suite with:

```powershell
.\fpga\scripts\run_iverilog.ps1
```

The script runs the existing SPI/CDC/CRC/loopback regressions plus:

- `tb_strategy_config.sv`: fifteen valid runtime writes covering global
  enables, all threshold registers, cooldown, symbol enable, and slot reset,
  plus two invalid commands;
- `tb_signal_engine.sv`: directed long/short threshold equality, invalid
  features, edge triggering, cooldown-expiry repeats, reversal, enable/disable
  re-arming, symbol isolation, slot reset, and signal backpressure;

- `tb_market_state_engine.sv`: directed first-bid/first-ask, valid and
  crossed quotes, spread/midpoint, signed imbalance, side isolation, trade
  isolation, zero quantity, ring fill/replacement, momentum warm-up/wrap,
  VWAP accumulator replacement, duplicate/stale/invalid sequence, invalid
  symbol/side, reset, and feature backpressure;
- `tb_unsigned_divider.sv`: 10 directed divider/reset/busy cases plus 5,000
  deterministic 101-bit-by-64-bit unsigned quotient cases;
- `tb_market_latency.sv`: warm-up and fully warm feature-service latency with
  all five normalized divide operations active, plus the incremental
  feature-to-signal stage latency;
- `tb_market_parameter.sv`: compile/elaborate/run smoke points for
  `NUM_SYMBOLS=1,4,8,16,32`;
- `tb_market_n32_stress.sv`: deterministic N32 round-robin, hot-symbol,
  pseudo-random, rolling-wrap, sequence-wrap, reset/interleaving, and
  feature-backpressure coverage;
- `tools/reference_model/differential_test.py`: deterministic packet replay
  through the dispatcher and market engine against the independent Python
  model.

The default differential stream contains exactly 1,000 events and, in the
latest run, produced 915 feature records, 84 engine rejections, and 1 explicit
dispatcher symbol error with zero mismatches. It includes all four starter
symbols, quote/trade interleaving, duplicate/stale and bad-side cases, crossed
quotes, zero quantity, large prices, and maximum-width price/quantity fields.

The same fixed seed (`0x5EED`) at 10,000 events produced exactly 9,032
accepted feature records/signals, 967 engine rejections, 1 dispatcher error,
and 0 mismatches. A 100,000-event run produced exactly 90,769 accepted
feature records/signals, 9,230 engine rejections, 1 dispatcher error, and 0
mismatches. The counts are reported by the test, not substituted into the
oracle to make a run pass.

The normalized fields and appended signal tuple are compared for every
accepted feature, including floor VWAP quotient, Q1.15 imbalance, signed bps
x100 ratios, raw midpoint-minus-VWAP, all validity flags, action, score, and
reason bits. The 100,000-event run was executed separately as a deterministic
long stress replay; the checked-in default regression remains 1,000 and
10,000 events to keep the routine local run practical.

The N32 stressbench completed with:

```text
tb_market_n32_stress: PASS symbols=32 rounds=6 random=160
```

It exercises rapid symbol changes, all-32 round robin, repeated symbol 7
traffic, deterministic random interleaving, circular-window updates, stale
sequence after the unsupported wrap boundary, reset while state is populated,
and a five-cycle feature stall. The feature record remained stable during the
stall and no symbol state leaked into another bank.

The C++ utility continues to exercise packet serialization/parsing and the
deterministic software transport. Its output labels simulation versus real
hardware and does not present software timing as FPGA timing.

The standalone signal bench reported:

```text
tb_signal_engine: PASS directed=20 interleaved_symbols=32
tb_strategy_config: PASS writes=15 slot_reset=1 invalid=2
```

The integrated latency bench reported 224 feature cycles for a warm quote,
536 for a fully warm trade, and 535 for a fully warm quote with all five
normalized divisions active. The signal stage added exactly one `clk27` cycle,
for event-to-signal latencies of 225, 537, and 536 cycles respectively.
Candidate records are internal at this stage; there is no signal packet
consumer on the physical SPI result path.

## Vendor implementation checks

`fpga/scripts/run_gowin_pnr.tcl` runs the default top through Gowin synthesis,
place-and-route, timing, and bitstream generation. The matrix driver
`fpga/scripts/run_gowin_matrix.ps1` repeats the flow for `NUM_SYMBOLS=4,8,16,32`
using physical-IO-preserving wrappers. Reports stay under the ignored
`fpga/gowin/validation` directory.

The candidate-signal matrix has zero setup/hold TNS at both `clk27` and
`spi_clk` for all four points. The post-P&R `clk27` Fmax values are 58.876,
67.423, 59.049, and 57.411 MHz for N4/N8/N16/N32; `spi_clk` Fmax is 107.252,
87.609, 93.948, and 105.815 MHz. Candidate-signal logic levels are 20, 20,
20, and 12 on `clk27`. The N32 point has 30.411 MHz of headroom over the
27 MHz system target, though it is below the preferred 60 MHz analysis goal.
The run still reports PR1014 for generic routing of `clk_d` and `spi_clk_d`;
this is documented in the bring-up guide and is not suppressed.

The host loopback regression also passed in its software transport mode:

```text
packets_sent=1000 packets_returned=1000 failures=0 missing=0 corrupted=0
other_failures=0 sequence_errors=0
```

That is a deterministic PC simulation result, not Pi↔FPGA hardware evidence.

Physical Pi↔Tang validation has not been run because the hardware is not
available in this development environment. Any hardware result must record
the board, bitstream commit, tool version, SPI mode/rate, packet count,
failures, wiring, and measurement setup.

## Historical replay checks

The historical replay path is verified separately with a deterministic
hand-check fixture and Python unit tests covering canonical CSV/binary
round-trips, provider parsing, quality diagnostics, session filters, latency
and executable-side fills, costs/no-pyramiding, date splits, forward-return
horizon selection, and end-to-end portfolio accounting. It reuses the same
`MarketModel` and `StrategyModel`; it does not change FPGA behavior or claim
physical hardware validation. Run it with:

```powershell
python -m unittest discover -s tools/backtest/tests -p "test_*.py" -v
```
