`timescale 1ns/1ps

`include "protocol_defs.svh"

// Directed signal-engine verification.  The bench uses NUM_SYMBOLS=32 so it
// also exercises interleaved symbol state at the production candidate size.
module tb_signal_engine;
    localparam integer NUM_SYMBOLS = 32;
    localparam integer TRADE_ACC_W = 37;

    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg feature_valid = 1'b0;
    wire feature_ready;
    reg [15:0] feature_symbol_id = 16'd0;
    reg [31:0] feature_sequence = 32'd0;
    reg signed [31:0] feature_spread_bps_x100 = 32'sd200;
    reg feature_spread_valid = 1'b1;
    reg signed [31:0] feature_momentum_bps_x100 = 32'sd200;
    reg feature_momentum_valid = 1'b1;
    reg signed [15:0] feature_imbalance_q15 = 16'sd2000;
    reg feature_imbalance_valid = 1'b1;
    reg [63:0] feature_vwap = 64'd100;
    reg feature_vwap_valid = 1'b1;
    reg signed [64:0] feature_midpoint_minus_vwap = 65'sd1;
    reg signed [31:0] feature_midpoint_minus_vwap_bps_x100 = 32'sd200;
    reg feature_vwap_delta_valid = 1'b1;
    reg [TRADE_ACC_W-1:0] feature_rolling_volume = 37'd100;

    reg strategy_enable = 1'b1;
    reg long_enable = 1'b1;
    reg short_enable = 1'b1;
    reg signed [31:0] long_min_momentum_bps_x100 = 32'sd100;
    reg signed [31:0] short_max_momentum_bps_x100 = -32'sd100;
    reg signed [31:0] long_min_vwap_delta_bps_x100 = 32'sd100;
    reg signed [31:0] short_max_vwap_delta_bps_x100 = -32'sd100;
    reg signed [15:0] long_min_imbalance_q15 = 16'sd1638;
    reg signed [15:0] short_max_imbalance_q15 = -16'sd1638;
    reg signed [31:0] max_spread_bps_x100 = 32'sd500;
    reg [TRADE_ACC_W-1:0] min_rolling_volume = 37'd100;
    reg [31:0] signal_cooldown_events = 32'd2;
    reg [NUM_SYMBOLS-1:0] symbol_enable = {NUM_SYMBOLS{1'b1}};

    reg slot_reset_valid = 1'b0;
    reg [15:0] slot_reset_symbol_id = 16'd0;
    wire slot_reset_ready;
    wire signal_valid;
    reg signal_ready = 1'b1;
    wire [15:0] signal_symbol_id;
    wire [31:0] signal_sequence;
    wire [1:0] signal_action;
    wire [3:0] signal_score;
    wire [15:0] signal_reason_bits;

    always #5 clk = ~clk;

    signal_engine #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
        .TRADE_ACC_W(TRADE_ACC_W)
    ) dut (
        .clk(clk), .reset_n(reset_n),
        .feature_valid(feature_valid), .feature_ready(feature_ready),
        .feature_symbol_id(feature_symbol_id), .feature_sequence(feature_sequence),
        .feature_spread_bps_x100(feature_spread_bps_x100),
        .feature_spread_valid(feature_spread_valid),
        .feature_momentum_bps_x100(feature_momentum_bps_x100),
        .feature_momentum_valid(feature_momentum_valid),
        .feature_imbalance_q15(feature_imbalance_q15),
        .feature_imbalance_valid(feature_imbalance_valid),
        .feature_vwap(feature_vwap), .feature_vwap_valid(feature_vwap_valid),
        .feature_midpoint_minus_vwap(feature_midpoint_minus_vwap),
        .feature_midpoint_minus_vwap_bps_x100(feature_midpoint_minus_vwap_bps_x100),
        .feature_vwap_delta_valid(feature_vwap_delta_valid),
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
        .slot_reset_ready(slot_reset_ready),
        .signal_valid(signal_valid), .signal_ready(signal_ready),
        .signal_symbol_id(signal_symbol_id), .signal_sequence(signal_sequence),
        .signal_action(signal_action), .signal_score(signal_score),
        .signal_reason_bits(signal_reason_bits)
    );

    task automatic send_feature;
        input integer symbol_value;
        input integer sequence_value;
        input integer momentum_value;
        input integer vwap_delta_value;
        input integer imbalance_value;
        input integer spread_value;
        input integer volume_value;
        input valid_spread;
        input valid_momentum;
        input valid_imbalance;
        input valid_vwap;
        input valid_vwap_delta;
        input [1:0] expected_action;
        input [3:0] expected_score;
        input [15:0] expected_reason;
        begin
            @(negedge clk);
            while (!feature_ready)
                @(negedge clk);
            feature_symbol_id = symbol_value;
            feature_sequence = sequence_value;
            feature_momentum_bps_x100 = momentum_value;
            feature_midpoint_minus_vwap_bps_x100 = vwap_delta_value;
            feature_imbalance_q15 = imbalance_value;
            feature_spread_bps_x100 = spread_value;
            feature_rolling_volume = volume_value;
            feature_spread_valid = valid_spread;
            feature_momentum_valid = valid_momentum;
            feature_imbalance_valid = valid_imbalance;
            feature_vwap_valid = valid_vwap;
            feature_vwap_delta_valid = valid_vwap_delta;
            feature_valid = 1'b1;
            @(posedge clk);
            #1;
            feature_valid = 1'b0;
            while (!signal_valid)
                begin @(posedge clk); #1; end
            if (signal_symbol_id !== symbol_value || signal_sequence !== sequence_value)
                $fatal(1, "signal metadata mismatch");
            if (signal_action !== expected_action || signal_score !== expected_score ||
                signal_reason_bits !== expected_reason)
                $fatal(1, "signal mismatch sym=%0d seq=%0d action=%0d/%0d score=%0d/%0d reason=%04h/%04h",
                       symbol_value, sequence_value, signal_action, expected_action,
                       signal_score, expected_score, signal_reason_bits, expected_reason);
            @(posedge clk);
            #1;
        end
    endtask

    task automatic reset_slot;
        input integer symbol_value;
        begin
            @(negedge clk);
            while (!slot_reset_ready)
                @(negedge clk);
            slot_reset_symbol_id = symbol_value;
            slot_reset_valid = 1'b1;
            @(posedge clk);
            #1;
            slot_reset_valid = 1'b0;
            if (signal_valid)
                $fatal(1, "slot reset did not clear held signal");
        end
    endtask

    integer held_symbol;
    reg [31:0] held_sequence;
    reg [1:0] held_action;
    reg [3:0] held_score;
    reg [15:0] held_reason;
    integer hold_index;

    initial begin
        #12;
        reset_n = 1'b1;

        // Invalid feature set is always NO_ACTION, with a diagnostic reason.
        send_feature(0, 1, 200, 200, 2000, 200, 100,
                     1'b0, 1'b0, 1'b0, 1'b0, 1'b0,
                     `SIGNAL_NONE, 4'd1, 16'h0110);

        // All five long conditions pass and produce a candidate on the edge.
        send_feature(0, 2, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);
        // Sustained condition is suppressed, then a cooldown-expiry repeat
        // is emitted on the third accepted feature event.
        send_feature(0, 3, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_NONE, 4'd5, 16'h20BF);
        send_feature(0, 4, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);

        // Disable/enable the strategy while a condition is true: enabling
        // re-arms the edge because disabled evaluations are disarmed.
        strategy_enable = 1'b0;
        send_feature(3, 1, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_NONE, 4'd5, 16'h243F);
        strategy_enable = 1'b1;
        send_feature(3, 2, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);

        // A direction reversal is an observable candidate when cooldown is 0.
        signal_cooldown_events = 0;
        send_feature(1, 1, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);
        send_feature(1, 2, -200, -200, -2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_SHORT_CANDIDATE, 4'd5, 16'h005F);

        // Threshold equality passes; one unit below fails momentum.
        send_feature(2, 1, 100, 100, 1638, 500, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);
        send_feature(2, 2, 99, 100, 1638, 500, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_NONE, 4'd4, 16'h001E);

        // Symbol enable is independent per slot.
        symbol_enable[7] = 1'b0;
        send_feature(7, 1, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_NONE, 4'd5, 16'h223F);
        symbol_enable[7] = 1'b1;
        send_feature(7, 2, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);

        // Slot reset removes the previous edge/cooldown state for one symbol.
        send_feature(8, 1, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);
        send_feature(8, 2, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_NONE, 4'd5, 16'h203F);
        reset_slot(8);
        send_feature(8, 3, 200, 200, 2000, 200, 100,
                     1'b1, 1'b1, 1'b1, 1'b1, 1'b1,
                     `SIGNAL_LONG_CANDIDATE, 4'd5, 16'h003F);

        // Hold a complete candidate record while the downstream consumer is
        // stalled.  Neither the record nor feature_ready may change.
        @(negedge clk);
        signal_ready = 1'b0;
        while (!feature_ready)
            @(negedge clk);
        feature_symbol_id = 10;
        feature_sequence = 1;
        feature_valid = 1'b1;
        @(posedge clk);
        #1;
        feature_valid = 1'b0;
        if (!signal_valid || feature_ready)
            $fatal(1, "signal backpressure handshake failed");
        held_symbol = signal_symbol_id;
        held_sequence = signal_sequence;
        held_action = signal_action;
        held_score = signal_score;
        held_reason = signal_reason_bits;
        for (hold_index = 0; hold_index < 3; hold_index = hold_index + 1) begin
            @(posedge clk);
            #1;
            if (!signal_valid || feature_ready || signal_symbol_id !== held_symbol ||
                signal_sequence !== held_sequence || signal_action !== held_action ||
                signal_score !== held_score || signal_reason_bits !== held_reason)
                $fatal(1, "signal record changed under backpressure");
        end
        signal_ready = 1'b1;
        @(posedge clk);
        #1;
        if (signal_valid)
            $fatal(1, "signal did not drain after ready");

        $display("tb_signal_engine: PASS directed=20 interleaved_symbols=32");
        $finish;
    end
endmodule
