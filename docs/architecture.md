# Architecture

## Scope of this milestone

This stage establishes a reliable binary packet path before any trading strategy is added. The FPGA path is:

```text
SPI pins -> SPI mode-0 slave -> RX packet FIFO -> loopback validator
                                      -> TX packet FIFO -> SPI mode-0 transmitter
```

The current processing logic is intentionally small. It checks the sync/version byte, message type, and CRC, then returns a deterministic `STATUS` packet. A valid `LOOPBACK` request receives status `OK`; malformed 32-byte packets receive a specific error status while preserving the request sequence number.

## Timing-hardened packet validation

The loopback stage validates and generates CRC-8/ATM values with the standalone `fpga/rtl/protocol/crc8_engine.sv`. It accepts one byte per `clk27` cycle and consumes the first 31 bytes of each 32-byte packet; the final byte is the transmitted CRC. The request and response CRC streams are separated by registered FSM states, so the old full-packet combinational CRC/response cone is no longer a single-cycle `clk27` path. The engine reports `done` after the final byte edge and holds the result until the next stream.

At 27 MHz, each 31-byte CRC stream takes 31 system-clock cycles (about 1.15 us). The loopback FSM takes approximately 67 `clk27` cycles from request acceptance to `response_valid`, including request validation, response construction, and response CRC. The host protocol still uses the existing 32-byte request, 32-byte turnaround, and 32-byte response transfers. At 5 MHz SPI, the 32-byte turnaround supplies 51.2 us, or about 1,382 `clk27` cycles, leaving more than 1,300 system-clock cycles of processing margin.

## Pi responsibilities

The Raspberry Pi 5 is the SPI master. It will own Linux networking, TLS/WebSockets, external market-data parsing, normalization, configuration, logs, historical replay, backtesting, portfolio state, final risk approval, and paper-broker APIs. The FPGA never needs to understand an external broker's JSON or wire framing.

## FPGA responsibilities

The FPGA is the deterministic data-plane accelerator. In later stages it will update market state and calculate integer/fixed-point features, signal decisions, and hardware risk checks. The first stage proves only the transport and buffering path.

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
