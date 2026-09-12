# Candidate-signal strategy contract

This milestone adds a deterministic candidate-signal engine. It evaluates
normalized features and reports `SIGNAL_NONE`, `SIGNAL_LONG_CANDIDATE`, or
`SIGNAL_SHORT_CANDIDATE`. A candidate is an observation for the Raspberry Pi;
it is never an order, position, risk approval, broker request, or live-trading
action.

## Inputs and action

Each accepted feature is evaluated independently for its symbol and sequence.
The engine requires all of these validity bits before a directional candidate
can be eligible:

| Required field | RTL representation |
| --- | --- |
| spread | signed spread bps x100, valid |
| momentum | signed momentum bps x100, valid |
| quote imbalance | signed Q1.15 normalized imbalance, valid |
| VWAP | unsigned quotient, valid |
| midpoint minus VWAP | signed bps x100, valid |

The rolling-volume value is compared numerically and does not have a separate
valid bit; the market engine's reset value is zero.

For a valid, enabled symbol and strategy, the long candidate condition is:

```text
momentum >= long_min_momentum_bps_x100
and midpoint_minus_vwap_bps_x100 >= long_min_vwap_delta_bps_x100
and imbalance_q15 >= long_min_imbalance_q15
and spread_bps_x100 <= max_spread_bps_x100
and rolling_volume >= min_rolling_volume
```

The short condition is symmetric:

```text
momentum <= short_max_momentum_bps_x100
and midpoint_minus_vwap_bps_x100 <= short_max_vwap_delta_bps_x100
and imbalance_q15 <= short_max_imbalance_q15
and spread_bps_x100 <= max_spread_bps_x100
and rolling_volume >= min_rolling_volume
```

Threshold equality passes. If intentionally overlapping thresholds make both
directions true, long has deterministic priority. The side enable is applied
after the raw direction is computed.

## Score and reason bits

The 4-bit score is the number of passed directional factors, from 0 through 5.
For a non-directional record it counts the factors that pass in either
direction. Every accepted feature creates a signal record, including
`SIGNAL_NONE`, so the Pi can diagnose invalid or suppressed evaluations.

The 16-bit reason field uses this fixed map:

| Bit | Meaning |
| ---: | --- |
| `0x0001` | momentum factor passed |
| `0x0002` | VWAP-delta factor passed |
| `0x0004` | imbalance factor passed |
| `0x0008` | spread factor passed |
| `0x0010` | volume factor passed |
| `0x0020` | raw long direction |
| `0x0040` | raw short direction |
| `0x0080` | cooldown was active and suppressed emission |
| `0x0100` | one or more required feature-valid bits were absent |
| `0x0200` | symbol slot disabled |
| `0x0400` | strategy globally disabled |
| `0x0800` | long side disabled |
| `0x1000` | short side disabled |
| `0x2000` | raw direction did not emit on this evaluation |

## Edge, cooldown, and reversal semantics

The state bank holds `previous_condition`, `last_emitted_action`, and a
per-symbol `cooldown_remaining` counter. A candidate fires on a false-to-true
direction edge when the counter is zero. Once emitted, the counter is loaded
with `signal_cooldown_events`. A sustained condition is suppressed while the
counter is nonzero. When the counter reaches one, the next accepted event
emits a repeat of the same direction and reloads the counter. A cooldown of
zero disables repeat suppression.

A direction reversal is a new edge and emits when the cooldown is clear. A
reversal during an active cooldown remains suppressed; it does not bypass the
counter. Global strategy disable, side disable, or symbol disable disarms the
stored previous condition. Re-enabling while the raw condition remains true
therefore produces a fresh edge. `CLEAR_SYMBOL_STATE` clears all three pieces
of signal state for one symbol and is coordinated with the market-state reset.

## Runtime configuration

The reset defaults are test/development defaults only. Strategy evaluation is
disabled by default; both sides and all configured symbol slots start enabled.
The runtime configuration bank accepts the following `MSG_CONTROL` commands:

| Subcommand | Data | Effect |
| ---: | --- | --- |
| `0x10` | `data64[2:0]` | global, long, and short enables |
| `0x11` | signed `data32` | long minimum momentum |
| `0x12` | signed `data32` | short maximum momentum |
| `0x13` | signed `data32` | long minimum VWAP delta |
| `0x14` | signed `data32` | short maximum VWAP delta |
| `0x15` | signed `data32[15:0]` | long minimum imbalance Q1.15 |
| `0x16` | signed `data32[15:0]` | short maximum imbalance Q1.15 |
| `0x17` | signed `data32` | maximum spread bps x100 |
| `0x18` | `data64[TRADE_ACC_W-1:0]` | minimum rolling volume |
| `0x19` | unsigned `data32` | cooldown events |
| `0x1A` | `data64[0]`, symbol field | per-symbol enable |
| `0x1B` | symbol field | coordinated market/signal slot reset |
| `0x1C` | reserved | readback is not implemented in this milestone |

Control packets use the existing 32-byte CRC-protected packet format and are
acknowledged through the existing loopback response path. A valid write returns
`STATUS_OK`; an out-of-range symbol or unsupported subcommand returns
`STATUS_BAD_CONTROL`; a slot reset held behind either state engine returns
`STATUS_CONFIG_BUSY`. Market quote/trade packet encoding is unchanged.

## Offline replay

`tools/replay/replay_strategy.py` reads normalized event CSV rows, runs the
market and strategy reference models, and writes one candidate row for every
accepted feature. It can also write fixed-width CRC-protected packet hex for
transport-oriented fixtures:

```powershell
python tools/replay/replay_strategy.py tools/replay/example_events.csv `
  --output build/example_signals.csv --packet-out build/example_packets.hex
```

Replay counters describe evaluated features and candidate classifications only.
They do not fabricate P&L, positions, fills, risk, or broker results.
