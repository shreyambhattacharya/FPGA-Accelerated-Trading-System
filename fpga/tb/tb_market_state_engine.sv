`timescale 1ns/1ps

`include "protocol_defs.svh"

module tb_market_state_engine;
    localparam integer NUM_SYMBOLS = 4;
    localparam integer TRADE_WINDOW = 4;
    localparam integer MOMENTUM_WINDOW = 3;
    localparam integer VWAP_WINDOW = 4;
    localparam integer TRADE_ACC_W = 32 + ((TRADE_WINDOW <= 1) ? 1 : $clog2(TRADE_WINDOW));
    localparam integer VWAP_ACC_W = 96 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));
    localparam integer VWAP_QTY_ACC_W = 32 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));

    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg event_valid = 1'b0;
    wire event_ready;
    reg [7:0] event_type = 8'd0;
    reg [15:0] event_symbol_id = 16'd0;
    reg [63:0] event_timestamp_ns = 64'd0;
    reg [63:0] event_price = 64'd0;
    reg [31:0] event_quantity = 32'd0;
    reg [7:0] event_side = 8'd0;
    reg [31:0] event_sequence = 32'd0;
    reg [15:0] event_flags = 16'd0;
    wire feature_valid;
    reg feature_ready = 1'b1;
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

    always #5 clk = ~clk;

    market_state_engine #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
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
        .feature_valid(feature_valid), .feature_ready(feature_ready),
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

    task automatic send_event;
        input [7:0] message_type;
        input [15:0] symbol_id;
        input [63:0] price;
        input [31:0] quantity;
        input [7:0] side;
        input [31:0] sequence_value;
        begin
            while (!event_ready) begin @(posedge clk); #1; end
            @(negedge clk);
            event_type = message_type;
            event_symbol_id = symbol_id;
            event_timestamp_ns = {32'd0, sequence_value};
            event_price = price;
            event_quantity = quantity;
            event_side = side;
            event_sequence = sequence_value;
            event_flags = 16'h55AA;
            event_valid = 1'b1;
            @(posedge clk); #1;
            event_valid = 1'b0;
            while (!(feature_valid || event_reject_pulse)) begin @(posedge clk); #1; end
        end
    endtask

    task automatic consume_feature;
        begin @(posedge clk); #1; end
    endtask

    task automatic expect_reject;
        input [7:0] expected_reason;
        input [15:0] expected_symbol;
        input [31:0] expected_sequence;
        begin
            if (!event_reject_pulse) $fatal(1, "expected rejection pulse");
            if (event_reject_reason !== expected_reason) $fatal(1, "wrong rejection reason");
            if (event_reject_symbol_id !== expected_symbol) $fatal(1, "wrong rejection symbol");
            if (event_reject_sequence !== expected_sequence) $fatal(1, "wrong rejection sequence");
            consume_feature();
        end
    endtask

    initial begin
        #20;
        reset_n = 1'b1;
        #10;

        // First bid: no two-sided quote features yet.
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd100, 32'd10, 8'd0, 32'd1);
        if (feature_sequence !== 32'd1 || feature_bid_price !== 64'd100 ||
            feature_bid_quantity !== 32'd10 || feature_spread_valid ||
            feature_midpoint_valid || feature_imbalance_valid ||
            feature_vwap_quotient_valid || feature_imbalance_normalized_valid ||
            feature_spread_bps_x100_valid || feature_momentum_bps_x100_valid ||
            feature_midpoint_minus_vwap_valid || feature_midpoint_minus_vwap_bps_x100_valid)
            $fatal(1, "first bid feature mismatch");
        consume_feature();

        // First ask: safe spread, midpoint, signed imbalance, no trade/VWAP.
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd102, 32'd20, 8'd1, 32'd2);
        if (!feature_spread_valid || feature_spread !== 64'd2 ||
            feature_midpoint !== 64'd101 || feature_imbalance_numerator !== -33'sd10 ||
            feature_imbalance_denominator !== 33'd30 || feature_rolling_volume !== 0 ||
            feature_vwap_valid || feature_spread_bps_x100 !== 32'sd19801 ||
            !feature_spread_bps_x100_valid ||
            feature_imbalance_normalized !== -16'sd10922 ||
            !feature_imbalance_normalized_valid || feature_vwap_quotient_valid)
            $fatal(1, "two-sided quote feature mismatch");
        consume_feature();

        // Trade updates rolling volume/VWAP but cannot overwrite quotes.
        send_event(`MSG_MARKET_TRADE, 16'd0, 64'd101, 32'd5, 8'd0, 32'd3);
        if (feature_bid_price !== 64'd100 || feature_ask_price !== 64'd102 ||
            feature_rolling_volume !== 38'd5 ||
            feature_vwap_sum_price_quantity !== 102'd505 ||
            feature_vwap_sum_quantity !== 38'd5 || !feature_vwap_valid ||
            feature_vwap !== 64'd101 || !feature_vwap_quotient_valid ||
            !feature_midpoint_minus_vwap_valid || feature_midpoint_minus_vwap !== 65'sd0 ||
            !feature_midpoint_minus_vwap_bps_x100_valid ||
            feature_midpoint_minus_vwap_bps_x100 !== 32'sd0)
            $fatal(1, "trade isolation or accumulator mismatch");
        consume_feature();

        // Bid-side quote changes only the bid and produces positive imbalance.
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd99, 32'd30, 8'd0, 32'd4);
        if (feature_ask_price !== 64'd102 || feature_spread !== 64'd3 ||
            feature_midpoint !== 64'd100 || feature_imbalance_numerator !== 33'sd10 ||
            feature_imbalance_denominator !== 33'd50 ||
            feature_spread_bps_x100 !== 32'sd30000 ||
            feature_imbalance_normalized !== 16'sd6553 ||
            feature_vwap !== 64'd101 || feature_midpoint_minus_vwap !== -65'sd1 ||
            feature_midpoint_minus_vwap_bps_x100 !== -32'sd9900)
            $fatal(1, "quote side isolation mismatch");
        consume_feature();

        // Crossed quote is explicitly invalid and must not append momentum.
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd98, 32'd40, 8'd1, 32'd5);
        if (feature_spread_valid || feature_midpoint_valid || feature_momentum_valid ||
            feature_imbalance_valid)
            $fatal(1, "crossed quote was treated as valid");
        consume_feature();

        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd103, 32'd40, 8'd1, 32'd6);
        if (!feature_spread_valid || feature_spread !== 64'd4 ||
            feature_midpoint !== 64'd101 || !feature_momentum_valid ||
            feature_momentum !== 65'sd0)
            $fatal(1, "repaired quote feature mismatch");
        consume_feature();

        // Fill then wrap the four-entry trade and VWAP rings.
        send_event(`MSG_MARKET_TRADE, 16'd0, 64'd101, 32'd1, 8'd0, 32'd7); consume_feature();
        send_event(`MSG_MARKET_TRADE, 16'd0, 64'd101, 32'd2, 8'd1, 32'd8); consume_feature();
        send_event(`MSG_MARKET_TRADE, 16'd0, 64'd101, 32'd3, 8'd0, 32'd9); consume_feature();
        send_event(`MSG_MARKET_TRADE, 16'd0, 64'd101, 32'd4, 8'd1, 32'd10);
        if (feature_rolling_volume !== 38'd10 || feature_vwap_sum_quantity !== 38'd10 ||
            feature_vwap_sum_price_quantity !== 102'd1010)
            $fatal(1, "window fill mismatch");
        consume_feature();
        send_event(`MSG_MARKET_TRADE, 16'd0, 64'd101, 32'd5, 8'd0, 32'd11);
        if (feature_rolling_volume !== 38'd14 || feature_vwap_sum_quantity !== 38'd14 ||
            feature_vwap_sum_price_quantity !== 102'd1414)
            $fatal(1, "window replacement mismatch");
        consume_feature();

        // Momentum becomes valid only after three valid midpoint observations.
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd97, 32'd30, 8'd0, 32'd12); consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd104, 32'd40, 8'd1, 32'd13); consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd98, 32'd30, 8'd0, 32'd14);
        if (!feature_momentum_valid || feature_momentum !== 65'sd0)
            $fatal(1, "momentum warm-up or signed delta mismatch");
        consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd106, 32'd40, 8'd1, 32'd15);
        if (!feature_momentum_valid || feature_momentum !== 65'sd2)
            $fatal(1, "momentum ring replacement mismatch");
        if (!feature_momentum_bps_x100_valid || feature_momentum_bps_x100 !== 32'sd20000 ||
            feature_midpoint_minus_vwap !== 65'sd1 ||
            feature_midpoint_minus_vwap_bps_x100 !== 32'sd9900)
            $fatal(1, "positive normalized momentum or VWAP delta mismatch");
        consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd90, 32'd40, 8'd0, 32'd16);
        if (!feature_momentum_valid || feature_momentum !== -65'sd2 ||
            !feature_momentum_bps_x100_valid || feature_momentum_bps_x100 !== -32'sd20000 ||
            feature_midpoint_minus_vwap !== -65'sd3 ||
            feature_midpoint_minus_vwap_bps_x100 !== -32'sd29702)
            $fatal(1, "negative normalized momentum or VWAP delta mismatch");
        consume_feature();

        // Symbol isolation and the first event sequence rule.
        send_event(`MSG_MARKET_QUOTE, 16'd1, 64'd200, 32'd1, 8'd0, 32'd100);
        if (feature_symbol_id !== 16'd1 || feature_bid_price !== 64'd200) $fatal(1, "symbol 1 state mismatch");
        consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd1, 64'd202, 32'd2, 8'd1, 32'd101);
        if (feature_symbol_id !== 16'd1 || feature_midpoint !== 64'd201) $fatal(1, "symbol 1 quote mismatch");
        consume_feature();

        send_event(`MSG_MARKET_QUOTE, 16'd1, 64'd201, 32'd3, 8'd2, 32'd102);
        expect_reject(`STATUS_BAD_SIDE, 16'd1, 32'd102);
        send_event(`MSG_MARKET_QUOTE, 16'd1, 64'd201, 32'd3, 8'd0, 32'd101);
        expect_reject(`STATUS_DUPLICATE_SEQ, 16'd1, 32'd101);
        send_event(`MSG_MARKET_QUOTE, 16'd1, 64'd201, 32'd3, 8'd0, 32'd99);
        expect_reject(`STATUS_STALE_SEQ, 16'd1, 32'd99);
        send_event(`MSG_MARKET_QUOTE, 16'hFFFF, 64'd1, 32'd1, 8'd0, 32'd1);
        expect_reject(`STATUS_BAD_SYMBOL, 16'hFFFF, 32'd1);

        // Weighted VWAP and equal midpoint/VWAP delta on an independent bank.
        send_event(`MSG_MARKET_TRADE, 16'd3, 64'd100, 32'd2, 8'd0, 32'd1);
        if (feature_vwap !== 64'd100 || feature_vwap_sum_price_quantity !== 102'd200 ||
            feature_vwap_sum_quantity !== 38'd2 || !feature_vwap_quotient_valid)
            $fatal(1, "first weighted VWAP mismatch");
        consume_feature();
        send_event(`MSG_MARKET_TRADE, 16'd3, 64'd110, 32'd1, 8'd1, 32'd2);
        if (feature_vwap !== 64'd103 || feature_vwap_sum_price_quantity !== 102'd310 ||
            feature_vwap_sum_quantity !== 38'd3)
            $fatal(1, "weighted VWAP truncation mismatch");
        consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd3, 64'd100, 32'd1, 8'd0, 32'd3);
        consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd3, 64'd106, 32'd1, 8'd1, 32'd4);
        if (feature_midpoint !== 64'd103 || feature_midpoint_minus_vwap !== 65'sd0 ||
            !feature_midpoint_minus_vwap_valid ||
            feature_midpoint_minus_vwap_bps_x100 !== 32'sd0 ||
            !feature_midpoint_minus_vwap_bps_x100_valid ||
            feature_imbalance_normalized !== 16'sd0)
            $fatal(1, "equal midpoint/VWAP delta mismatch");
        consume_feature();

        // Zero-size top-of-book is a valid quote but has no imbalance
        // denominator. The spread ratio remains valid because midpoint is
        // non-zero; normalized imbalance must be explicitly invalid.
        send_event(`MSG_MARKET_QUOTE, 16'd2, 64'd100, 32'd0, 8'd0, 32'd1);
        consume_feature();
        send_event(`MSG_MARKET_QUOTE, 16'd2, 64'd102, 32'd0, 8'd1, 32'd2);
        if (!feature_spread_bps_x100_valid || feature_spread_bps_x100 !== 32'sd19801 ||
            feature_imbalance_normalized_valid || feature_imbalance_normalized !== 16'sd0 ||
            feature_vwap_quotient_valid || feature_midpoint_minus_vwap_valid)
            $fatal(1, "zero-denominator normalized feature mismatch");
        consume_feature();

        // Backpressure must hold a complete registered feature record.
        feature_ready = 1'b0;
        send_event(`MSG_MARKET_TRADE, 16'd1, 64'd201, 32'd7, 8'd1, 32'd102);
        if (!feature_valid || feature_sequence !== 32'd102 || feature_rolling_volume !== 38'd7)
            $fatal(1, "feature backpressure did not hold output");
        repeat (3) begin @(posedge clk); #1; if (!feature_valid) $fatal(1, "feature was lost under backpressure"); end
        feature_ready = 1'b1;
        @(posedge clk); #1;
        if (feature_valid) $fatal(1, "feature did not drain after ready");

        // Reset clears valid state; a post-reset event is accepted as first.
        reset_n = 1'b0;
        #15;
        reset_n = 1'b1;
        #5;
        send_event(`MSG_MARKET_QUOTE, 16'd0, 64'd500, 32'd9, 8'd0, 32'd1);
        if (feature_bid_price !== 64'd500 || feature_bid_quantity !== 32'd9 || feature_spread_valid)
            $fatal(1, "reset did not clear market state");

        $display("tb_market_state_engine: PASS");
        $finish;
    end
endmodule
