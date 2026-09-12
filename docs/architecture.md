# Architecture

## Scope of this milestone

This stage keeps the reliable binary packet path and adds the first real
market-data stage. The FPGA path is:

```text
SPI pins -> SPI mode-0 slave -> RX packet FIFO -> packet dispatcher
                                      |              |-> loopback validator -> TX packet FIFO
                                      |              `-> normalized event -> market state/features
                                      `------------------------------------> SPI mode-0 transmitter
```

`packet_dispatcher.sv` validates sync and CRC with the shared sequential CRC
engine, preserves the existing loopback/status path, and exposes a flat
valid/ready event record for quote/trade traffic. `MSG_LOOPBACK` and malformed
or unsupported packets remain diagnostic traffic. Valid quote/trade packets
with an out-of-range symbol produce an explicit `STATUS_BAD_SYMBOL` error and
do not enter market state.

The starter symbol table is numeric and parameterized: `0=SPY`, `1=QQQ`,
`2=NVDA`, and `3=AMD`. `NUM_SYMBOLS` can be changed without changing the
packet format or physical interface.

## Timing-hardened packet validation

The loopback stage validates and generates CRC-8/ATM values with the standalone `fpga/rtl/protocol/crc8_engine.sv`. It accepts one byte per `clk27` cycle and consumes the first 31 bytes of each 32-byte packet; the final byte is the transmitted CRC. The request and response CRC streams are separated by registered FSM states, so the old full-packet combinational CRC/response cone is no longer a single-cycle `clk27` path. The engine reports `done` after the final byte edge and holds the result until the next stream.

At 27 MHz, each 31-byte CRC stream takes 31 system-clock cycles (about 1.15
us). Market events then pass through a registered dispatcher boundary and a
multi-state market update/feature/output sequence. The feature record is
registered and held under downstream backpressure. Market packets do not
create a loopback response; the existing three-transfer request/turnaround/
response framing remains available for diagnostics and future result packets.

## Pi responsibilities

The Raspberry Pi 5 is the SPI master. It will own Linux networking, TLS/WebSockets, external market-data parsing, normalization, configuration, logs, historical replay, backtesting, portfolio state, final risk approval, and paper-broker APIs. The FPGA never needs to understand an external broker's JSON or wire framing.

## FPGA responsibilities

The FPGA is the deterministic data-plane accelerator. This stage updates
per-symbol quote/trade state and calculates spread, midpoint, momentum,
rolling volume, signed quote imbalance, and VWAP numerator/denominator
accumulators. It deliberately emits no trading signal, strategy decision, risk
approval, broker order, or live-trading action.

## Market-state semantics

The market engine accepts only `MSG_MARKET_QUOTE` and `MSG_MARKET_TRADE` on its
normalized interface. Sequence numbers are unsigned and monotonic per symbol:
the first event is accepted, a larger sequence is accepted, an equal sequence
is rejected as duplicate, and a lower sequence is rejected as stale. Sequence
wrap is not supported in this milestone.

Quotes with side `0` update only the bid; side `1` updates only the ask.
Trades update last-trade state, rolling volume, and VWAP accumulators but never
overwrite either quote side. A quote is usable only when both sides are valid
and `ask >= bid`; crossed or incomplete quotes retain their raw sides but do
not produce spread/midpoint/imbalance or append a momentum observation. Each
accepted event then calculates a feature record; a trade also observes the
current safe quote and can append its midpoint sample.

Trade volume uses a parameterized circular window (default 32). Midpoint
history uses a circular window (default 16) and momentum becomes valid only
after that many valid midpoint observations. VWAP uses a circular window
(default 32), replacing old `price*quantity` and quantity terms without
recomputing the window. The output exposes the accumulators; division is left
to a later consumer.

## Clocking note

The Tang Nano's onboard 27 MHz oscillator (`clk`, physical pin 4) is the system clock. SPI framing and bit shifting remain in the external `spi_clk` domain. RX and TX packet movement crosses the boundary through independent Gray-pointer asynchronous FIFOs, with two-flop synchronizers for each pointer. Packet validation and response generation therefore run synchronously at 27 MHz; no raw SPI-clock signal is used as the processing clock.

The shared power-on reset is generated from the 27 MHz clock and does not require a Raspberry Pi reset wire. SPI-domain state also has FPGA configuration-time reset values so the design is safe while the Pi holds SCLK idle during FPGA power-up. The async FIFO testbench intentionally uses unrelated clocks and checks ordering, full/empty behavior, overflow/underflow counters, and reset while traffic is resident.

This is a deliberate first-milestone tradeoff, not a claim that a raw SPI clock is the final system clock architecture.

## Request/response framing

CS is a packet frame boundary. Every transaction clocks exactly 32 bytes. The response is not returned in the same transaction because the FPGA must first receive the complete request and run it through the RX FIFO and loopback logic. The host therefore uses three CS-framed transfers:

1. request: 32 request bytes;
2. turnaround: 32 zero bytes, allowing RX FIFO processing to complete;
3. response: 32 zero bytes, receiving the TX FIFO packet.

The host checks the response packet's CRC, type, status, and sequence. A missing or zero response is an error, not a successful loopback.

The all-zero 32-byte turnaround and response writes are transport filler used to provide clock edges. The FPGA accepts filler without creating a status response or packet-error event; nonzero packets enter the normal protocol validator.

## Physical interface

| Raspberry Pi 5 signal | Tang Nano 20K signal | Direction |
| --- | --- | --- |
| GPIO10 / physical pin 19, MOSI | `spi_mosi`, FPGA pin 27 | Pi → FPGA |
| GPIO9 / physical pin 21, MISO | `spi_miso`, FPGA pin 28 | FPGA → Pi |
| GPIO11 / physical pin 23, SCLK | `spi_clk`, FPGA pin 25 | Pi → FPGA |
| GPIO8 / physical pin 24, CE0 | `spi_cs_n`, FPGA pin 26 | Pi → FPGA |
| GND | GND | reference |

The onboard 27 MHz oscillator is constrained as `clk` on FPGA pin 4. LED0–LED5 use FPGA pins 15–20 and are active-low; LED0 is a heartbeat, LED1 is packet activity, and LED2 is a sticky packet/protocol error. The LCD/FPC connector must remain disconnected for this external SPI wiring. Use the Pi's normal 3.3 V GPIO SPI signals and a common ground; do not connect Pi 5 V or Pi 3.3 V to the USB-powered Tang Nano. See `docs/hardware_bringup.md` and `fpga/constraints/tang_nano_20k.cst` for the checked-in bring-up assumptions.
