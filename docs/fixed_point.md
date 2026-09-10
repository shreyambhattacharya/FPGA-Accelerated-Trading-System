# Fixed-point design notes

No market indicator arithmetic is implemented in this milestone. The protocol reserves a 64-bit data field so future price semantics can be introduced without using floating point in RTL.

The current candidate for market prices is an integer number of micro-dollars (`1 USD = 1,000,000` units) stored in an unsigned 64-bit field. Before freezing that choice, the project should measure the supported instrument range, decide whether signed deltas are required, and specify saturation versus wrap behavior for every operation.

Future feature modules must document:

- input and output bit widths;
- scale and signedness;
- intermediate widths and overflow behavior;
- rounding/truncation behavior;
- reset and warm-up behavior for rolling windows;
- matching software reference-model semantics.

The FPGA V1 rule is deterministic integer/fixed-point arithmetic only. Synthesizable floating point and machine learning are not part of the initial design.
