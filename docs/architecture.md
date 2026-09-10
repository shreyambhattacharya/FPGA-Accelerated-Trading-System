# Architecture

## Scope of this milestone

This stage establishes a reliable binary packet path before any trading strategy is added. The FPGA path is:

```text
SPI pins -> SPI mode-0 slave -> RX packet FIFO -> loopback validator
                                      -> TX packet FIFO -> SPI mode-0 transmitter
```

The current processing logic is intentionally small. It checks the sync/version byte, message type, and CRC, then returns a deterministic `STATUS` packet. A valid `LOOPBACK` request receives status `OK`; malformed 32-byte packets receive a specific error status while preserving the request sequence number.

## Pi responsibilities

The Raspberry Pi 5 is the SPI master. It will own Linux networking, TLS/WebSockets, external market-data parsing, normalization, configuration, logs, historical replay, backtesting, portfolio state, final risk approval, and paper-broker APIs. The FPGA never needs to understand an external broker's JSON or wire framing.

## FPGA responsibilities

The FPGA is the deterministic data-plane accelerator. In later stages it will update market state and calculate integer/fixed-point features, signal decisions, and hardware risk checks. The first stage proves only the transport and buffering path.

## Clocking note

The first RTL proof keeps packet FIFOs and loopback processing in the SPI clock domain. This avoids silently introducing an unsafe clock-domain crossing while the packet semantics are being verified. The top-level `clk` port is reserved for the continuously running FPGA clock and future control/status logic. A later hardening stage should replace the packet FIFOs with an explicitly verified dual-clock FIFO or a request/response CDC bridge before a continuously running system clock is used for the market pipeline.

This is a deliberate first-milestone tradeoff, not a claim that a raw SPI clock is the final system clock architecture.

## Request/response framing

CS is a packet frame boundary. Every transaction clocks exactly 32 bytes. The response is not returned in the same transaction because the FPGA must first receive the complete request and run it through the RX FIFO and loopback logic. The host therefore uses three CS-framed transfers:

1. request: 32 request bytes;
2. turnaround: 32 zero bytes, allowing RX FIFO processing to complete;
3. response: 32 zero bytes, receiving the TX FIFO packet.

The host checks the response packet's CRC, type, status, and sequence. A missing or zero response is an error, not a successful loopback.

## Physical interface

| Raspberry Pi 5 signal | Tang Nano signal | Direction |
| --- | --- | --- |
| MOSI | FPGA MOSI input | Pi → FPGA |
| MISO | FPGA MISO output | FPGA → Pi |
| SCLK | FPGA SPI clock input | Pi → FPGA |
| CE0 / chip select | FPGA CS input | Pi → FPGA |
| GND | GND | reference |

Use the Pi's normal 3.3 V GPIO SPI signals and a common ground. Do not connect until the Tang Nano 20K board-specific pins have been confirmed from authoritative board documentation. No pin numbers are guessed in this repository; see `fpga/constraints/tang_nano_20k_placeholders.cst`.
