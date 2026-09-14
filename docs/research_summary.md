# Historical research summary

Strategy validation was performed to test whether the FPGA/reference feature
set supported a defensible paper-trading signal before investing further in
integration. It did not establish profitability, and none of the failed
research strategies is an active project feature.

- V1 was tested against real Alpaca IEX Level-1 quote/trade data and produced
  strongly negative results.
- V2 investigated time-based momentum, VWAP, cooldown, and persistence
  variants; the family remained weak.
- V3 investigated relative strength, breakout, volatility, and activity
  variants; the family remained weak.
- Edge attribution showed negative midpoint selection before execution cost.
- Level-1 microstructure discovery also remained weak.

These findings are historical engineering evidence, not a claim that every
future strategy is impossible. Detailed research code, tests, and ignored
results remain recoverable from the annotated
`research-archive-2026-09-14` tag without rewriting Git history.

Future strategy logic is intentionally modular and replaceable. The current
engineering priority is completing the real-time hardware/software trading
platform: deterministic host event handling, networking, SPI integration,
candidate-result transport, host risk, portfolio state, paper brokerage, and
physical validation.
