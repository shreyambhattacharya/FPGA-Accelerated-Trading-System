# Benchmarking

The transport benchmark record for each request/response contains the packet sequence, send timestamp, receive timestamp, round-trip duration, and success/failure classification. The host reports count, missing/corrupt/sequence errors, average, minimum, maximum, P50, P95, P99, total elapsed time, and effective packets/second.

The current loopback exchange includes three 32-byte transfers, so its round trip includes request, turnaround, and response framing. That is an intentional transport measurement definition. Market events are consumed by the dispatcher and state engine and produce no loopback response packet.

For market events, the service path is measured separately in RTL by the
dispatcher/engine testbench: a packet is CRC-checked, normalized, accepted or
rejected by sequence/symbol/side policy, and a feature record is registered.
The feature record is held when `feature_ready` is low. A host round-trip
number must not be interpreted as market-feature latency.

The serialized engine accepts the next quote after 12 `clk27` cycles or the
next trade after 13 cycles when the feature consumer is always ready. These
are theoretical RTL bounds of 2,250,000 quotes/s and 2,076,923 trades/s at
27 MHz, excluding SPI wire time, FIFO/dispatcher occupancy, host scheduling,
and future consumers.

The recorded software-loopback result is labeled `mode=SIMULATION`; the
host's `mode=REAL_HARDWARE` is reserved for the Linux spidev path. FPGA RTL
and Gowin P&R results are implementation evidence, not physical latency or
market-data throughput. Only a physical Pi ↔ Tang Nano run can support a
hardware result, and the test must record SPI clock, toolchain/bitstream,
kernel, wiring conditions, packet count, failures, and whether the result
includes host scheduling overhead.

The final local loopback regression passed in software transport mode:

```text
mode=SIMULATION
transport=software-loopback
packets_sent=1000 packets_returned=1000 failures=0 missing=0 corrupted=0
other_failures=0 sequence_errors=0
elapsed_ns=647700 effective_packets_per_second=1.54392e+06
rtt_ns: min=400 p50=500 p95=500 p99=500 max=1700 average_us=0.4584
```

This is a reproducible PC result for packet serialization and the software
transport, not a measurement of a physical Pi↔Tang Nano link.

## Before/after Gowin matrix

The baseline is the measured matrix from commit `651fc38`. The new values are
from the final Gowin V1.9.11.03 Education run on `GW2AR-LV18QN88C8/I7`, with
the same constraints and physical pins. `Fmax` and logic levels are the P&R
report's `clk27` values; setup/hold slack is the worst path in the same P&R
report. Both setup and hold TNS are 0.000 ns at both clocks for every point.

| Symbols | Old `clk27` Fmax | New `clk27` Fmax | Old setup/hold slack | New setup/hold slack | Old LUT / FF | New LUT / FF | Old BSRAM* | New BSRAM | Old P&R RAM16 | New P&R RAM16 | Old DSP / New DSP |
| ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 93.264 MHz | 68.327 MHz | 26.315 / 0.313 ns | 22.402 / 0.313 ns | 1,609 / 2,101 | 1,609 / 2,308 | NR | 0 | 133 | 124 | 0 / 0 |
| 8 | 86.086 MHz | 72.627 MHz | 25.421 / 0.313 ns | 23.268 / 0.313 ns | 1,451 / 2,105 | 1,671 / 2,445 | NR | 0 | 133 | 124 | 0 / 0 |
| 16 | 76.170 MHz | 82.566 MHz | 23.909 / 0.313 ns | 24.926 / 0.313 ns | 1,505 / 2,113 | 1,830 / 2,718 | NR | 0 | 133 | 124 | 0 / 0 |
| 32 | 29.366 MHz | 75.516 MHz | 2.985 / 0.313 ns | 23.795 / 0.313 ns | 2,153 / 3,132 | 2,156 / 3,263 | 6 | 0 | 130 | 124 | 0 / 0 |

The P&R `spi_clk` Fmax values for the new run are 111.260, 102.224,
103.155, and 110.880 MHz for N4/N8/N16/N32. The old corresponding values
were 109.586, 98.838, 116.179, and 105.698 MHz. New P&R logic levels are
8, 8, 7, and 7 respectively. The old archived matrix did not retain a
per-point logic-level field; its old setup/hold and resource values above are
the preserved baseline records. `NR` means the old per-point synthesis
hierarchy did not retain a BSRAM field in the archived matrix. The P&R RAM16
column is the vendor's total `SSRAM(RAM16)` resource. The old N32 synthesis
hierarchy separately recorded 6 BSRAM blocks in the market engine; the final
N32 synthesis report records 0 market BSRAM and `BSRAM 0/46` overall.

The new critical paths are:

| Symbols | New P&R critical path |
| ---: | --- |
| 4 | `impl/rx_fifo_i/rd_bin_1_s0/Q -> impl/packet_dispatcher_i/packet_reg_0_s0/CE` |
| 8 | `impl/rx_fifo_i/rd_bin_1_s0/Q -> impl/packet_dispatcher_i/packet_reg_0_s0/CE` |
| 16 | `impl/loopback_engine_i/byte_index_4_s1/Q -> impl/loopback_engine_i/crc8_engine_i/crc_reg_3_s0/D` |
| 32 | `impl/loopback_engine_i/byte_index_3_s1/Q -> impl/loopback_engine_i/crc8_engine_i/result_7_s0/D` |

The N32 market-state path is no longer the limiting top-level path. The new
N32 point uses 2,156 LUTs, 3,263 FFs, 182 ALU resources, 124 RAM16 FIFO
resources, and 0 DSPs: 15% of device logic and 21% of FF capacity. The
synthesis report shows `BSRAM 0/46`; 12,487 FFs and 17,654 logic resources
remain, and all 46 BSRAM blocks remain unused. The histories are RAM
extraction candidates in the synthesis log, but their mapping is not counted
as BSRAM in the final target report.

## Interpretation

The N32 improvement is architectural, not a claim that every matrix point
must improve monotonically. N4 and N8 are now dominated by fixed transport
and dispatcher/CRC placement, while N16/N32 no longer expose the old
symbol-dependent state-write cone. The extra market-engine cycles are the
latency cost for a shared arithmetic path and registered state access. The
27 MHz target has 48.516 MHz of Fmax margin at N32, but future logic must be
budgeted against the 75.516 MHz implementation result rather than assuming
the entire margin is free.
