`timescale 1ns/1ps

`include "protocol_defs.svh"

// Small elaboration/smoke test used for NUM_SYMBOLS synthesis variants.
module tb_market_parameter #(
    parameter integer NUM_SYMBOLS = 4
);
    localparam integer TRADE_WINDOW = 32;
    localparam integer MOMENTUM_WINDOW = 16;
    localparam integer VWAP_WINDOW = 32;
    localparam integer TRADE_ACC_W = 32 + ((TRADE_WINDOW <= 1) ? 1 : $clog2(TRADE_WINDOW));
    localparam integer VWAP_ACC_W = 96 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));
    localparam integer VWAP_QTY_ACC_W = 32 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));

    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg event_valid = 1'b0;
    wire event_ready;
    reg [7:0] event_type = `MSG_MARKET_QUOTE;
    reg [15:0] event_symbol_id = 16'd0;
    reg [63:0] event_timestamp_ns = 64'd1;
    reg [63:0] event_price = 64'd100;
    reg [31:0] event_quantity = 32'd1;
    reg [7:0] event_side = 8'd0;
    reg [31:0] event_sequence = 32'd1;
    reg [15:0] event_flags = 16'd0;
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
    wire event_reject_pulse;
    wire [7:0] event_reject_reason;
    wire [15:0] event_reject_symbol_id;
    wire [31:0] event_reject_sequence;

    always #5 clk = ~clk;

    market_state_engine #(.NUM_SYMBOLS(NUM_SYMBOLS)) dut (
        .clk(clk), .reset_n(reset_n),
        .event_valid(event_valid), .event_ready(event_ready),
        .event_type(event_type), .event_symbol_id(event_symbol_id),
        .event_timestamp_ns(event_timestamp_ns), .event_price(event_price),
        .event_quantity(event_quantity), .event_side(event_side),
        .event_sequence(event_sequence), .event_flags(event_flags),
        .feature_valid(feature_valid), .feature_ready(1'b1),
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
        .event_reject_pulse(event_reject_pulse), .event_reject_reason(event_reject_reason),
        .event_reject_symbol_id(event_reject_symbol_id), .event_reject_sequence(event_reject_sequence)
    );

    initial begin
        #12;
        reset_n = 1'b1;
        @(negedge clk);
        event_valid = 1'b1;
        @(posedge clk); #1;
        event_valid = 1'b0;
        wait (feature_valid === 1'b1);
        if (feature_symbol_id !== 16'd0 || feature_sequence !== 32'd1 ||
            feature_bid_price !== 64'd100)
            $fatal(1, "parameterized engine smoke mismatch NUM_SYMBOLS=%0d", NUM_SYMBOLS);
        @(posedge clk); #1;
        $display("tb_market_parameter NUM_SYMBOLS=%0d: PASS", NUM_SYMBOLS);
        $finish;
    end
endmodule
