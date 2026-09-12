# Historical data pipeline

The historical path is deliberately shaped like the hardware path:

```text
provider/parser -> NormalizedEvent -> chronological replay
                  -> existing MarketModel -> existing StrategyModel
```

`tools/backtest/canonical.py` defines the canonical event record:

| field | meaning |
|---|---|
| `timestamp_ns` | UTC nanoseconds since Unix epoch |
| `event_type` | `quote` or `trade` (`0x01`/`0x02`) |
| `symbol_id` | compact integer symbol key |
| `price` | integer micro-dollars; trades and quotes use the same wire units |
| `quantity` | non-negative integer size |
| `side` | `0` bid/sell-side update, `1` ask/buy-side update |
| `sequence` | provider sequence, or blank before deterministic replay assignment |
| `flags` | protocol-reserved flags |

CSV is intended for inspection and debugging. Binary input is a concatenation
of the existing 32-byte CRC-8/ATM packet format, so the binary path does not
invent a second semantic encoding. Provider adapters are outside the reference
model and may parse JSON, but every adapter must emit the canonical record.

## Level-1 requirement

The backtest requires quote and trade events, not OHLCV bars. A bar contains no
executable bid/ask queue state and cannot support the fill assumptions in
`execution_simulator.py`. Alpaca support in `tools/backtest/providers.py` is
parser/downloader scaffolding: it maps historical quote snapshots to a
deterministic bid event followed by an ask event and maps trades to trade
events. No credentials are checked in, and no real dataset is included or
downloaded by tests.

If a user explicitly downloads data, the adapter reads `ALPACA_API_KEY` and
`ALPACA_API_SECRET` (or receives them in memory), writes only to the requested
output path, and supports `kind=quotes` or `kind=trades`. Downloading does not
make the resulting data a validated FPGA-compatible feed; run the quality-only
CLI first.

## Ordering and sequences

The replay first validates the supplied order, then globally sorts by
`(timestamp_ns, source_index)`. Equal timestamps therefore have a stable,
reproducible tie-break. Missing sequences are assigned one-based per-symbol
sequences after session filtering. The optional session reset makes those
counters restart on each local trading date; provider sequences are retained
and duplicate/stale behavior is left visible to the exact market model.

## Quality checks

Validation reports, without silently discarding records:

- out-of-order timestamps and large gaps;
- duplicate records and duplicate per-symbol sequences;
- negative/zero timestamps, prices, quantities, invalid side values, and
  unsupported event types;
- missing symbol mappings and malformed CSV/provider rows;
- quote crosses.

Crossed quotes are retained because the reference RTL/model defines quote
validity explicitly. A quality error does not mutate the replay stream unless
`strict_quality` is enabled in the backtest configuration.

## Sessions and reset

Regular-hours replay defaults to 09:30–16:00 in
`America/New_York`. Extended mode defaults to 04:00–20:00 and is opt-in.
At a local session boundary the simulator attempts EOD flattening, resets the
market quote/trade histories and strategy edge/cooldown state, and starts the
next day with deterministic replay sequencing when provider sequences were
missing. Session filtering happens before model evaluation, so premarket and
after-hours records do not warm the regular-hours state by accident.

The provider adapter is intentionally not a broker integration. It does not
submit orders, manage credentials, or claim that any real data was obtained.
