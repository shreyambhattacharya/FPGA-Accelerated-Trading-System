`timescale 1ns/1ps

`include "protocol_defs.svh"

module tb_strategy_config;
    localparam integer NUM_SYMBOLS = 32;
    localparam integer TRADE_ACC_W = 37;

    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg cfg_write_valid = 1'b0;
    wire cfg_write_ready;
    reg [7:0] cfg_subcommand = 8'd0;
    reg [15:0] cfg_symbol_id = 16'd0;
    reg [63:0] cfg_data64 = 64'd0;
    reg [31:0] cfg_data32 = 32'd0;
    reg [15:0] cfg_flags = 16'd0;
    wire [7:0] cfg_status;
    wire strategy_enable;
    wire long_enable;
    wire short_enable;
    wire signed [31:0] long_min_momentum_bps_x100;
    wire signed [31:0] short_max_momentum_bps_x100;
    wire signed [31:0] long_min_vwap_delta_bps_x100;
    wire signed [31:0] short_max_vwap_delta_bps_x100;
    wire signed [15:0] long_min_imbalance_q15;
    wire signed [15:0] short_max_imbalance_q15;
    wire signed [31:0] max_spread_bps_x100;
    wire [TRADE_ACC_W-1:0] min_rolling_volume;
    wire [31:0] signal_cooldown_events;
    wire [NUM_SYMBOLS-1:0] symbol_enable;
    wire slot_reset_valid;
    wire [15:0] slot_reset_symbol_id;
    reg slot_reset_ready = 1'b1;

    always #5 clk = ~clk;

    strategy_config #(
        .NUM_SYMBOLS(NUM_SYMBOLS), .TRADE_ACC_W(TRADE_ACC_W)
    ) dut (
        .clk(clk), .reset_n(reset_n),
        .cfg_write_valid(cfg_write_valid), .cfg_write_ready(cfg_write_ready),
        .cfg_subcommand(cfg_subcommand), .cfg_symbol_id(cfg_symbol_id),
        .cfg_data64(cfg_data64), .cfg_data32(cfg_data32), .cfg_flags(cfg_flags),
        .cfg_status(cfg_status),
        .strategy_enable(strategy_enable), .long_enable(long_enable),
        .short_enable(short_enable),
        .long_min_momentum_bps_x100(long_min_momentum_bps_x100),
        .short_max_momentum_bps_x100(short_max_momentum_bps_x100),
        .long_min_vwap_delta_bps_x100(long_min_vwap_delta_bps_x100),
        .short_max_vwap_delta_bps_x100(short_max_vwap_delta_bps_x100),
        .long_min_imbalance_q15(long_min_imbalance_q15),
        .short_max_imbalance_q15(short_max_imbalance_q15),
        .max_spread_bps_x100(max_spread_bps_x100),
        .min_rolling_volume(min_rolling_volume),
        .signal_cooldown_events(signal_cooldown_events),
        .symbol_enable(symbol_enable),
        .slot_reset_valid(slot_reset_valid),
        .slot_reset_symbol_id(slot_reset_symbol_id),
        .slot_reset_ready(slot_reset_ready)
    );

    task automatic write_config;
        input [7:0] subcommand;
        input [15:0] symbol_value;
        input [63:0] data64_value;
        input [31:0] data32_value;
        input [7:0] expected_status;
        begin
            @(negedge clk);
            while (!cfg_write_ready)
                @(negedge clk);
            cfg_subcommand = subcommand;
            cfg_symbol_id = symbol_value;
            cfg_data64 = data64_value;
            cfg_data32 = data32_value;
            #1;
            if (cfg_status !== expected_status)
                $fatal(1, "unexpected config status %02h/%02h", cfg_status, expected_status);
            cfg_write_valid = 1'b1;
            @(posedge clk);
            #1;
            cfg_write_valid = 1'b0;
            @(negedge clk);
        end
    endtask

    initial begin
        #12;
        reset_n = 1'b1;
        if (strategy_enable !== 1'b0 || !long_enable || !short_enable ||
            symbol_enable !== {NUM_SYMBOLS{1'b1}})
            $fatal(1, "development defaults are incorrect");

        write_config(`CONTROL_SET_GLOBAL_FLAGS, 16'd0, 64'd7, 32'd0, `STATUS_OK);
        if (!strategy_enable || !long_enable || !short_enable)
            $fatal(1, "global flags did not update");
        write_config(`CONTROL_SET_LONG_MOMENTUM, 16'd0, 64'd0, 32'hFFFFFF9C, `STATUS_OK);
        write_config(`CONTROL_SET_SHORT_MOMENTUM, 16'd0, 64'd0, 32'h00000064, `STATUS_OK);
        write_config(`CONTROL_SET_LONG_VWAP_DELTA, 16'd0, 64'd0, 32'd250, `STATUS_OK);
        write_config(`CONTROL_SET_SHORT_VWAP_DELTA, 16'd0, 64'd0, 32'hFFFFFF06, `STATUS_OK);
        write_config(`CONTROL_SET_LONG_IMBALANCE, 16'd0, 64'd0, 32'd2000, `STATUS_OK);
        write_config(`CONTROL_SET_SHORT_IMBALANCE, 16'd0, 64'd0, 32'hFFFFF830, `STATUS_OK);
        write_config(`CONTROL_SET_MAX_SPREAD, 16'd0, 64'd0, 32'd750, `STATUS_OK);
        write_config(`CONTROL_SET_MIN_VOLUME, 16'd0, 64'd1234, 32'd0, `STATUS_OK);
        write_config(`CONTROL_SET_COOLDOWN, 16'd0, 64'd0, 32'd9, `STATUS_OK);
        if (long_min_momentum_bps_x100 !== -32'sd100 ||
            short_max_momentum_bps_x100 !== 32'sd100 ||
            long_min_vwap_delta_bps_x100 !== 32'sd250 ||
            short_max_vwap_delta_bps_x100 !== -32'sd250 ||
            long_min_imbalance_q15 !== 16'sd2000 ||
            short_max_imbalance_q15 !== -16'sd2000 ||
            max_spread_bps_x100 !== 32'sd750 ||
            min_rolling_volume !== 37'd1234 || signal_cooldown_events !== 32'd9)
            $fatal(1, "threshold register update mismatch");

        write_config(`CONTROL_SET_SYMBOL_ENABLE, 16'd7, 64'd0, 32'd0, `STATUS_OK);
        if (symbol_enable[7] !== 1'b0)
            $fatal(1, "symbol disable did not update");
        write_config(`CONTROL_SET_SYMBOL_ENABLE, 16'd7, 64'd1, 32'd0, `STATUS_OK);
        if (symbol_enable[7] !== 1'b1)
            $fatal(1, "symbol enable did not update");

        // The reset command is accepted, held until the reset consumer is
        // ready, and does not permit a following write while pending.
        write_config(`CONTROL_CLEAR_SYMBOL_STATE, 16'd7, 64'd0, 32'd0, `STATUS_OK);
        if (!slot_reset_valid || slot_reset_symbol_id !== 16'd7 || cfg_write_ready)
            $fatal(1, "slot reset was not held as a coordinated request");
        @(posedge clk);
        #1;
        if (slot_reset_valid || !cfg_write_ready)
            $fatal(1, "slot reset did not complete");

        write_config(8'hFE, 16'd0, 64'd0, 32'd0, `STATUS_BAD_CONTROL);
        write_config(`CONTROL_SET_SYMBOL_ENABLE, 16'd99, 64'd0, 32'd0, `STATUS_BAD_CONTROL);

        $display("tb_strategy_config: PASS writes=15 slot_reset=1 invalid=2");
        $finish;
    end
endmodule
