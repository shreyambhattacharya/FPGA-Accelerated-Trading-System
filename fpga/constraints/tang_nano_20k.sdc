# Tang Nano 20K onboard clock: 27 MHz = 37.037 ns period.
create_clock -name clk27 -period 37.037 [get_ports {clk}]

# Initial physical target is 5 MHz SPI. This clock remains configurable in
# the host and must be re-measured for 10/20/30/40+ MHz hardware tests.
create_clock -name spi_clk -period 200.000 [get_ports {spi_clk}]

# The packet FIFOs are intentionally asynchronous CDC boundaries.
set_clock_groups -asynchronous \
    -group [get_clocks {clk27}] \
    -group [get_clocks {spi_clk}]
