# Build one NUM_SYMBOLS point of the Gowin resource/timing matrix.
# The PowerShell driver sets GOWIN_MATRIX_SYMBOLS to 4, 8, 16, or 32.
set repo C:/Users/shrey/OneDrive/Desktop/Projects/FPGA_Accelerated_Trading_System
if {[info exists ::env(GOWIN_MATRIX_SYMBOLS)]} {
    set symbol_count $::env(GOWIN_MATRIX_SYMBOLS)
} else {
    set symbol_count 4
}
if {($symbol_count != 4) && ($symbol_count != 8) && ($symbol_count != 16) && ($symbol_count != 32)} {
    error "GOWIN_MATRIX_SYMBOLS must be 4, 8, 16, or 32"
}

set validation_dir [file join $repo fpga gowin validation matrix $symbol_count]
set project_name fpga_trading_$symbol_count
create_project -force -name $project_name -dir $validation_dir -pn GW2AR-LV18QN88C8/I7 -device_version C

add_file [file join $repo fpga rtl reset power_on_reset.sv]
add_file [file join $repo fpga rtl fifo async_packet_fifo.sv]
add_file [file join $repo fpga rtl protocol protocol_pkg.sv]
add_file [file join $repo fpga rtl protocol crc8_engine.sv]
add_file [file join $repo fpga rtl protocol packet_dispatcher.sv]
add_file [file join $repo fpga rtl spi spi_slave.sv]
add_file [file join $repo fpga rtl protocol loopback_engine.sv]
add_file [file join $repo fpga rtl math unsigned_divider.sv]
add_file [file join $repo fpga rtl math feature_normalizer.sv]
add_file [file join $repo fpga rtl strategy strategy_config.sv]
add_file [file join $repo fpga rtl strategy signal_engine.sv]
add_file [file join $repo fpga rtl market market_state_engine.sv]
add_file [file join $repo fpga rtl top trading_spi_top.sv]
add_file [file join $repo fpga rtl top trading_spi_top_matrix.sv]
add_file [file join $repo fpga constraints tang_nano_20k.cst]
add_file [file join $repo fpga constraints tang_nano_20k.sdc]

set_option -top_module trading_spi_top_$symbol_count
set_option -verilog_std sysv2017
set_option -synthesis_tool gowinsynthesis
set_option -output_base_name $project_name
set_option -place_option 0
set_option -route_option 0
set_option -clock_route_order 0
set_option -correct_hold_violation 1
set_option -route_maxfan 23
set_option -global_freq 100.000

run all
