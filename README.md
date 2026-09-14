# FPGA-Accelerated Trading System

An engineering platform for automated paper trading: Raspberry Pi 5 host
software receives and normalizes market data, manages symbols and sequences,
sends fixed-width events over SPI, and will own final risk, portfolio state,
and paper-broker integration. A Tang Nano 20K FPGA provides deterministic
fixed-point market-state processing and replaceable candidate-signal logic.

The project is paper-trading-only. It makes no profitability claim, has no
live-money path, and does not claim that physical Pi-to-FPGA SPI validation is
complete.

## Current milestone: Real-Time Host Integration

```text
External market-data feed
          |
          v
Raspberry Pi 5 / Linux C++
  networking, normalization, symbols, logging, telemetry
          |
          | SPI
          v
Tang Nano 20K FPGA
  market state, fixed-point features, candidate engine
          |
          | current result egress: not implemented
          v
Raspberry Pi host
  final risk, portfolio state, paper broker, fills, P&L
```

The current host runtime includes a provider-independent normalized
`MarketEvent`, a 32-slot `SymbolRegistry`, per-slot sequence management, a
synthetic source, an optional real-time Alpaca market-data WebSocket receiver,
an FPGA packet client, and a `TradingEngine` simulation path. Networking is
opt-in because Boost.Beast, OpenSSL, and nlohmann/json are external
dependencies; the core protocol and synthetic tests remain dependency-free.
Candidate-result egress, risk approval, and broker execution remain future
work.

The FPGA V1 candidate engine is a reference/demo strategy implementation. The
strategy is intentionally replaceable. Historical validation rejected
profitability claims for V1, V2, V3, and the later Level-1 microstructure
research; those results are summarized in [`docs/research_summary.md`](docs/research_summary.md)
and recoverable through the `research-archive-2026-09-14` tag.

## Repository layout

```text
fpga/                         Tang Nano RTL, constraints, testbenches
host/                         C++17 protocol, transport, and host runtime
tools/backtest/               Reusable canonical replay/backtest infrastructure
tools/reference_model/        Independent protocol/market/strategy models
tools/replay/                 Offline normalized-event replay utilities
tools/market_data_generator/  Deterministic synthetic event generation
configs/backtest/             V1 reference configuration
docs/                         Architecture, protocol, bring-up, and validation
```

## Build and run the host simulation

On Linux or Raspberry Pi OS:

```bash
cmake -S host -B host/build -DCMAKE_BUILD_TYPE=Release
cmake --build host/build
./host/build/trading_engine --sim
```

Expected output is deterministic and reports the simulation mode, event
counters, transport failures, and unknown symbols. It does not print
profitability statistics or submit orders.

The existing protocol and loopback utilities remain available:

```bash
./host/build/protocol_unit_test
./host/build/loopback_test --sim --count 100
```

`--device /dev/spidev0.0 --speed 5000000` is reserved for future real-hardware
bring-up. The program labels REAL_HARDWARE only when the Linux spidev path is
actually selected and opened.

## Historical backtesting support

The generic backtest path remains available for validating future FPGA
strategies against canonical quote/trade data:

```powershell
python -m tools.backtest.run_backtest `
  --data tools/backtest/fixtures/hand_check.csv `
  --config configs/backtest/v1_reference.json `
  --output build/backtest_run
python -m unittest discover -s tools/backtest/tests -p "test_*.py" -v
```

The fixture is for deterministic correctness checks, not statistical claims.
Raw data, normalized data, research caches, results, build outputs, and local
credentials remain ignored.

## Roadmap

1. FPGA SPI/protocol foundation — complete in simulation
2. FPGA market-state and normalized feature pipeline — complete
3. Configurable FPGA candidate-signal engine — complete
4. Historical replay / real-data validation infrastructure — complete
5. C++ real-time host-engine foundation — complete
6. Real-time market-data WebSocket receiver — complete
7. Pi-to-FPGA normalized event path — simulation path complete; physical validation next
8. FPGA-to-Pi candidate/result packet path
9. Symbol-slot management and dynamic remapping
10. Host risk and portfolio engine
11. Alpaca PAPER broker integration
12. Physical Pi-to-Tang SPI validation
13. End-to-end latency, fault, and recovery testing
14. Continuous paper-trading system

The next engineering milestone is the FPGA-to-Pi candidate/result packet path.
No step implies physical SPI completion, live trading, or a profitable
strategy without a measured implementation and explicit validation.
