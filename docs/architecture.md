# Architecture

## Scope of this milestone

This stage keeps the reliable binary packet path and adds the first real
market-data and candidate-signal stages. The FPGA path is:

```text
SPI pins -> SPI mode-0 slave -> RX packet FIFO -> packet dispatcher
                                      |              |-> loopback validator -> TX packet FIFO
                                       |              `-> normalized event -> market state/features -> candidate signal
                                       |              `-> control -> strategy config / slot reset -> loopback ACK
                                      `------------------------------------> SPI mode-0 transmitter
```

`packet_dispatcher.sv` validates sync and CRC with the shared sequential CRC
engine, preserves the existing loopback/status path, and exposes a flat
valid/ready event record for quote/trade traffic. `MSG_LOOPBACK` and malformed
or unsupported packets remain diagnostic traffic. Valid `MSG_CONTROL` packets
write the strategy configuration bank or request a coordinated symbol-slot
reset, then use the existing loopback response as an ACK. Valid quote/trade
packets with an out-of-range symbol produce an explicit `STATUS_BAD_SYMBOL`
error and do not enter market state.

The starter symbol table is numeric and parameterized: `0=SPY`, `1=QQQ`,
`2=NVDA`, and `3=AMD`. `NUM_SYMBOLS` can be changed without changing the
packet format or physical interface. Each accepted feature record now also
contains the exact rolling VWAP quotient, normalized Q1.15 quote imbalance,
spread bps x100, momentum bps x100, and midpoint-minus-VWAP raw/bps fields;
these are internal feature-bus fields until a later result/CSR packet is
defined.

## Measured N32 bottleneck and refactor

The baseline for commit `651fc38` was measured from the Gowin post-P&R report.
The exact N32 critical path was:

```text
impl/packet_dispatcher_i/event_symbol_id_reg_4_s0/Q
  -> impl/market_state_engine_i/last_sequence_number[20]_7_s0/CE
```

It had 34.017 ns data delay, 2.985 ns setup slack against the 37.037 ns
`clk27` constraint, and a reported `clk27` Fmax of 29.366 MHz. This cone
combined packet-derived symbol decode, dynamic per-symbol bank access, and
wide state write-enable generation. The baseline N32 market-engine hierarchy
reported 1,038 registers, 715 LUTs, and 6 BSRAM blocks; the device P&R report
showed 130 total `SSRAM(RAM16)` resources.

The new market engine is serialized and registered:

```text
IDLE -> READ_STATE -> CHECK_SEQUENCE -> SET_HISTORY_ADDR
     -> READ_HISTORY -> CAPTURE_HISTORY -> [MULTIPLY_TRADE]
     -> APPLY_EVENT -> FEATURE_CALC -> NORMALIZE_START
     -> NORMALIZE_WAIT -> WRITE_STATE
     -> WRITE_HISTORY -> OUTPUT
```

The event fields, symbol index, and one-hot bank select are captured first.
`READ_STATE` copies all selected per-symbol fields into a working register
bank. History terms are read and captured in `old_*` registers before rolling
sum and momentum arithmetic. `FEATURE_CALC` therefore consumes registered
working values instead of a live 32-way array read feeding the feature cone.
`WRITE_STATE` commits only the selected bank. The longer schedule is an
intentional latency/area/timing tradeoff.

The selected symbol's quote/sequence state, valid bits, rolling accumulators,
pointers, and counts are packed into one logical per-symbol state word. A
reset bitmap provides logical-zero semantics without clearing every memory
word. This keeps the single-event read/modify/write transaction coherent while
allowing Gowin to map the bank compactly. The four circular histories remain
separate banked arrays with one registered read and one write at the commit
stage; their contents and replacement semantics are unchanged.

Gowin logs RAM extraction for `state_bank`, `trade_quantity_history`,
`midpoint_history`, `vwap_price_quantity_history`, and
`vwap_quantity_history`. The final N32 post-P&R mapping uses 11 BSRAM and 324
`SSRAM(RAM16)` resources; the state bank and history memories are now compact
memory targets rather than a NUM_SYMBOLS-wide flip-flop bank. The protocol
FIFOs and history contents remain intact.

There is one shared, event-serialized unsigned `64 x 32 -> 96` multiplier in
`MULTIPLY_TRADE`, and one shared sequential unsigned divider in
`feature_normalizer.sv`. The default divider is 101-bit numerator by 64-bit
denominator, iterates once per numerator bit, and is scheduled across VWAP,
imbalance, spread, momentum, and midpoint-minus-VWAP operations. Bps scaling is
staged before the divide; invalid or zero-denominator operations exit early.
No per-symbol divider or normalized feature engine is instantiated.

The internal normalized feature fields are:

| Field | Representation | Validity |
| --- | --- | --- |
| VWAP quotient | unsigned 64-bit | rolling quantity sum is nonzero and divider completes |
| normalized imbalance | signed 16-bit Q1.15 | safe two-sided quote and quantity denominator nonzero |
| spread bps x100 | signed 32-bit | safe quote and midpoint nonzero |
| momentum bps x100 | signed 32-bit | momentum warm-up complete and reference midpoint nonzero |
| midpoint-minus-VWAP | signed 65-bit raw plus signed 32-bit bps x100 | raw requires quote plus VWAP; bps also requires nonzero VWAP |

The divider's `done` is captured before state/history commit, so the external
feature valid/ready contract remains registered and stable under backpressure.
For the default windows, the latency testbench measured 224 `clk27` cycles for
a warm two-sided quote, 536 cycles for a fully warm trade, and 535 cycles for a
fully warm quote with all five divide operations active. Exact latency varies
with feature validity because skipped operations cost only scheduler cycles;
the steady-state event rate is correspondingly data-dependent.

## Candidate-signal stage

`signal_engine.sv` consumes one registered normalized feature record at a time
and emits one registered candidate record for every accepted feature. The
record contains symbol ID, sequence, action (`SIGNAL_NONE`,
`SIGNAL_LONG_CANDIDATE`, or `SIGNAL_SHORT_CANDIDATE`), a five-factor score, and
diagnostic reason bits. It is a candidate notification only: it is not an
order, position, risk approval, broker request, or live-trading action.

The long and short predicates require valid momentum, midpoint-minus-VWAP bps,
normalized imbalance, VWAP quotient, and spread inputs. They then apply the
directional thresholds, maximum spread, rolling-volume minimum, strategy and
side enables, per-symbol enable, and edge/cooldown state. Long has deterministic
priority if thresholds overlap. See [`strategy.md`](strategy.md) for the exact
comparison operators, reason-bit assignments, runtime command map, and replay
format.

The signal record uses a standard valid/ready boundary. The feature engine's
`feature_ready` is driven by the signal engine, so a stalled downstream
consumer holds the complete record and blocks the upstream event path without
changing its fields. Per-symbol condition, last-emitted direction, and
cooldown counters are independent. A coordinated `CLEAR_SYMBOL_STATE` command
resets both market and signal state only when both stages are able to accept
the reset.

The direct latency measurement adds exactly one `clk27` cycle from a registered
feature record to the registered signal record. With the default normalized
path, the integrated latency bench measured 225 cycles for a warm quote, 537
cycles for a fully warm trade, and 536 cycles for a fully warm quote with all
five normalized divisions active. These are FPGA-clock simulation measurements;
they exclude SPI wire time, CDC FIFO occupancy, dispatcher delay, host
scheduling, and any future signal consumer.

## Timing-hardened packet validation

The loopback stage validates and generates CRC-8/ATM values with the standalone `fpga/rtl/protocol/crc8_engine.sv`. It accepts one byte per `clk27` cycle and consumes the first 31 bytes of each 32-byte packet; the final byte is the transmitted CRC. The request and response CRC streams are separated by registered FSM states, so the old full-packet combinational CRC/response cone is no longer a single-cycle `clk27` path. The engine reports `done` after the final byte edge and holds the result until the next stream.

At 27 MHz, each 31-byte CRC stream takes 31 system-clock cycles (about 1.15
us). The normalized market engine now asserts a feature only after the shared
divider schedule completes. With the signal consumer ready, the latency
testbench measured 224 `clk27` cycles for a warm two-sided quote, 536 cycles
for a fully warm trade, and 535 cycles for a fully warm quote with all five
divide operations active; the corresponding event-to-signal records were 225,
537, and 536 cycles. Invalid operations take the early-exit path, so exact
latency is data-dependent. Backpressure holds the registered feature/signal
record and deasserts `event_ready`; these rates exclude SPI, FIFO, dispatcher,
host, and future consumer costs. Market packets do not create a loopback
response; the existing three-transfer request/turnaround/response framing
remains available for diagnostics and future result packets.

## Pi responsibilities

The Raspberry Pi 5 is the SPI master. It will own Linux networking, TLS/WebSockets, external market-data parsing, normalization, configuration, logs, historical replay, backtesting, portfolio state, final risk approval, and paper-broker APIs. The FPGA never needs to understand an external broker's JSON or wire framing.

## FPGA responsibilities

The FPGA is the deterministic data-plane accelerator. This stage updates
per-symbol quote/trade state and calculates spread, midpoint, momentum,
rolling volume, signed quote imbalance, VWAP numerator/denominator
accumulators, VWAP quotient, and normalized bps/imbalance features. It then
emits candidate-only signal records with explainable reason bits. It does not
emit a risk approval, broker order, or live-trading action.

## Market-state semantics

The market engine accepts only `MSG_MARKET_QUOTE` and `MSG_MARKET_TRADE` on its
normalized interface. Sequence numbers are unsigned and monotonic per symbol:
the first event is accepted, a larger sequence is accepted, an equal sequence
is rejected as duplicate, and a lower sequence is rejected as stale. Sequence
wrap is not supported in this milestone.

Quotes with side `0` update only the bid; side `1` updates only the ask.
Trades update rolling volume and VWAP accumulators but never overwrite either
quote side. A quote is usable only when both sides are valid
and `ask >= bid`; crossed or incomplete quotes retain their raw sides but do
not produce spread/midpoint/imbalance or append a momentum observation. Each
accepted event then calculates a feature record; a trade also observes the
current safe quote and can append its midpoint sample.

Trade volume uses a parameterized circular window (default 32). Midpoint
history uses a circular window (default 16) and momentum becomes valid only
after that many valid midpoint observations. VWAP uses a circular window
(default 32), replacing old `price*quantity` and quantity terms without
recomputing the window. The exact VWAP quotient is valid when the rolling
quantity sum is nonzero. Normalized imbalance is signed Q1.15 and is valid
only for a safe quote with nonzero total quantity. Spread bps x100 requires a
safe quote with nonzero midpoint; momentum bps x100 requires momentum warm-up
and a nonzero reference midpoint. Midpoint-minus-VWAP raw delta requires both
a safe quote and valid VWAP; its bps form additionally requires nonzero VWAP.
All normalized outputs are registered and zero with valid deasserted when
their prerequisite is absent.

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
