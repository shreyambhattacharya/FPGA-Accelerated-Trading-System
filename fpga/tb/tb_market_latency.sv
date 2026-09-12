`timescale 1ns/1ps

`include "protocol_defs.svh"

// Measures the event-to-feature and event-to-candidate-record latency of the
// serialized market path for a warm-up quote, then a full-feature trade and
// quote with all five divisions active.  The signal consumer is always ready.
module tb_market_latency;
    localparam integer NUM_SYMBOLS = 1;
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
    wire [15:0] feature_symbol_id;
    wire [31:0] feature_sequence;
    wire [63:0] feature_vwap;
    wire signed [64:0] feature_midpoint_minus_vwap;
    wire signed [31:0] feature_midpoint_minus_vwap_bps_x100;
    wire signed [31:0] feature_spread_bps_x100;
    wire signed [31:0] feature_momentum_bps_x100;
    wire signed [15:0] feature_imbalance_normalized;
    wire [TRADE_ACC_W-1:0] feature_rolling_volume;
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
    wire signal_feature_ready;
    wire signal_valid;
    wire [15:0] signal_symbol_id;
    wire [31:0] signal_sequence;
    wire [1:0] signal_action;
    wire [3:0] signal_score;
    wire [15:0] signal_reason_bits;
    reg strategy_enable = 1'b0;
    reg long_enable = 1'b1;
    reg short_enable = 1'b1;
    reg signed [31:0] long_min_momentum_bps_x100 = 32'sd100;
    reg signed [31:0] short_max_momentum_bps_x100 = -32'sd100;
    reg signed [31:0] long_min_vwap_delta_bps_x100 = 32'sd100;
    reg signed [31:0] short_max_vwap_delta_bps_x100 = -32'sd100;
    reg signed [15:0] long_min_imbalance_q15 = 16'sd1638;
    reg signed [15:0] short_max_imbalance_q15 = -16'sd1638;
    reg signed [31:0] max_spread_bps_x100 = 32'sd500;
    reg [TRADE_ACC_W-1:0] min_rolling_volume = {TRADE_ACC_W{1'b1}};
    reg [31:0] signal_cooldown_events = 32'd4;
    reg [NUM_SYMBOLS-1:0] symbol_enable = {NUM_SYMBOLS{1'b1}};
    wire event_reject_pulse;
    wire [7:0] event_reject_reason;
    wire [15:0] event_reject_symbol_id;
    wire [31:0] event_reject_sequence;

    integer cycle_count = 0;
    integer latency_first_quote;
    integer latency_full_trade;
    integer latency_full_quote;
    integer signal_latency_first_quote;
    integer signal_latency_full_trade;
    integer signal_latency_full_quote;
    integer accept_cycle;
    integer feature_cycle;
    integer signal_cycle;
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
        .slot_reset_valid(1'b0), .slot_reset_symbol_id(16'd0),
        .slot_reset_ready(),
        .feature_valid(feature_valid), .feature_ready(signal_feature_ready),
        .feature_symbol_id(feature_symbol_id), .feature_sequence(feature_sequence),
        .feature_bid_price(), .feature_bid_quantity(),
        .feature_ask_price(), .feature_ask_quantity(),
        .feature_spread(), .feature_spread_valid(feature_spread_valid),
        .feature_midpoint(), .feature_midpoint_valid(),
        .feature_momentum(), .feature_momentum_valid(feature_momentum_valid),
        .feature_rolling_volume(feature_rolling_volume),
        .feature_imbalance_numerator(), .feature_imbalance_denominator(),
        .feature_imbalance_valid(feature_imbalance_valid),
        .feature_vwap_sum_price_quantity(), .feature_vwap_sum_quantity(),
        .feature_vwap_valid(), .feature_vwap(feature_vwap),
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
        .feature_midpoint_minus_vwap_bps_x100_valid(
            feature_midpoint_minus_vwap_bps_x100_valid),
        .history_trade_probe(), .history_midpoint_probe(),
        .history_vwap_price_quantity_probe(), .history_vwap_quantity_probe(),
        .event_reject_pulse(event_reject_pulse),
        .event_reject_reason(event_reject_reason),
        .event_reject_symbol_id(event_reject_symbol_id),
        .event_reject_sequence(event_reject_sequence)
    );

    signal_engine #(
        .NUM_SYMBOLS(1),
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
        .slot_reset_valid(1'b0), .slot_reset_symbol_id(16'd0),
        .slot_reset_ready(),
        .signal_valid(signal_valid), .signal_ready(1'b1),
        .signal_symbol_id(signal_symbol_id), .signal_sequence(signal_sequence),
        .signal_action(signal_action), .signal_score(signal_score),
        .signal_reason_bits(signal_reason_bits)
    );

    task automatic send_event;
        input [7:0] message_type;
        input [63:0] price;
        input [31:0] quantity;
        input [7:0] side;
        input [31:0] sequence_number;
        output integer feature_latency;
        output integer signal_latency;
        integer feature_seen;
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
            feature_seen = 0;
            @(posedge clk);
            #1;
            event_valid = 1'b0;

            begin : wait_for_feature
                forever begin
                    @(posedge clk);
                    #1;
                    if (!feature_seen && feature_valid) begin
                        if (sequence_number >= 18 &&
                            (!feature_vwap_quotient_valid ||
                             !feature_imbalance_normalized_valid ||
                             !feature_spread_bps_x100_valid ||
                             !feature_momentum_bps_x100_valid ||
                             !feature_midpoint_minus_vwap_valid ||
                             !feature_midpoint_minus_vwap_bps_x100_valid))
                            $fatal(1, "full-feature record was not fully valid");
                        feature_cycle = cycle_count;
                        feature_latency = feature_cycle - accept_cycle;
                        feature_seen = 1;
                    end
                    if (signal_valid) begin
                        if (!feature_seen || signal_symbol_id !== 16'd0 ||
                            signal_sequence !== sequence_number) begin
                            $fatal(1, "signal record was not aligned to feature");
                        end
                        signal_cycle = cycle_count;
                        signal_latency = signal_cycle - accept_cycle;
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
                   latency_first_quote, signal_latency_first_quote);
        send_event(`MSG_MARKET_QUOTE, 64'd102, 32'd20, 8'd1, 32'd2,
                   latency_first_quote, signal_latency_first_quote);

        // Fill the momentum history with safe quote observations.
        for (index = 0; index < 15; index = index + 1)
            send_event(`MSG_MARKET_QUOTE, 64'd100 + (index % 2), 32'd10, 8'd0,
                       index + 3, latency_first_quote, signal_latency_first_quote);

        // VWAP, imbalance, spread, momentum, and midpoint-minus-VWAP are all
        // valid for these two final records.
        send_event(`MSG_MARKET_TRADE, 64'd101, 32'd5, 8'd0, 32'd18,
                   latency_full_trade, signal_latency_full_trade);
        send_event(`MSG_MARKET_QUOTE, 64'd101, 32'd11, 8'd0, 32'd19,
                   latency_full_quote, signal_latency_full_quote);

        if (event_reject_pulse)
            $fatal(1, "latency test event was rejected");
        if (latency_full_trade <= latency_first_quote ||
            latency_full_quote <= latency_first_quote)
            $fatal(1, "full-feature latency did not include divider schedule");
        if (signal_latency_first_quote != latency_first_quote + 1 ||
            signal_latency_full_trade != latency_full_trade + 1 ||
            signal_latency_full_quote != latency_full_quote + 1)
            $fatal(1, "signal stage latency was not one clk27 cycle");

        $display("tb_market_latency: PASS warm_quote=%0d full_trade=%0d full_quote=%0d event_to_signal_warm=%0d event_to_signal_trade=%0d event_to_signal_quote=%0d feature_to_signal=1",
                 latency_first_quote, latency_full_trade, latency_full_quote,
                 signal_latency_first_quote, signal_latency_full_trade,
                 signal_latency_full_quote);
        $finish;
    end
endmodule
