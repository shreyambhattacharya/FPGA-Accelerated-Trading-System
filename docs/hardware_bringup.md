# Tang Nano 20K hardware bring-up

This guide is the physical handoff for the first Raspberry Pi 5 ↔ Tang Nano 20K data-plane milestone. It covers the real board pinout used by the checked-in constraints, the Gowin project flow, safe wiring, and the Raspberry Pi test command. It does not claim that a bitstream has been downloaded or that a physical exchange has passed; those steps require the board and wiring.

## Board assumptions

The target is the Sipeed Tang Nano 20K using the Gowin `GW2AR-18C` device, part `GW2AR-LV18QN88C8/I7`. The board's onboard oscillator is 27 MHz and is used as the RTL system clock. The top-level reset is generated internally from that clock, so the Raspberry Pi does not need a reset GPIO.

Review the official [Tang Nano 20K hardware page](https://wiki.sipeed.com/hardware/en/tang/tang-nano-20k/nano-20k.html), especially its pin diagram, schematic, and datasheet links, before powering or wiring a board. The LED behavior follows Sipeed's [Tang Nano 20K LED example](https://wiki.sipeed.com/hardware/en/tang/tang-nano-20k/example/led.html): the user LEDs are active-low.

The CST in `fpga/constraints/tang_nano_20k.cst` uses the following intended mapping:

| Signal | Tang Nano FPGA pin | Function |
| --- | ---: | --- |
| `clk` | 4 | onboard 27 MHz oscillator |
| `spi_mosi` | 27 | Pi MOSI input |
| `spi_miso` | 28 | Pi MISO output |
| `spi_clk` | 25 | Pi SCLK input |
| `spi_cs_n` | 26 | Pi CE0 input, pulled high |
| `led[0]` … `led[5]` | 15 … 20 | active-low board LEDs |

The external SPI mapping is the wiring contract for this project and must be cross-checked against the board diagram during assembly. Keep the LCD/FPC connector disconnected; these pins are being used as the external SPI interface for this milestone.

## Build the FPGA project

1. Install the Gowin IDE/toolchain appropriate for the Tang Nano 20K and connect the board through its normal USB-C programming interface.
2. Open `fpga/gowin/fpga_trading.gprj`.
3. Confirm the project device is `GW2AR-18C` / `GW2AR-LV18QN88C8/I7`, the top module is `trading_spi_top`, and the source list includes the reset, async FIFO, SPI, `crc8_engine`, `packet_dispatcher`, `market_state_engine`, loopback, and top-level RTL files.
4. Set the HDL language option to **SystemVerilog 2017** (`sysv2017` / `sysv-2017`, depending on the IDE version). The checked-in `fpga/gowin/configure_project.tcl` contains the explicit `set_option -verilog_std sysv2017` setting; source it in the Gowin Tcl console or configure the same option in the project GUI. The `.gprj` remains in the vendor's portable project format.
5. Confirm the project includes `fpga/constraints/tang_nano_20k.cst` and `fpga/constraints/tang_nano_20k.sdc`.
6. Run synthesis and inspect the synthesis log. Do not proceed to place-and-route or program hardware while EX3209, EX2213, AG0100, or AG0101 warnings remain.
7. After synthesis is clean, run place-and-route/bitstream generation. Resolve any IDE-version-specific constraint or primitive warning before programming.
8. Download the generated bitstream to the board for a volatile test, or program the board's configuration flash using the normal Gowin/Sipeed flow if persistence is desired.

The checked-in RTL was also exercised with GowinSynthesis and place-and-route
when that vendor tool was available in the development environment. The CRC
engine is a byte-per-system-clock datapath. Gowin logs the four market history
arrays as RAM extraction candidates, but the final synthesis report maps
`BSRAM 0/46`; the 124 P&R RAM16 resources are the existing RX/TX FIFO storage.
Synthesis/P&R success is not evidence of physical Pi↔FPGA validation.

For a reproducible command-line implementation run, use the checked-in
`fpga/scripts/run_gowin_pnr.tcl` with Gowin's `gw_sh.exe`. The local default
run used Gowin V1.9.11.03 Education and the target part above. The full
parameter matrix and its measured resource/timing results are recorded below.

### Gowin `NUM_SYMBOLS` matrix

All points use the same 27 MHz `clk27`, 5 MHz `spi_clk`, constraints, and
physical pins. `SSRAM(RAM16)` is the vendor report's block-memory resource;
the implementation used no DSP blocks.

| `NUM_SYMBOLS` | LUT | FF | RAM16/BRAM blocks | DSP | `clk27` Fmax | setup slack | hold slack | TNS setup/hold | critical `clk27` path |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 4 | 1,609 | 2,308 | 124 | 0 | 68.327 MHz | 22.402 ns | 0.313 ns | 0 / 0 ns | RX FIFO pointer → dispatcher packet register CE |
| 8 | 1,671 | 2,445 | 124 | 0 | 72.627 MHz | 23.268 ns | 0.313 ns | 0 / 0 ns | RX FIFO pointer → dispatcher packet register CE |
| 16 | 1,830 | 2,718 | 124 | 0 | 82.566 MHz | 24.926 ns | 0.313 ns | 0 / 0 ns | loopback byte index → CRC register D |
| 32 | 2,156 | 3,263 | 124 | 0 | 75.516 MHz | 23.795 ns | 0.313 ns | 0 / 0 ns | loopback byte index → CRC result D |

The new 32-symbol point closes at 75.516 MHz, comfortably above both the
40 MHz minimum and the preferred 50 MHz analysis goal. The old symbol/state
write cone is no longer the top-level critical path; N32 is now limited by an
existing loopback/CRC path. PR1014 remains present for `clk_d` and `spi_clk_d`
at every point. The complete before/after matrix, including old `spi_clk`
Fmax and logic levels, is in `docs/benchmarking.md`.

## Clock routing and PR1014

The baseline Tang Nano 20K place-and-route report identified the N32 `clk27`
path as the packet-dispatcher symbol register driving the market engine's
per-symbol sequence-register clock enable. The final N32 path is instead in
the existing loopback/CRC logic. The report also emits PR1014 for the routed
`clk_d` and `spi_clk_d` nets. The report places the onboard `clk` input on pin
4 with the device's `LPLL1_T_in` capability and shows the primary clock
resource in use; the external `spi_clk` input is pin 25 and is not a dedicated
clock input in this package. The current constraints preserve these physical
mappings and do not force a speculative pin change or unsafe clock constraint.
Recheck PR1014 on the final P&R run: it is a clock-input/topology warning, not
evidence that the CRC FSM is combinationally unsafe. The permanent CDC
constraint is the one-line clock-group exception in
`fpga/constraints/tang_nano_20k.sdc`:

```tcl
set_clock_groups -asynchronous -group [get_clocks {clk27}] -group [get_clocks {spi_clk}]
```

Do not program hardware while timing or control-loop warnings remain unresolved. In particular, PR1014 should be documented with the generated clock report and device pin capabilities rather than suppressed by a warning filter.

## Synthesis-warning troubleshooting

- **EX3209** means the HDL was parsed as Verilog 2001 or a helper function was left at compilation-unit scope. Use SystemVerilog 2017 and keep shared helpers in `protocol_pkg.sv`.
- **EX2213** means a persistent state transition is being performed from an asynchronous set/reset branch. CS may only clear the per-frame shift/count registers; protocol phase transitions must occur on rising `spi_clk`.
- **AG0100 / AG0101** means the synthesized control path contains a logical feedback loop or non-DAG netlist. Do not program the board until the underlying RTL is corrected and the warnings disappear; warning filters are not an acceptable fix.

## Safe wiring sequence

Power both devices off before wiring. Use short, direct 3.3 V-compatible jumper wires and connect a common ground first.

| Raspberry Pi 5 physical pin | Pi signal | Tang Nano FPGA signal/pin | Direction |
| ---: | --- | --- | --- |
| 19 | GPIO10 / MOSI | `spi_mosi` / pin 27 | Pi → Tang |
| 21 | GPIO9 / MISO | `spi_miso` / pin 28 | Tang → Pi |
| 23 | GPIO11 / SCLK | `spi_clk` / pin 25 | Pi → Tang |
| 24 | GPIO8 / CE0 | `spi_cs_n` / pin 26 | Pi → Tang |
| any Pi GND | ground | Tang GND | reference |

Use the Pi's normal 3.3 V GPIO SPI levels. Power the Pi and Tang Nano from their normal supplies. Do not connect Pi 5 V, and do not use the Pi 3.3 V rail to power the USB-powered Tang Nano. Do not connect a reset wire: the FPGA's 27 MHz `power_on_reset` block holds logic in reset during startup.

Before the first transaction, inspect the wiring for swapped MOSI/MISO, a missing ground, shorts to 5 V, and an accidentally connected LCD/FPC cable. Keep the initial SPI speed at 5 MHz and mode 0.

## Raspberry Pi setup and test

On Raspberry Pi OS:

```bash
sudo raspi-config
# Interface Options -> SPI -> Enable
sudo reboot
ls -l /dev/spidev*
```

Build the host utility on the Pi:

```bash
cmake -S host -B host/build -DCMAKE_BUILD_TYPE=Release
cmake --build host/build
```

Wait for FPGA configuration to complete after power-up, then run a small test first:

```bash
./host/build/loopback_test --device /dev/spidev0.0 --speed 5000000 --count 10 --verbose
```

Then run the longer smoke/throughput test:

```bash
./host/build/loopback_test --device /dev/spidev0.0 --speed 5000000 --count 1000
```

Expected output must begin with `mode=REAL_HARDWARE`, show `failures=0`, and show `sequence_errors=0`. The reported `effective_packets_per_second` is the end-to-end host exchange rate for three 32-byte SPI transfers per packet. It includes Linux scheduling, spidev overhead, wire time, the turnaround transfer, and FPGA buffering; it is not an isolated FPGA service-rate measurement.

If the result reports a missing response, stop and check CS polarity, MOSI/MISO direction, common ground, board power, bitstream configuration, and mode 0 before increasing the clock. A PC run with `--sim` is useful for validating the host protocol but must remain labeled `mode=SIMULATION` and must not be used as a hardware result.

## LED and debug expectations

- LED0: active-low heartbeat, toggling at approximately 1 Hz.
- LED1: active-low activity indication held briefly after a received packet frame.
- LED2: active-low sticky protocol/CRC error indication.
- LED3–LED5: off in this milestone.

The internal counters for FIFO overflow/underflow and packet errors are retained for simulation and future debug instrumentation. Incomplete frames are discarded by the per-frame reset and are not exposed as a separate asynchronous diagnostic wire.

## Physical validation record

For each real hardware run, record:

- Tang Nano board revision and Gowin IDE/tool version;
- exact bitstream source commit and whether it was volatile or flash-programmed;
- Raspberry Pi model, OS/kernel, host compiler, and `/dev/spidev*` device;
- SPI mode, speed, packet count, failures, sequence errors, and effective throughput;
- wiring/power arrangement and whether a logic analyzer was used.

Only a run with those observations may be reported as a hardware result.
