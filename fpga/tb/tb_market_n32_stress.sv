`timescale 1ns/1ps

`include "protocol_defs.svh"

// Deterministic N=32 state-isolation and flow-control stress. This bench is
// intentionally focused on the per-symbol engine boundary: it does not alter
// the SPI, FIFO, CDC, or packet protocol tests.
module tb_market_n32_stress;
    localparam integer NUM_SYMBOLS = 32;
    localparam integer TRADE_WINDOW = 4;
    localparam integer MOMENTUM_WINDOW = 3;
    localparam integer VWAP_WINDOW = 4;
    localparam integer TRADE_ACC_W = 32 + $clog2(TRADE_WINDOW);
    localparam integer VWAP_ACC_W = 96 + $clog2(VWAP_WINDOW);
    localparam integer VWAP_QTY_ACC_W = 32 + $clog2(VWAP_WINDOW);

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
    reg [15:0] event_flags = 16'hA55A;
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
    wire event_reject_pulse;
    wire [7:0] event_reject_reason;
    wire [15:0] event_reject_symbol_id;
    wire [31:0] event_reject_sequence;

    reg [63:0] expected_bid [0:NUM_SYMBOLS-1];
    reg [31:0] expected_bid_quantity [0:NUM_SYMBOLS-1];
    reg [63:0] expected_ask [0:NUM_SYMBOLS-1];
    reg [31:0] expected_ask_quantity [0:NUM_SYMBOLS-1];
    reg [31:0] expected_sequence [0:NUM_SYMBOLS-1];
    integer symbol_index;
    integer round_index;
    integer random_index;
    reg [31:0] lfsr;

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
        .event_reject_pulse(event_reject_pulse), .event_reject_reason(event_reject_reason),
        .event_reject_symbol_id(event_reject_symbol_id),
        .event_reject_sequence(event_reject_sequence)
    );

    task automatic consume_feature;
        begin
            @(posedge clk); #1;
        end
    endtask

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
            event_valid = 1'b1;
            @(posedge clk); #1;
            event_valid = 1'b0;
            while (!(feature_valid || event_reject_pulse)) begin @(posedge clk); #1; end
        end
    endtask

    task automatic check_feature;
        input integer expected_symbol;
        input [31:0] expected_seq;
        begin
            if (!feature_valid || feature_symbol_id !== expected_symbol[15:0] ||
                feature_sequence !== expected_seq ||
                feature_bid_price !== expected_bid[expected_symbol] ||
                feature_bid_quantity !== expected_bid_quantity[expected_symbol] ||
                feature_ask_price !== expected_ask[expected_symbol] ||
                feature_ask_quantity !== expected_ask_quantity[expected_symbol])
                $fatal(1, "N32 state leak symbol=%0d got symbol=%0d seq=%0d bid=%0d ask=%0d",
                       expected_symbol, feature_symbol_id, feature_sequence,
                       feature_bid_price, feature_ask_price);
        end
    endtask

    task automatic accept_quote;
        input integer symbol_value;
        input [63:0] price_value;
        input [31:0] quantity_value;
        input [7:0] side_value;
        input [31:0] sequence_value;
        begin
            if (side_value == 8'd0) begin
                expected_bid[symbol_value] = price_value;
                expected_bid_quantity[symbol_value] = quantity_value;
            end else begin
                expected_ask[symbol_value] = price_value;
                expected_ask_quantity[symbol_value] = quantity_value;
            end
            expected_sequence[symbol_value] = sequence_value;
            send_event(`MSG_MARKET_QUOTE, symbol_value[15:0], price_value,
                       quantity_value, side_value, sequence_value);
            check_feature(symbol_value, sequence_value);
            consume_feature();
        end
    endtask

    initial begin
        for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1) begin
            expected_bid[symbol_index] = 64'd0;
            expected_bid_quantity[symbol_index] = 32'd0;
            expected_ask[symbol_index] = 64'd0;
            expected_ask_quantity[symbol_index] = 32'd0;
            expected_sequence[symbol_index] = 32'd0;
        end

        #12;
        reset_n = 1'b1;

        // Round-robin all 32 symbols, then complete every quote independently.
        for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1)
            accept_quote(symbol_index, 64'd1000 + symbol_index, 32'd10 + symbol_index,
                         8'd0, 32'd1);
        for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1)
            accept_quote(symbol_index, 64'd2000 + symbol_index, 32'd20 + symbol_index,
                         8'd1, 32'd2);

        // Rapid symbol changes and circular-window traffic updates.
        for (round_index = 0; round_index < 6; round_index = round_index + 1) begin
            for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1) begin
                expected_sequence[symbol_index] = 32'd3 + round_index;
                send_event(`MSG_MARKET_TRADE, symbol_index[15:0],
                           64'd1500 + symbol_index, 32'd1 + round_index,
                           round_index[0], expected_sequence[symbol_index]);
                check_feature(symbol_index, expected_sequence[symbol_index]);
                consume_feature();
            end
        end

        // Hot-symbol burst after the round-robin traffic.
        for (round_index = 0; round_index < 20; round_index = round_index + 1) begin
            expected_sequence[7] = 32'd32 + round_index;
            send_event(`MSG_MARKET_TRADE, 16'd7, 64'd1510,
                       32'd100 + round_index, round_index[0], expected_sequence[7]);
            check_feature(7, expected_sequence[7]);
            consume_feature();
        end

        // Deterministic pseudo-random interleaving across all symbols.
        lfsr = 32'h1ACE_B00C;
        for (random_index = 0; random_index < 160; random_index = random_index + 1) begin
            lfsr = {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
            symbol_index = lfsr[4:0];
            expected_sequence[symbol_index] = expected_sequence[symbol_index] + 1'b1;
            if (random_index[1:0] == 2'b00) begin
                accept_quote(symbol_index, 64'd3000 + random_index,
                             32'd30 + random_index, random_index[0],
                             expected_sequence[symbol_index]);
            end else begin
                send_event(`MSG_MARKET_TRADE, symbol_index[15:0],
                           64'd1600 + symbol_index, 32'd2 + random_index,
                           random_index[0], expected_sequence[symbol_index]);
                check_feature(symbol_index, expected_sequence[symbol_index]);
                consume_feature();
            end
        end

        // Sequence wrap is intentionally unsupported for every symbol.
        for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1) begin
            accept_quote(symbol_index, 64'd4000 + symbol_index, 32'd1,
                         8'd0, 32'hFFFF_FFFE);
            send_event(`MSG_MARKET_QUOTE, symbol_index[15:0], 64'd4100 + symbol_index,
                       32'd1, 8'd1, 32'd1);
            if (!event_reject_pulse || event_reject_reason !== `STATUS_STALE_SEQ ||
                event_reject_symbol_id !== symbol_index[15:0] ||
                event_reject_sequence !== 32'd1)
                $fatal(1, "N32 sequence-wrap policy failed for symbol %0d", symbol_index);
            consume_feature();
        end

        // Reset during a populated stream must clear every symbol's valid state.
        reset_n = 1'b0;
        #17;
        reset_n = 1'b1;
        expected_bid[31] = 64'd0;
        expected_bid_quantity[31] = 32'd0;
        expected_ask[31] = 64'd0;
        expected_ask_quantity[31] = 32'd0;
        expected_sequence[31] = 32'd1;
        accept_quote(31, 64'd5000, 32'd55, 8'd0, 32'd1);

        // Feature backpressure must hold the complete post-reset record.
        feature_ready = 1'b0;
        expected_sequence[31] = 32'd2;
        send_event(`MSG_MARKET_TRADE, 16'd31, 64'd5001, 32'd9, 8'd1, 32'd2);
        check_feature(31, 32'd2);
        repeat (5) begin
            @(posedge clk); #1;
            if (!feature_valid) $fatal(1, "N32 feature lost under backpressure");
        end
        feature_ready = 1'b1;
        consume_feature();

        $display("tb_market_n32_stress: PASS symbols=%0d rounds=%0d random=%0d",
                 NUM_SYMBOLS, 6, 160);
        $finish;
    end
endmodule
