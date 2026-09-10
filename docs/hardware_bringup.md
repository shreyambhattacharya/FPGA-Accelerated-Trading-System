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
3. Confirm the project device is `GW2AR-18C` / `GW2AR-LV18QN88C8/I7`, the top module is `trading_spi_top`, and the source list includes the reset, async FIFO, SPI, protocol, and top-level RTL files.
4. Confirm the project includes `fpga/constraints/tang_nano_20k.cst` and `fpga/constraints/tang_nano_20k.sdc`.
5. Run synthesis, then place-and-route/bitstream generation. Resolve any IDE version-specific constraint or primitive warning before programming.
6. Download the generated bitstream to the board for a volatile test, or program the board's configuration flash using the normal Gowin/Sipeed flow if persistence is desired.

This repository does not include a Gowin compiler invocation because the vendor toolchain is not installed in the development environment used to prepare the RTL. A successful Icarus simulation is not evidence of place-and-route or physical timing closure.

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

The internal counters for FIFO overflow/underflow, incomplete frames, and packet errors are retained for simulation and future debug instrumentation. They are not exposed as extra Pi wires in this milestone.

## Physical validation record

For each real hardware run, record:

- Tang Nano board revision and Gowin IDE/tool version;
- exact bitstream source commit and whether it was volatile or flash-programmed;
- Raspberry Pi model, OS/kernel, host compiler, and `/dev/spidev*` device;
- SPI mode, speed, packet count, failures, sequence errors, and effective throughput;
- wiring/power arrangement and whether a logic analyzer was used.

Only a run with those observations may be reported as a hardware result.
