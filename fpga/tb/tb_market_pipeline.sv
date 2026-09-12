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
    wire event_reject_pulse;
    wire [7:0] event_reject_reason;
    wire [15:0] event_reject_symbol_id;
    wire [31:0] event_reject_sequence;

    reg [255:0] packet_memory [0:9999];
    integer event_count;
    integer event_index;
    reg [1023:0] event_file;

    always #5 clk = ~clk;

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

    always @(posedge clk) begin
        #1;
        if (dispatch_error_pulse)
            $display("DISPATCH_ERROR %02h %04h %08h", dispatch_error_reason,
                     dispatch_error_symbol_id, dispatch_error_sequence);
        if (event_reject_pulse)
            $display("EVENT_REJECT %02h %04h %08h", event_reject_reason,
                     event_reject_symbol_id, event_reject_sequence);
        if (feature_valid)
            $display("FEATURE %04h %08h %016h %08h %016h %08h %016h %01h %016h %01h %017h %01h %0h %09h %09h %01h %0h %0h %01h",
                     feature_symbol_id, feature_sequence,
                     feature_bid_price, feature_bid_quantity,
                     feature_ask_price, feature_ask_quantity,
                     feature_spread, feature_spread_valid,
                     feature_midpoint, feature_midpoint_valid,
                     feature_momentum, feature_momentum_valid,
                     feature_rolling_volume, feature_imbalance_numerator,
                     feature_imbalance_denominator, feature_imbalance_valid,
                     feature_vwap_sum_price_quantity, feature_vwap_sum_quantity,
                     feature_vwap_valid);
    end

    initial begin
        if (!$value$plusargs("EVENT_FILE=%s", event_file)) $fatal(1, "EVENT_FILE plusarg is required");
        if (!$value$plusargs("EVENT_COUNT=%d", event_count)) $fatal(1, "EVENT_COUNT plusarg is required");
        if (event_count > 10000) $fatal(1, "event count exceeds replay memory");
        $readmemh(event_file, packet_memory);

        #12;
        reset_n = 1'b1;
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
