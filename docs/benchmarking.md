# Benchmarking

The transport benchmark record for each request/response contains the packet sequence, send timestamp, receive timestamp, round-trip duration, and success/failure classification. The host reports count, missing/corrupt/sequence errors, average, minimum, maximum, P50, P95, P99, total elapsed time, and effective packets/second.

The current loopback exchange includes three 32-byte transfers, so its round trip includes request, turnaround, and response framing. That is an intentional transport measurement definition. Market events are consumed by the dispatcher and state engine and produce no loopback response packet.

For market events, the service path is measured separately in RTL by the
dispatcher/engine testbench: a packet is CRC-checked, normalized, accepted or
rejected by sequence/symbol/side policy, and a feature record is registered.
The feature record is held when `feature_ready` is low. A host round-trip
number must not be interpreted as market-feature latency.

No benchmark number is claimed until the test has actually run. Software-loopback results are labeled `mode=SIMULATION`; the host's `mode=REAL_HARDWARE` is reserved for the Linux spidev path. FPGA RTL results are simulation results, not hardware latency. Only a physical Pi ↔ Tang Nano run can support a hardware result, and the test must record SPI clock, toolchain/bitstream, kernel, wiring conditions, packet count, failures, and whether the result includes host scheduling overhead.
