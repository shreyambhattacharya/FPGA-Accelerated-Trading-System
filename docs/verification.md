# Verification plan

## Automated checks

Run the complete local suite with:

```powershell
.\fpga\scripts\run_iverilog.ps1
```

The script runs the existing SPI/CDC/CRC/loopback regressions plus:

- `tb_market_state_engine.sv`: directed first-bid/first-ask, valid and
  crossed quotes, spread/midpoint, signed imbalance, side isolation, trade
  isolation, zero quantity, ring fill/replacement, momentum warm-up/wrap,
  VWAP accumulator replacement, duplicate/stale/invalid sequence, invalid
  symbol/side, reset, and feature backpressure;
- `tb_unsigned_divider.sv`: 10 directed divider/reset/busy cases plus 5,000
  deterministic 101-bit-by-64-bit unsigned quotient cases;
- `tb_market_latency.sv`: warm-up and fully warm feature-service latency with
  all five normalized divide operations active;
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
accepted feature records, 967 engine rejections, 1 dispatcher error, and 0
mismatches. The counts are reported by the test, not substituted into the
oracle to make a run pass.

The normalized fields are compared as part of every feature tuple, including
floor VWAP quotient, Q1.15 imbalance, signed bps x100 ratios, raw
midpoint-minus-VWAP, and all validity flags. No 100,000-event replay was run
because the 10,000-event run already exercises the same exact comparison while
keeping the local Icarus regression practical.

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

## Vendor implementation checks

`fpga/scripts/run_gowin_pnr.tcl` runs the default top through Gowin synthesis,
place-and-route, timing, and bitstream generation. The matrix driver
`fpga/scripts/run_gowin_matrix.ps1` repeats the flow for `NUM_SYMBOLS=4,8,16,32`
using physical-IO-preserving wrappers. Reports stay under the ignored
`fpga/gowin/validation` directory.

The final matrix has zero setup/hold TNS at both `clk27` and `spi_clk` for all
four points. The post-P&R `clk27` Fmax values are 68.327, 72.627, 82.566, and
75.516 MHz for the archived raw-feature build; the normalized-feature build
measures 66.439, 62.427, 60.565, and 68.821 MHz for N4/N8/N16/N32. The
normalized P&R logic levels are 19, 19, 21, and 20. It still reports PR1014
for generic routing of `clk_d` and `spi_clk_d`; this is documented in the
bring-up guide and is not suppressed.

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
