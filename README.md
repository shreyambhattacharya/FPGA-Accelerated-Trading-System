# FPGA-Accelerated Intraday Trading System

This repository is the foundation for a paper-trading research and engineering platform built around a Raspberry Pi 5 and a Tang Nano 20K FPGA. The Pi owns networking, external market-data protocol handling, configuration, logging, portfolio state, and eventual paper-broker integration. The FPGA owns deterministic streaming work that benefits from fixed-width hardware, beginning with the SPI packet path and loopback validation.

This is not institutional colocated HFT, a profitability claim, or a live-money trading system. The project is paper-trading-only by design. No live order path is implemented.

## Current milestone

The market-data milestone preserves the data-plane foundation and adds a
parameterized quote/trade state engine:

```text
Pi SPI master
  -> 32-byte packet transfer
Tang Nano SPI slave -> SPI-domain RX FIFO -> 27 MHz packet dispatcher
                                      |-> LOOPBACK -> status/TX FIFO
                                      `-> QUOTE/TRADE -> normalized event -> market state/features
  <- turnaround/response transfers remain available for diagnostic packets
```

The packet format is fixed at 32 bytes, uses big-endian integer fields, and ends with CRC-8/ATM. The RTL is written in synthesizable SystemVerilog. The packet FIFOs are Gray-pointer asynchronous FIFOs with two-flop pointer synchronizers; SPI framing remains in the external SPI clock domain while validation, market-state updates, and diagnostic response generation run on the Tang Nano's onboard 27 MHz clock. The market engine uses integer micro-dollar prices, per-symbol sequence checks, quote/trade isolation, spread, midpoint, momentum, rolling volume, imbalance, and VWAP accumulators. Its market path is a serialized, registered read/modify/write pipeline with one shared 64x32 multiplier and one shared sequential 101/64 divider; it does not replicate feature engines per symbol. It emits the actual VWAP quotient, Q1.15 imbalance, spread/momentum/midpoint-VWAP basis-point fields scaled by 100, and explicit validity flags. Per-symbol state is held in a packed state bank with logical reset tracking, while the rolling histories remain independent. It emits no trading signals or broker orders. The host side is modern C++17 and uses Linux `spidev` when built and run on Raspberry Pi OS. A deterministic software transport and Python reference/differential model are included so protocol and market semantics can be exercised on a development PC without pretending that physical SPI was tested.

## Responsibilities and rationale

The Raspberry Pi receives market data over its normal Linux network connection. It will eventually normalize broker/WebSocket messages into the binary event format before sending them over SPI. It also remains the final risk and execution authority.

The FPGA does not parse JSON, TLS, TCP, or WebSocket framing. It is being used for deterministic, streaming, integer/fixed-point operations such as market-state updates, feature calculations, and hardware risk checks once the communication foundation is stable. Machine learning and floating-point RTL are intentionally out of scope for V1.

## Repository layout

```text
docs/                         Architecture, protocol, arithmetic, verification, benchmarks
fpga/rtl/                     SPI slave, CDC FIFOs, reset, protocol, dispatcher, market RTL
fpga/tb/                      Self-checking RTL testbenches
fpga/constraints/             Tang Nano 20K CST pin assignments and timing constraints
fpga/gowin/                   Gowin project file for GW2AR-18C
fpga/scripts/                 Simulation and Gowin CLI build helpers
host/include/                 C++ protocol and transport interfaces
host/src/                     Linux SPI and software-loopback implementations
host/tests/                   C++ loopback/stress utility
tools/reference_model/       Independent protocol and market reference models
tools/market_data_generator/ Deterministic synthetic quote/trade event stream
```

## Build the host side

The checked-in build description is `host/CMakeLists.txt`. On a Linux/Raspberry Pi development environment:

```bash
cmake -S host -B host/build -DCMAKE_BUILD_TYPE=Release
cmake --build host/build
./host/build/loopback_test --sim --count 1000
```

On this Windows development machine, CMake was not available during the initial bootstrap, so the equivalent direct compile is:

```powershell
g++ -std=c++17 -Wall -Wextra -Wpedantic -O2 -I host/include `
  host/src/protocol.cpp host/src/spi_transport.cpp host/src/latency_tracker.cpp `
  host/tests/loopback_test.cpp `
  -o host/loopback_test.exe
.\host\loopback_test.exe --sim --count 1000
```

For physical Pi use, follow [`docs/hardware_bringup.md`](docs/hardware_bringup.md), then run the same program with `--device /dev/spidev0.0`. The program prints `mode=REAL_HARDWARE` only on the spidev path; `--sim` is explicitly labeled `mode=SIMULATION`. Its throughput is an end-to-end exchange rate over the measured run, not a claim about FPGA service time.

## Run RTL simulation

With Icarus Verilog installed:

```powershell
.\fpga\scripts\run_iverilog.ps1
```

The script compiles the RTL and self-checking testbenches into a local build directory and runs them with `vvp`. The checks cover reset, valid loopback, sequential packets, checksum rejection, incomplete frames, mode-0 response timing, FIFO backpressure/overflow visibility, a 64-packet async FIFO stress test with unrelated clocks, standalone CRC vectors, the standalone 101/64 divider, directed raw and normalized market feature/edge-case tests, measured market latency, `NUM_SYMBOLS=1/4/8/16/32` elaboration, a 32-symbol state-isolation/backpressure/reset stressbench, and deterministic 1,000- and 10,000-event Python-versus-RTL differential replays. If Gowin is installed, `fpga/scripts/run_gowin_pnr.tcl` drives the default build and `fpga/scripts/run_gowin_matrix.ps1` drives the 4/8/16/32 resource/timing matrix through `gw_sh.exe`; implementation outputs remain ignored. The measured implementation matrix and the normalized feature-layer resource/timing delta are in [`docs/benchmarking.md`](docs/benchmarking.md).

## Hardware still required

Physical validation still requires:

1. A Tang Nano 20K bitstream built with the board's actual device/toolchain settings.
2. The checked-in `fpga/constraints/tang_nano_20k.cst` reviewed against the Tang Nano 20K board documentation and the intended external SPI header wiring.
3. Safe 3.3 V-compatible connections and a shared ground between the Pi and FPGA.
4. SPI enabled on Raspberry Pi OS, with mode 0 and an initial 5 MHz clock.
5. A logic analyzer or oscilloscope if signal-integrity or clock-rate limits need investigation.

The exact wiring, power precautions, Gowin IDE flow, and Raspberry Pi commands are documented in `docs/hardware_bringup.md`. No physical bitstream download or Pi↔FPGA test has been claimed from this development environment.

## Roadmap

1. Pi ↔ FPGA SPI foundation — complete
2. Normalized quote/trade protocol and dispatcher — current milestone
3. Parameterized market-state engine, normalized fixed-point feature layer, and latency/resource characterization — current milestone
4. C++ real-time market-data receiver
5. Full Pi → FPGA event/feature path
6. Configurable signal FSM
7. Hardware risk checks
8. Historical replay and strategy evaluation
9. Paper broker integration
10. Latency, throughput, and robustness benchmarking

The next stages must report measured results only, distinguish simulation from hardware, and keep paper trading as the only execution mode.
