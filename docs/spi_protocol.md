# SPI protocol

## Framing

The transport is SPI mode 0: CPOL=0 and CPHA=0. The Raspberry Pi is master, the FPGA is slave, CS is active low, and one CS assertion frames one fixed 32-byte packet. The initial clock target is 5 MHz. RTL counters are sized for the 32-byte frame and do not encode a 5 MHz-specific timing assumption.

The first request/response exchange uses three transfers: request, turnaround, response. The turnaround transfer is intentionally explicit because a slave cannot send a complete response that depends on the final request byte until that byte has arrived and passed through the RX FIFO and loopback logic.

The turnaround and response transfers clock all-zero 32-byte filler packets. The FPGA consumes an all-zero packet as a transport filler and does not generate a status response or count it as a packet error. An all-zero packet is therefore reserved for clocking and is not a valid application request.

## Packet layout

All multi-byte fields are unsigned unless noted and are transmitted most-significant byte first (big-endian/network order).

| Byte(s) | Width | Field | Definition |
| --- | ---: | --- | --- |
| 0 | 8 | sync/version | `0xA1` for protocol version 1 |
| 1 | 8 | message type | `0x01` quote, `0x02` trade, `0x03` control, `0x04` heartbeat, `0x05` signal, `0x06` status, `0x07` loopback |
| 2–3 | 16 | symbol ID | Numeric ID; no ticker strings in RTL |
| 4–11 | 64 | timestamp | Nanoseconds since Unix epoch for normalized events; opaque for loopback |
| 12–19 | 64 | price/data | Price in micro-dollars for quote/trade events; opaque data for loopback |
| 20–23 | 32 | quantity | Unsigned quantity/data field |
| 24 | 8 | side/status | Side for market events; status code for responses |
| 25–28 | 32 | sequence | Monotonically increasing host sequence number |
| 29–30 | 16 | flags/reserved | Bit 0 of a response is `response_generated`; remaining bits reserved and zero in requests |
| 31 | 8 | checksum | CRC-8/ATM over bytes 0–30 |

CRC-8/ATM parameters: polynomial `0x07`, initial value `0x00`, no reflection, no final XOR. For every input byte, XOR it into the CRC and process eight MSB-first shifts; if the old CRC MSB is one, XOR `0x07` after the shift. The checksum byte is excluded from the CRC input.

## Market event types

`MSG_MARKET_QUOTE` (`0x01`) and `MSG_MARKET_TRADE` (`0x02`) are normalized
events produced by the Pi. The FPGA does not parse JSON, TCP, TLS, or broker
frames.

For both types, byte 12–19 is unsigned price in micro-dollars, byte 20–23 is
unsigned quantity, byte 24 is side (`0=bid/seller-side`, `1=ask/buyer-side`),
and byte 25–28 is the per-symbol sequence. Byte 4–11 is normalized
nanoseconds and byte 29–30 is reserved flags.

Quotes update only the selected book side. Trades update last trade, rolling
volume, and VWAP accumulators and never replace bid/ask. Supported starter
IDs are `0=SPY`, `1=QQQ`, `2=NVDA`, and `3=AMD`; the RTL accepts IDs below the
configured `NUM_SYMBOLS` and rejects others explicitly.

The dispatcher presents accepted quote/trade packets as a flat valid/ready
record: type, symbol, timestamp, price, quantity, side, sequence, and flags.
The market state engine returns a registered feature record with valid bits
for quote-derived fields and momentum warm-up.

## Status codes

| Code | Meaning |
| ---: | --- |
| `0x01` | loopback accepted |
| `0xE1` | invalid sync/version |
| `0xE2` | CRC/checksum failure |
| `0xE3` | unsupported message type |
| `0xE4` | symbol ID is outside `NUM_SYMBOLS` |
| `0xE5` | duplicate sequence number |
| `0xE6` | stale/lower sequence number |
| `0xE7` | market side is not 0 or 1 |

For a 32-byte malformed request, the FPGA copies the request's symbol, timestamp, data, quantity, and sequence fields into the response to simplify diagnostics. An incomplete CS-framed transfer is discarded by the per-frame reset and produces no packet; the next complete frame starts cleanly. No asynchronous incomplete-frame diagnostic is part of the production datapath.

## SPI bit order

Within each byte, bits are transferred MSB first, as configured by Linux `spidev` and implemented by the RTL. The FPGA samples MOSI on rising SCLK edges and advances its MISO bit on falling SCLK edges.
