# Verification plan

## Current automated checks

The SystemVerilog testbench is self-checking and exercises:

- reset and repeated CS-framed transfers;
- a valid loopback packet;
- sequence preservation across several packets;
- CRC rejection with a deterministic error status;
- incomplete-frame detection;
- RX FIFO overflow visibility;
- TX FIFO empty behavior during request/turnaround transfers;
- mode-0 response scheduling across request, turnaround, and response frames;
- power-on reset with the external SPI clock held idle;
- async FIFO ordering, full/empty flags, reset during traffic, and overflow/underflow counters using unrelated clocks.

The C++ utility exercises the same packet serializer/parser and a deterministic software transport. It reports success/failure statistics, latency percentiles, end-to-end elapsed time, and effective packets/second. The output explicitly labels simulation versus real hardware and does not label software timing as FPGA hardware timing.

## Future checks

Before connecting a market-data engine, add directed tests for every packet type, randomized packet fields, CRC fault injection, FIFO boundaries, back-to-back CS behavior, reset during idle and transfer, and a bit-accurate comparison with the Python reference model. Hardware-in-the-loop tests should capture the actual Pi and FPGA configuration, clock rate, packet count, errors, and environment.
