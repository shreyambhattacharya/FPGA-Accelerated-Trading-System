`timescale 1ns/1ps

`include "protocol_defs.svh"

// Measures the event-to-feature latency of the serialized market path for a
// warm-up quote, then a full-feature trade and quote with all five divisions
// active.  The test deliberately keeps feature_ready asserted.
module tb_market_latency;
    localparam integer TRADE_WINDOW = 32;
    localparam integer MOMENTUM_WINDOW = 16;
    localparam integer VWAP_WINDOW = 32;
    localparam integer TRADE_ACC_W = 32 + $clog2(TRADE_WINDOW);
    localparam integer VWAP_ACC_W = 96 + $clog2(VWAP_WINDOW);
    localparam integer VWAP_QTY_ACC_W = 32 + $clog2(VWAP_WINDOW);

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
    wire feature_vwap_quotient_valid;
    wire feature_spread_valid;
    wire feature_midpoint_valid;
    wire feature_momentum_valid;
    wire feature_imbalance_valid;
    wire feature_imbalance_normalized_valid;
    wire feature_spread_bps_x100_valid;
    wire feature_momentum_bps_x100_valid;
    wire feature_midpoint_minus_vwap_valid;
    wire feature_midpoint_minus_vwap_bps_x100_valid;
    wire event_reject_pulse;
    wire [7:0] event_reject_reason;
    wire [15:0] event_reject_symbol_id;
    wire [31:0] event_reject_sequence;

    integer cycle_count = 0;
    integer latency_first_quote;
    integer latency_full_trade;
    integer latency_full_quote;
    integer accept_cycle;
    integer feature_cycle;
    integer index;

    always #5 clk = ~clk;
    always @(posedge clk) cycle_count = cycle_count + 1;

    market_state_engine #(
        .NUM_SYMBOLS(1),
        .TRADE_WINDOW(TRADE_WINDOW),
        .MOMENTUM_WINDOW(MOMENTUM_WINDOW),
        .VWAP_WINDOW(VWAP_WINDOW)
    ) dut (
        .clk(clk), .reset_n(reset_n),
        .event_valid(event_valid), .event_ready(event_ready),
        .event_type(event_type), .event_symbol_id(event_symbol_id),
        .event_timestamp_ns(event_timestamp_ns), .event_price(event_price),
        .event_quantity(event_quantity), .event_side(event_side),
        .event_sequence(event_sequence), .event_flags(event_flags),
        .feature_valid(feature_valid), .feature_ready(1'b1),
        .feature_spread_valid(feature_spread_valid),
        .feature_midpoint_valid(feature_midpoint_valid),
        .feature_momentum_valid(feature_momentum_valid),
        .feature_imbalance_valid(feature_imbalance_valid),
        .feature_vwap_quotient_valid(feature_vwap_quotient_valid),
        .feature_imbalance_normalized_valid(feature_imbalance_normalized_valid),
        .feature_spread_bps_x100_valid(feature_spread_bps_x100_valid),
        .feature_momentum_bps_x100_valid(feature_momentum_bps_x100_valid),
        .feature_midpoint_minus_vwap_valid(feature_midpoint_minus_vwap_valid),
        .feature_midpoint_minus_vwap_bps_x100_valid(
            feature_midpoint_minus_vwap_bps_x100_valid),
        .event_reject_pulse(event_reject_pulse),
        .event_reject_reason(event_reject_reason),
        .event_reject_symbol_id(event_reject_symbol_id),
        .event_reject_sequence(event_reject_sequence)
    );

    task automatic send_event;
        input [7:0] message_type;
        input [63:0] price;
        input [31:0] quantity;
        input [7:0] side;
        input [31:0] sequence_number;
        output integer latency;
        begin
            @(negedge clk);
            while (!event_ready)
                @(negedge clk);
            event_type = message_type;
            event_price = price;
            event_quantity = quantity;
            event_side = side;
            event_sequence = sequence_number;
            event_valid = 1'b1;
            accept_cycle = cycle_count + 1;
            @(posedge clk);
            #1;
            event_valid = 1'b0;

            begin : wait_for_feature
                forever begin
                    @(posedge clk);
                    #1;
                    if (feature_valid) begin
                        if (sequence_number >= 18 &&
                            (!feature_vwap_quotient_valid ||
                             !feature_imbalance_normalized_valid ||
                             !feature_spread_bps_x100_valid ||
                             !feature_momentum_bps_x100_valid ||
                             !feature_midpoint_minus_vwap_valid ||
                             !feature_midpoint_minus_vwap_bps_x100_valid))
                            $fatal(1, "full-feature record was not fully valid");
                        feature_cycle = cycle_count;
                        latency = feature_cycle - accept_cycle;
                        disable wait_for_feature;
                    end
                end
            end
        end
    endtask

    initial begin
        #12;
        reset_n = 1'b1;

        // A two-sided quote establishes spread/imbalance validity.
        send_event(`MSG_MARKET_QUOTE, 64'd100, 32'd10, 8'd0, 32'd1,
                   latency_first_quote);
        send_event(`MSG_MARKET_QUOTE, 64'd102, 32'd20, 8'd1, 32'd2,
                   latency_first_quote);

        // Fill the momentum history with safe quote observations.
        for (index = 0; index < 15; index = index + 1)
            send_event(`MSG_MARKET_QUOTE, 64'd100 + (index % 2), 32'd10, 8'd0,
                       index + 3, latency_first_quote);

        // VWAP, imbalance, spread, momentum, and midpoint-minus-VWAP are all
        // valid for these two final records.
        send_event(`MSG_MARKET_TRADE, 64'd101, 32'd5, 8'd0, 32'd18,
                   latency_full_trade);
        send_event(`MSG_MARKET_QUOTE, 64'd101, 32'd11, 8'd0, 32'd19,
                   latency_full_quote);

        if (event_reject_pulse)
            $fatal(1, "latency test event was rejected");
        if (latency_full_trade <= latency_first_quote ||
            latency_full_quote <= latency_first_quote)
            $fatal(1, "full-feature latency did not include divider schedule");

        $display("tb_market_latency: PASS warm_quote=%0d full_trade=%0d full_quote=%0d",
                 latency_first_quote, latency_full_trade, latency_full_quote);
        $finish;
    end
endmodule
