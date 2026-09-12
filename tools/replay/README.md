# Offline strategy replay

`replay_strategy.py` drives the integer `MarketModel` and `StrategyModel` with
normalized CSV events. It produces candidate telemetry only; it does not model
orders, positions, cash, execution, or profitability.

Input columns:

```text
timestamp,event_type,symbol_id,price,quantity,side,sequence,flags
```

`event_type` may be `quote`, `trade`, `1`, or `2`. Prices use the existing
micro-dollar units. Example:

```powershell
python tools/replay/replay_strategy.py events.csv `
  --output signals.csv `
  --packet-out fpga_packets.hex
```

The output contains `symbol_id`, `sequence`, `action`, `score`, and
`reason_bits`. `--packet-out` is a future-hardware replay aid and does not
claim that a board was exercised.
