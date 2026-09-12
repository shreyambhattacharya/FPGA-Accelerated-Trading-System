`timescale 1ns/1ps

`include "protocol_defs.svh"

// Deterministic candidate-signal engine.
//
// This block consumes the registered normalized feature record and emits a
// signal record for every accepted feature.  SIGNAL_NONE records are
// intentional: they carry reason bits for invalid, disabled, failed, or
// suppressed evaluations, so the Raspberry Pi can explain each decision.
// The block never represents an order, position, balance, or broker action.
module signal_engine #(
    parameter integer NUM_SYMBOLS = 4,
    parameter integer TRADE_ACC_W = 32 + ((32 <= 1) ? 1 : $clog2(32))
) (
    input wire                         clk,
    input wire                         reset_n,

    input wire                         feature_valid,
    output wire                        feature_ready,
    input wire [15:0]                  feature_symbol_id,
    input wire [31:0]                  feature_sequence,
    input wire signed [31:0]           feature_spread_bps_x100,
    input wire                         feature_spread_valid,
    input wire signed [31:0]           feature_momentum_bps_x100,
    input wire                         feature_momentum_valid,
    input wire signed [15:0]           feature_imbalance_q15,
    input wire                         feature_imbalance_valid,
    input wire [63:0]                  feature_vwap,
    input wire                         feature_vwap_valid,
    input wire signed [64:0]           feature_midpoint_minus_vwap,
    input wire signed [31:0]           feature_midpoint_minus_vwap_bps_x100,
    input wire                         feature_vwap_delta_valid,
    input wire [TRADE_ACC_W-1:0]       feature_rolling_volume,

    input wire                         strategy_enable,
    input wire                         long_enable,
    input wire                         short_enable,
    input wire signed [31:0]           long_min_momentum_bps_x100,
    input wire signed [31:0]           short_max_momentum_bps_x100,
    input wire signed [31:0]           long_min_vwap_delta_bps_x100,
    input wire signed [31:0]           short_max_vwap_delta_bps_x100,
    input wire signed [15:0]           long_min_imbalance_q15,
    input wire signed [15:0]           short_max_imbalance_q15,
    input wire signed [31:0]           max_spread_bps_x100,
    input wire [TRADE_ACC_W-1:0]       min_rolling_volume,
    input wire [31:0]                  signal_cooldown_events,
    input wire [NUM_SYMBOLS-1:0]       symbol_enable,

    input wire                         slot_reset_valid,
    input wire [15:0]                  slot_reset_symbol_id,
    output wire                        slot_reset_ready,

    output wire                        signal_valid,
    input wire                         signal_ready,
    output wire [15:0]                 signal_symbol_id,
    output wire [31:0]                 signal_sequence,
    output wire [1:0]                  signal_action,
    output wire [3:0]                  signal_score,
    output wire [15:0]                 signal_reason_bits
);
    localparam [1:0] ACTION_NONE = `SIGNAL_NONE;
    localparam [1:0] ACTION_LONG = `SIGNAL_LONG_CANDIDATE;
    localparam [1:0] ACTION_SHORT = `SIGNAL_SHORT_CANDIDATE;

    reg [1:0] previous_condition [0:NUM_SYMBOLS-1];
    reg [1:0] last_emitted_action [0:NUM_SYMBOLS-1];
    reg [31:0] cooldown_remaining [0:NUM_SYMBOLS-1];

    reg signal_valid_reg = 1'b0;
    reg [15:0] signal_symbol_id_reg = 16'd0;
    reg [31:0] signal_sequence_reg = 32'd0;
    reg [1:0] signal_action_reg = ACTION_NONE;
    reg [3:0] signal_score_reg = 4'd0;
    reg [15:0] signal_reason_bits_reg = 16'd0;

    reg symbol_is_enabled_eval;
    reg [1:0] previous_condition_eval;
    reg [1:0] last_emitted_action_eval;
    reg [31:0] cooldown_remaining_eval;

    reg [1:0] raw_direction_eval;
    reg [1:0] eligible_direction_eval;
    reg candidate_fire_eval;
    reg [3:0] score_eval;
    reg [15:0] reason_eval;
    reg long_momentum_pass;
    reg long_vwap_delta_pass;
    reg long_imbalance_pass;
    reg long_spread_pass;
    reg long_volume_pass;
    reg short_momentum_pass;
    reg short_vwap_delta_pass;
    reg short_imbalance_pass;
    reg short_spread_pass;
    reg short_volume_pass;
    reg feature_set_valid;

    wire symbol_in_range = (feature_symbol_id < NUM_SYMBOLS);
    wire direction_edge = (raw_direction_eval != ACTION_NONE) &&
                          (raw_direction_eval != previous_condition_eval);
    wire cooldown_expiry_repeat = (signal_cooldown_events != 0) &&
                                  (cooldown_remaining_eval == 32'd1) &&
                                  (raw_direction_eval == last_emitted_action_eval);

    function automatic [3:0] score_conditions;
        input pass_momentum;
        input pass_vwap_delta;
        input pass_imbalance;
        input pass_spread;
        input pass_volume;
        integer total;
        begin
            total = pass_momentum + pass_vwap_delta + pass_imbalance +
                    pass_spread + pass_volume;
            score_conditions = total[3:0];
        end
    endfunction

    always @* begin
        symbol_is_enabled_eval = 1'b0;
        previous_condition_eval = ACTION_NONE;
        last_emitted_action_eval = ACTION_NONE;
        cooldown_remaining_eval = 32'd0;
        if (symbol_in_range) begin
            symbol_is_enabled_eval = symbol_enable[feature_symbol_id];
            previous_condition_eval = previous_condition[feature_symbol_id];
            last_emitted_action_eval = last_emitted_action[feature_symbol_id];
            cooldown_remaining_eval = cooldown_remaining[feature_symbol_id];
        end
    end

    always @* begin
        feature_set_valid = feature_spread_valid &&
                            feature_momentum_valid &&
                            feature_imbalance_valid &&
                            feature_vwap_valid &&
                            feature_vwap_delta_valid;

        long_momentum_pass = feature_momentum_valid &&
                             (feature_momentum_bps_x100 >= long_min_momentum_bps_x100);
        long_vwap_delta_pass = feature_vwap_delta_valid &&
                               (feature_midpoint_minus_vwap_bps_x100 >=
                                long_min_vwap_delta_bps_x100);
        long_imbalance_pass = feature_imbalance_valid &&
                              (feature_imbalance_q15 >= long_min_imbalance_q15);
        long_spread_pass = feature_spread_valid &&
                           (feature_spread_bps_x100 <= max_spread_bps_x100);
        long_volume_pass = (feature_rolling_volume >= min_rolling_volume);

        short_momentum_pass = feature_momentum_valid &&
                              (feature_momentum_bps_x100 <= short_max_momentum_bps_x100);
        short_vwap_delta_pass = feature_vwap_delta_valid &&
                                (feature_midpoint_minus_vwap_bps_x100 <=
                                 short_max_vwap_delta_bps_x100);
        short_imbalance_pass = feature_imbalance_valid &&
                               (feature_imbalance_q15 <= short_max_imbalance_q15);
        short_spread_pass = feature_spread_valid &&
                            (feature_spread_bps_x100 <= max_spread_bps_x100);
        short_volume_pass = (feature_rolling_volume >= min_rolling_volume);

        // Long has deterministic priority if intentionally overlapping
        // thresholds make both directions true.  The raw direction tracks
        // the feature condition even when a side is disabled, so enabling a
        // side later creates a clean false-to-true edge.
        raw_direction_eval = ACTION_NONE;
        if (long_momentum_pass && long_vwap_delta_pass && long_imbalance_pass &&
            long_spread_pass && long_volume_pass)
            raw_direction_eval = ACTION_LONG;
        else if (short_momentum_pass && short_vwap_delta_pass &&
                 short_imbalance_pass && short_spread_pass && short_volume_pass)
            raw_direction_eval = ACTION_SHORT;

        eligible_direction_eval = ACTION_NONE;
        if (feature_set_valid && symbol_is_enabled_eval && strategy_enable) begin
            if ((raw_direction_eval == ACTION_LONG) && long_enable)
                eligible_direction_eval = ACTION_LONG;
            else if ((raw_direction_eval == ACTION_SHORT) && short_enable)
                eligible_direction_eval = ACTION_SHORT;
        end

        candidate_fire_eval = (eligible_direction_eval != ACTION_NONE) &&
                              ((direction_edge && (cooldown_remaining_eval == 0)) ||
                               ((eligible_direction_eval == last_emitted_action_eval) &&
                                cooldown_expiry_repeat));

        reason_eval = 16'd0;
        if (raw_direction_eval == ACTION_LONG) begin
            if (long_momentum_pass) reason_eval = reason_eval | `SIGNAL_REASON_MOMENTUM_PASS;
            if (long_vwap_delta_pass) reason_eval = reason_eval | `SIGNAL_REASON_VWAP_DELTA_PASS;
            if (long_imbalance_pass) reason_eval = reason_eval | `SIGNAL_REASON_IMBALANCE_PASS;
            if (long_spread_pass) reason_eval = reason_eval | `SIGNAL_REASON_SPREAD_PASS;
            if (long_volume_pass) reason_eval = reason_eval | `SIGNAL_REASON_VOLUME_PASS;
            reason_eval = reason_eval | `SIGNAL_REASON_LONG_DIRECTION;
        end else if (raw_direction_eval == ACTION_SHORT) begin
            if (short_momentum_pass) reason_eval = reason_eval | `SIGNAL_REASON_MOMENTUM_PASS;
            if (short_vwap_delta_pass) reason_eval = reason_eval | `SIGNAL_REASON_VWAP_DELTA_PASS;
            if (short_imbalance_pass) reason_eval = reason_eval | `SIGNAL_REASON_IMBALANCE_PASS;
            if (short_spread_pass) reason_eval = reason_eval | `SIGNAL_REASON_SPREAD_PASS;
            if (short_volume_pass) reason_eval = reason_eval | `SIGNAL_REASON_VOLUME_PASS;
            reason_eval = reason_eval | `SIGNAL_REASON_SHORT_DIRECTION;
        end else begin
            if (long_momentum_pass || short_momentum_pass)
                reason_eval = reason_eval | `SIGNAL_REASON_MOMENTUM_PASS;
            if (long_vwap_delta_pass || short_vwap_delta_pass)
                reason_eval = reason_eval | `SIGNAL_REASON_VWAP_DELTA_PASS;
            if (long_imbalance_pass || short_imbalance_pass)
                reason_eval = reason_eval | `SIGNAL_REASON_IMBALANCE_PASS;
            if (long_spread_pass || short_spread_pass)
                reason_eval = reason_eval | `SIGNAL_REASON_SPREAD_PASS;
            if (long_volume_pass || short_volume_pass)
                reason_eval = reason_eval | `SIGNAL_REASON_VOLUME_PASS;
        end

        if (!feature_set_valid)
            reason_eval = reason_eval | `SIGNAL_REASON_INVALID_FEATURE;
        if (!symbol_is_enabled_eval)
            reason_eval = reason_eval | `SIGNAL_REASON_SYMBOL_DISABLED;
        if (!strategy_enable)
            reason_eval = reason_eval | `SIGNAL_REASON_STRATEGY_DISABLED;
        if (!long_enable && (raw_direction_eval == ACTION_LONG))
            reason_eval = reason_eval | `SIGNAL_REASON_LONG_DISABLED;
        if (!short_enable && (raw_direction_eval == ACTION_SHORT))
            reason_eval = reason_eval | `SIGNAL_REASON_SHORT_DISABLED;
        if ((raw_direction_eval != ACTION_NONE) && (cooldown_remaining_eval != 0) &&
            !candidate_fire_eval)
            reason_eval = reason_eval | `SIGNAL_REASON_COOLDOWN_ACTIVE;
        if ((raw_direction_eval != ACTION_NONE) && !candidate_fire_eval)
            reason_eval = reason_eval | `SIGNAL_REASON_SUPPRESSED;

        if (raw_direction_eval == ACTION_LONG)
            score_eval = score_conditions(long_momentum_pass, long_vwap_delta_pass,
                                          long_imbalance_pass, long_spread_pass,
                                          long_volume_pass);
        else if (raw_direction_eval == ACTION_SHORT)
            score_eval = score_conditions(short_momentum_pass, short_vwap_delta_pass,
                                          short_imbalance_pass, short_spread_pass,
                                          short_volume_pass);
        else begin
            score_eval = score_conditions(
                long_momentum_pass || short_momentum_pass,
                long_vwap_delta_pass || short_vwap_delta_pass,
                long_imbalance_pass || short_imbalance_pass,
                long_spread_pass || short_spread_pass,
                long_volume_pass || short_volume_pass);
        end
    end

    assign feature_ready = !slot_reset_valid &&
                           (!signal_valid_reg || signal_ready);
    assign slot_reset_ready = !signal_valid_reg || signal_ready;
    assign signal_valid = signal_valid_reg;
    assign signal_symbol_id = signal_symbol_id_reg;
    assign signal_sequence = signal_sequence_reg;
    assign signal_action = signal_action_reg;
    assign signal_score = signal_score_reg;
    assign signal_reason_bits = signal_reason_bits_reg;

    integer symbol_index;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            signal_valid_reg <= 1'b0;
            signal_symbol_id_reg <= 16'd0;
            signal_sequence_reg <= 32'd0;
            signal_action_reg <= ACTION_NONE;
            signal_score_reg <= 4'd0;
            signal_reason_bits_reg <= 16'd0;
            for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1) begin
                previous_condition[symbol_index] <= ACTION_NONE;
                last_emitted_action[symbol_index] <= ACTION_NONE;
                cooldown_remaining[symbol_index] <= 32'd0;
            end
        end else if (slot_reset_valid && slot_reset_ready &&
                     (slot_reset_symbol_id < NUM_SYMBOLS)) begin
            previous_condition[slot_reset_symbol_id] <= ACTION_NONE;
            last_emitted_action[slot_reset_symbol_id] <= ACTION_NONE;
            cooldown_remaining[slot_reset_symbol_id] <= 32'd0;
            signal_valid_reg <= 1'b0;
        end else begin
            if (signal_valid_reg && signal_ready)
                signal_valid_reg <= 1'b0;

            if (feature_valid && feature_ready) begin
                signal_symbol_id_reg <= feature_symbol_id;
                signal_sequence_reg <= feature_sequence;
                signal_action_reg <= candidate_fire_eval ? eligible_direction_eval : ACTION_NONE;
                signal_score_reg <= score_eval;
                signal_reason_bits_reg <= reason_eval;
                signal_valid_reg <= 1'b1;

                if (symbol_in_range) begin
                    // Disabled strategy/slots are deliberately disarmed so
                    // enabling them while the feature condition is already
                    // true produces a fresh edge instead of inheriting a
                    // hidden prior condition.
                    if (!strategy_enable || !symbol_is_enabled_eval ||
                        ((raw_direction_eval == ACTION_LONG) && !long_enable) ||
                        ((raw_direction_eval == ACTION_SHORT) && !short_enable))
                        previous_condition[feature_symbol_id] <= ACTION_NONE;
                    else
                        previous_condition[feature_symbol_id] <= raw_direction_eval;
                    if (candidate_fire_eval) begin
                        last_emitted_action[feature_symbol_id] <= eligible_direction_eval;
                        cooldown_remaining[feature_symbol_id] <= signal_cooldown_events;
                    end else if (cooldown_remaining_eval != 0) begin
                        cooldown_remaining[feature_symbol_id] <= cooldown_remaining_eval - 1'b1;
                    end
                end
            end
        end
    end
endmodule
