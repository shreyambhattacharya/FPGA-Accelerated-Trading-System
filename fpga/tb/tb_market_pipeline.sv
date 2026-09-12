`timescale 1ns/1ps

`include "protocol_defs.svh"

// Packet-level dispatcher-to-engine replay bench. The Python differential
// runner supplies the same fixed 32-byte packets to the reference model and
// this testbench, then compares the machine-readable output records.
module tb_market_pipeline;
    localparam integer NUM_SYMBOLS = 4;
    localparam integer TRADE_WINDOW = 32;
    localparam integer MOMENTUM_WINDOW = 16;
    localparam integer VWAP_WINDOW = 32;
    localparam integer TRADE_ACC_W = 32 + ((TRADE_WINDOW <= 1) ? 1 : $clog2(TRADE_WINDOW));
    localparam integer VWAP_ACC_W = 96 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));
    localparam integer VWAP_QTY_ACC_W = 32 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));

    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg packet_valid = 1'b0;
    wire packet_ready;
    reg [255:0] packet_in = 256'd0;

    wire loopback_valid;
    wire [255:0] loopback_packet;
    wire loopback_override_valid;
    wire [7:0] loopback_override;
    wire event_valid;
    wire event_ready;
    wire [7:0] event_type;
    wire [15:0] event_symbol_id;
    wire [63:0] event_timestamp_ns;
    wire [63:0] event_price;
    wire [31:0] event_quantity;
    wire [7:0] event_side;
    wire [31:0] event_sequence;
    wire [15:0] event_flags;
    wire dispatch_error_pulse;
    wire [7:0] dispatch_error_reason;
    wire [15:0] dispatch_error_symbol_id;
    wire [31:0] dispatch_error_sequence;

    wire feature_valid;
    wire [15:0] feature_symbol_id;
    wire [31:0] feature_sequence;
    wire [63:0] feature_bid_price;
    wire [31:0] feature_bid_quantity;
    wire [63:0] feature_ask_price;
    wire [31:0] feature_ask_quantity;
    wire [63:0] feature_spread;
    wire feature_spread_valid;
    wire [63:0] feature_midpoint;
    wire feature_midpoint_valid;
    wire signed [64:0] feature_momentum;
    wire feature_momentum_valid;
    wire [TRADE_ACC_W-1:0] feature_rolling_volume;
    wire signed [32:0] feature_imbalance_numerator;
    wire [32:0] feature_imbalance_denominator;
    wire feature_imbalance_valid;
    wire [VWAP_ACC_W-1:0] feature_vwap_sum_price_quantity;
    wire [VWAP_QTY_ACC_W-1:0] feature_vwap_sum_quantity;
    wire feature_vwap_valid;
    wire [63:0] feature_vwap;
    wire feature_vwap_quotient_valid;
    wire signed [15:0] feature_imbalance_normalized;
    wire feature_imbalance_normalized_valid;
    wire signed [31:0] feature_spread_bps_x100;
    wire feature_spread_bps_x100_valid;
    wire signed [31:0] feature_momentum_bps_x100;
    wire feature_momentum_bps_x100_valid;
    wire signed [64:0] feature_midpoint_minus_vwap;
    wire feature_midpoint_minus_vwap_valid;
    wire signed [31:0] feature_midpoint_minus_vwap_bps_x100;
    wire feature_midpoint_minus_vwap_bps_x100_valid;
    wire event_reject_pulse;
    wire [7:0] event_reject_reason;
    wire [15:0] event_reject_symbol_id;
    wire [31:0] event_reject_sequence;

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
    reg cfg_write_valid = 1'b0;
    wire cfg_write_ready;
    reg [7:0] cfg_subcommand = 8'd0;
    reg [15:0] cfg_symbol_id = 16'd0;
    reg [63:0] cfg_data64 = 64'd0;
    reg [31:0] cfg_data32 = 32'd0;
    reg [15:0] cfg_flags = 16'd0;
    wire [7:0] cfg_status;
    wire slot_reset_valid;
    wire [15:0] slot_reset_symbol_id;
    wire market_slot_reset_ready;
    wire signal_slot_reset_ready;
    wire slot_reset_ready = market_slot_reset_ready && signal_slot_reset_ready;

    wire signal_feature_ready;
    wire signal_valid;
    wire [15:0] signal_symbol_id;
    wire [31:0] signal_sequence;
    wire [1:0] signal_action;
    wire [3:0] signal_score;
    wire [15:0] signal_reason_bits;

    reg [255:0] packet_memory [0:99999];
    integer event_count;
    integer event_index;
    reg [1023:0] event_file;

    always #5 clk = ~clk;

    strategy_config #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
        .TRADE_ACC_W(TRADE_ACC_W)
    ) config_dut (
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

    packet_dispatcher #(.NUM_SYMBOLS(NUM_SYMBOLS)) dispatcher (
        .clk(clk), .reset_n(reset_n),
        .packet_valid(packet_valid), .packet_ready(packet_ready), .packet_in(packet_in),
        .loopback_valid(loopback_valid), .loopback_ready(1'b1),
        .loopback_packet(loopback_packet),
        .loopback_status_override_valid(loopback_override_valid),
        .loopback_status_override(loopback_override),
        .event_valid(event_valid), .event_ready(event_ready),
        .event_type(event_type), .event_symbol_id(event_symbol_id),
        .event_timestamp_ns(event_timestamp_ns), .event_price(event_price),
        .event_quantity(event_quantity), .event_side(event_side),
        .event_sequence(event_sequence), .event_flags(event_flags),
        .control_valid(), .control_ready(1'b0),
        .control_subcommand(), .control_symbol_id(),
        .control_data64(), .control_data32(), .control_flags(),
        .control_status(8'h00),
        .dispatch_error_pulse(dispatch_error_pulse),
        .dispatch_error_reason(dispatch_error_reason),
        .dispatch_error_symbol_id(dispatch_error_symbol_id),
        .dispatch_error_sequence(dispatch_error_sequence)
    );

    market_state_engine #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
        .TRADE_WINDOW(TRADE_WINDOW),
        .MOMENTUM_WINDOW(MOMENTUM_WINDOW),
        .VWAP_WINDOW(VWAP_WINDOW)
    ) engine (
        .clk(clk), .reset_n(reset_n),
        .event_valid(event_valid), .event_ready(event_ready),
        .event_type(event_type), .event_symbol_id(event_symbol_id),
        .event_timestamp_ns(event_timestamp_ns), .event_price(event_price),
        .event_quantity(event_quantity), .event_side(event_side),
        .event_sequence(event_sequence), .event_flags(event_flags),
        .slot_reset_valid(slot_reset_valid), .slot_reset_symbol_id(slot_reset_symbol_id),
        .slot_reset_ready(market_slot_reset_ready),
        .feature_valid(feature_valid), .feature_ready(signal_feature_ready),
        .feature_symbol_id(feature_symbol_id), .feature_sequence(feature_sequence),
        .feature_bid_price(feature_bid_price), .feature_bid_quantity(feature_bid_quantity),
        .feature_ask_price(feature_ask_price), .feature_ask_quantity(feature_ask_quantity),
        .feature_spread(feature_spread), .feature_spread_valid(feature_spread_valid),
        .feature_midpoint(feature_midpoint), .feature_midpoint_valid(feature_midpoint_valid),
        .feature_momentum(feature_momentum), .feature_momentum_valid(feature_momentum_valid),
        .feature_rolling_volume(feature_rolling_volume),
        .feature_imbalance_numerator(feature_imbalance_numerator),
        .feature_imbalance_denominator(feature_imbalance_denominator),
        .feature_imbalance_valid(feature_imbalance_valid),
        .feature_vwap_sum_price_quantity(feature_vwap_sum_price_quantity),
        .feature_vwap_sum_quantity(feature_vwap_sum_quantity),
        .feature_vwap_valid(feature_vwap_valid),
        .feature_vwap(feature_vwap),
        .feature_vwap_quotient_valid(feature_vwap_quotient_valid),
        .feature_imbalance_normalized(feature_imbalance_normalized),
        .feature_imbalance_normalized_valid(feature_imbalance_normalized_valid),
        .feature_spread_bps_x100(feature_spread_bps_x100),
        .feature_spread_bps_x100_valid(feature_spread_bps_x100_valid),
        .feature_momentum_bps_x100(feature_momentum_bps_x100),
        .feature_momentum_bps_x100_valid(feature_momentum_bps_x100_valid),
        .feature_midpoint_minus_vwap(feature_midpoint_minus_vwap),
        .feature_midpoint_minus_vwap_valid(feature_midpoint_minus_vwap_valid),
        .feature_midpoint_minus_vwap_bps_x100(feature_midpoint_minus_vwap_bps_x100),
        .feature_midpoint_minus_vwap_bps_x100_valid(feature_midpoint_minus_vwap_bps_x100_valid),
        .event_reject_pulse(event_reject_pulse), .event_reject_reason(event_reject_reason),
        .event_reject_symbol_id(event_reject_symbol_id), .event_reject_sequence(event_reject_sequence)
    );

    signal_engine #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
        .TRADE_ACC_W(TRADE_ACC_W)
    ) signal_dut (
        .clk(clk), .reset_n(reset_n),
        .feature_valid(feature_valid), .feature_ready(signal_feature_ready),
        .feature_symbol_id(feature_symbol_id), .feature_sequence(feature_sequence),
        .feature_spread_bps_x100(feature_spread_bps_x100),
        .feature_spread_valid(feature_spread_bps_x100_valid),
        .feature_momentum_bps_x100(feature_momentum_bps_x100),
        .feature_momentum_valid(feature_momentum_bps_x100_valid),
        .feature_imbalance_q15(feature_imbalance_normalized),
        .feature_imbalance_valid(feature_imbalance_normalized_valid),
        .feature_vwap(feature_vwap), .feature_vwap_valid(feature_vwap_quotient_valid),
        .feature_midpoint_minus_vwap(feature_midpoint_minus_vwap),
        .feature_midpoint_minus_vwap_bps_x100(feature_midpoint_minus_vwap_bps_x100),
        .feature_vwap_delta_valid(feature_midpoint_minus_vwap_bps_x100_valid),
        .feature_rolling_volume(feature_rolling_volume),
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
        .slot_reset_ready(signal_slot_reset_ready),
        .signal_valid(signal_valid), .signal_ready(1'b1),
        .signal_symbol_id(signal_symbol_id), .signal_sequence(signal_sequence),
        .signal_action(signal_action), .signal_score(signal_score),
        .signal_reason_bits(signal_reason_bits)
    );

    always @(posedge clk) begin
        #1;
        if (dispatch_error_pulse)
            $display("DISPATCH_ERROR %02h %04h %08h", dispatch_error_reason,
                     dispatch_error_symbol_id, dispatch_error_sequence);
        if (event_reject_pulse)
            $display("EVENT_REJECT %02h %04h %08h", event_reject_reason,
                     event_reject_symbol_id, event_reject_sequence);
        if (feature_valid)
            $display("FEATURE %04h %08h %016h %08h %016h %08h %016h %01h %016h %01h %017h %01h %0h %09h %09h %01h %0h %0h %01h %016h %01h %04h %01h %08h %01h %08h %01h %017h %01h %08h %01h",
                     feature_symbol_id, feature_sequence,
                     feature_bid_price, feature_bid_quantity,
                     feature_ask_price, feature_ask_quantity,
                     feature_spread, feature_spread_valid,
                     feature_midpoint, feature_midpoint_valid,
                     feature_momentum, feature_momentum_valid,
                     feature_rolling_volume, feature_imbalance_numerator,
                     feature_imbalance_denominator, feature_imbalance_valid,
                     feature_vwap_sum_price_quantity, feature_vwap_sum_quantity,
                     feature_vwap_valid, feature_vwap, feature_vwap_quotient_valid,
                     feature_imbalance_normalized, feature_imbalance_normalized_valid,
                     feature_spread_bps_x100, feature_spread_bps_x100_valid,
                     feature_momentum_bps_x100, feature_momentum_bps_x100_valid,
                     feature_midpoint_minus_vwap, feature_midpoint_minus_vwap_valid,
                     feature_midpoint_minus_vwap_bps_x100,
                     feature_midpoint_minus_vwap_bps_x100_valid);
        if (signal_valid)
            $display("SIGNAL %04h %08h %01h %01h %04h",
                     signal_symbol_id, signal_sequence, signal_action,
                     signal_score, signal_reason_bits);
    end

    task automatic write_config;
        input [7:0] subcommand;
        input [63:0] data64_value;
        input [31:0] data32_value;
        begin
            @(negedge clk);
            while (!cfg_write_ready)
                @(negedge clk);
            cfg_subcommand = subcommand;
            cfg_data64 = data64_value;
            cfg_data32 = data32_value;
            cfg_write_valid = 1'b1;
            @(posedge clk);
            #1;
            if (cfg_status !== `STATUS_OK)
                $fatal(1, "configuration write rejected: %02h", cfg_status);
            cfg_write_valid = 1'b0;
        end
    endtask

    initial begin
        if (!$value$plusargs("EVENT_FILE=%s", event_file)) $fatal(1, "EVENT_FILE plusarg is required");
        if (!$value$plusargs("EVENT_COUNT=%d", event_count)) $fatal(1, "EVENT_COUNT plusarg is required");
        if (event_count > 100000) $fatal(1, "event count exceeds replay memory");
        $readmemh(event_file, packet_memory);

        #12;
        reset_n = 1'b1;
        // End-to-end differential configuration. These intentionally simple
        // TEST / DEVELOPMENT DEFAULTS make both directions observable in a
        // deterministic replay; they are not profitability parameters.
        write_config(`CONTROL_SET_GLOBAL_FLAGS, 64'd7, 32'd0);
        write_config(`CONTROL_SET_LONG_MOMENTUM, 64'd0, 32'd0);
        write_config(`CONTROL_SET_SHORT_MOMENTUM, 64'd0, 32'd0);
        write_config(`CONTROL_SET_LONG_VWAP_DELTA, 64'd0, 32'd0);
        write_config(`CONTROL_SET_SHORT_VWAP_DELTA, 64'd0, 32'd0);
        write_config(`CONTROL_SET_LONG_IMBALANCE, 64'd0, 32'd0);
        write_config(`CONTROL_SET_SHORT_IMBALANCE, 64'd0, 32'd0);
        write_config(`CONTROL_SET_MAX_SPREAD, 64'd0, 32'h7fffffff);
        write_config(`CONTROL_SET_MIN_VOLUME, 64'd0, 32'd0);
        write_config(`CONTROL_SET_COOLDOWN, 64'd0, 32'd3);
        for (event_index = 0; event_index < event_count; event_index = event_index + 1) begin
            while (!packet_ready) begin @(posedge clk); #1; end
            @(negedge clk);
            packet_in = packet_memory[event_index];
            packet_valid = 1'b1;
            @(posedge clk); #1;
            packet_valid = 1'b0;
            while (!(packet_ready && event_ready && !feature_valid)) begin @(posedge clk); #1; end
        end
        repeat (8) begin @(posedge clk); #1; end
        $display("DIFF_DONE");
        $finish;
    end
endmodule
