# Real-time host runtime foundation

The host runtime is the Raspberry Pi-side coordination layer. It is written in
C++17 and is deliberately testable on a development PC before networking or
physical hardware is introduced.

```text
MarketDataSource
      |
      v
normalized MarketEvent -> SymbolRegistry -> per-slot SequenceManager
      |                                      |
      +-------------- TradingEngine --------+
                             |
                             v
                       FpgaClient -> ByteTransport -> FPGA
```

## MarketEvent

`trading::MarketEvent` is independent of Alpaca JSON and uses the FPGA packet
types `QUOTE` and `TRADE`. Its fields are event type, numeric symbol ID,
`timestamp_ns`, integer `price_microdollars`, quantity, side, sequence, and
flags. Prices are USD multiplied by 1,000,000; the host never uses binary
floating-point prices on the FPGA-facing path. Decimal text is parsed
deterministically and rejects negative values, excess fractional precision,
and overflow.

Quote-side semantics remain exactly those of the existing protocol: `0` is
BID and `1` is ASK. A complete external quote is expanded into a BID event
followed by an ASK event at the same timestamp. The FPGA still receives one
side per packet; its semantics are not changed.

## SymbolRegistry and remapping

`SymbolRegistry` supports up to 32 slots and provides ticker-to-slot and
slot-to-ticker lookup, assignment, clearing, enable/disable, duplicate-active
mapping rejection, capacity validation, and a generation counter. The default
development mapping is `0 SPY`, `1 QQQ`, `2 NVDA`, `3 AMD`; those names are
host configuration, not protocol constants in RTL.

The software remap state machine is:

```text
ACTIVE -> begin_remap/disable -> AWAITING_FPGA_CLEAR_ACK
       -> acknowledge_clear -> READY_FOR_ASSIGNMENT
       -> complete_remap(assign + enable) -> ACTIVE
```

The ACK transition is intentionally explicit. Real hardware must provide the
actual `CLEAR_SYMBOL_STATE` ACK; the host does not pretend to receive one.

## Sequences and FPGA client

`SequenceManager` owns one monotonic `uint32_t` sequence per slot. The first
sequence is zero, it increments only when an event is prepared for FPGA
submission, and a coordinated remap resets it. Reaching `UINT32_MAX` raises an
explicit overflow error; the host never silently wraps.

`FpgaClient` converts a `MarketEvent` into the existing exact 32-byte big-endian
packet and reuses `Packet::serialize()` and the existing CRC-8/ATM helper. It
submits one-way market events through the existing `ByteTransport`, exposes
transport counters, and provides a control-packet boundary for supported
commands. It does not fabricate candidate results.

The existing `SpiTransport` remains the real Linux `/dev/spidev*` transport.
`SoftwareLoopbackTransport` is used only for deterministic simulation; its
market-event path is one-way and its control/loopback path models the existing
three-transfer diagnostic exchange.

## TradingEngine and sources

`MarketDataSource` is a small abstract interface:

```cpp
virtual bool next(MarketEvent& event) = 0;
```

This allows future Alpaca WebSocket, historical replay, or synthetic sources
without changing the engine. `SyntheticMarketDataSource` currently emits a
deterministic sequence of BID quote, ASK quote, trade, BID update, and ASK
update events across the four default symbols. It is a development source,
not a market simulator.

`TradingEngine` validates numeric slots and enabled mappings, assigns outgoing
sequences, records latency samples, and submits events through `FpgaClient`.
Its counters include received, rejected, sent, transport failures, unknown
symbols, sequence assignments, first/last market timestamps, and an explicit
last error. It contains no strategy, broker, account, order, or P&L logic.

Run the deterministic path with:

```text
trading_engine --sim --events 20
```

The executable reports `mode=SIMULATION`, symbol count, lifecycle counters,
and latency sample count. `--device /dev/spidev0.0 --speed 5000000` selects the
future hardware transport only when Linux spidev actually opens successfully.

## Result egress and future work

The FPGA candidate engine currently exists internally. There is not yet an
externally defined candidate/result packet path from RTL to the host. The host
therefore exposes a future-facing control/client boundary but does not claim
to receive candidate decisions.

The real-time Alpaca market-data adapter is now available behind the optional
`FPGA_TRADER_ENABLE_NETWORKING` CMake option. Its lifecycle, normalization,
bounded queue, telemetry, credential policy, and dependency requirements are
documented in [`docs/market_data_streaming.md`](market_data_streaming.md).
Later milestones can add a deliberately specified FPGA result packet/RTL path,
host final risk and portfolio ownership, paper-broker integration, and
physical Pi-to-Tang SPI validation. None of those are implemented by the
market-data milestone.
