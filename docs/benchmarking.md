# Benchmarking

The benchmark record for each request/response contains the packet sequence, send timestamp, receive timestamp, round-trip duration, and success/failure classification. The host reports count, missing/corrupt/sequence errors, average, minimum, maximum, P50, P95, P99, total elapsed time, and effective packets/second.

The current exchange includes three 32-byte transfers, so its round trip includes request, turnaround, and response framing. That is an intentional first-milestone measurement definition. Later reports may separately measure wire time, FPGA service time, and end-to-end host overhead.

No benchmark number is claimed until the test has actually run. Software-loopback results are labeled `mode=SIMULATION`; the host's `mode=REAL_HARDWARE` is reserved for the Linux spidev path. FPGA RTL results are simulation results, not hardware latency. Only a physical Pi ↔ Tang Nano run can support a hardware result, and the test must record SPI clock, toolchain/bitstream, kernel, wiring conditions, packet count, failures, and whether the result includes host scheduling overhead.
