`timescale 1ns/1ps

`include "protocol_defs.svh"

// Parameterized per-symbol market-state and feature engine.
//
// The engine is deliberately serialized: one normalized event is captured,
// its selected symbol bank is read into working registers, and one shared
// arithmetic path updates that working copy before the result is committed.
// This is a throughput/area tradeoff. It keeps the packet protocol and
// valid/ready contract unchanged while preventing packet decode, a wide
// multiplier, and a NUM_SYMBOLS-way state write enable from sharing one
// timing cone.
//
// The history memories are banked by symbol and window entry. Their old terms
// are read into registers in READ_HISTORY, and are written only in
// WRITE_HISTORY. This style keeps the dynamic bank access out of the feature
// arithmetic cone while allowing the vendor to evaluate the memory mapping.
module market_state_engine #(
    parameter integer NUM_SYMBOLS = 4,
    parameter integer TRADE_WINDOW = 32,
    parameter integer MOMENTUM_WINDOW = 16,
    parameter integer VWAP_WINDOW = 32,
    parameter integer TRADE_ACC_W = 32 + ((TRADE_WINDOW <= 1) ? 1 : $clog2(TRADE_WINDOW)),
    parameter integer MOMENTUM_PTR_W = (MOMENTUM_WINDOW <= 1) ? 1 : $clog2(MOMENTUM_WINDOW),
    parameter integer VWAP_ACC_W = 96 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW)),
    parameter integer VWAP_QTY_ACC_W = 32 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW)),
    parameter integer PRICE_SCALE = 1000000,
    parameter integer BPS_SCALE = 10000,
    parameter integer BPS_OUTPUT_SCALE = 100,
    parameter integer IMBALANCE_FRAC_BITS = 15
) (
    input  wire                    clk,
    input  wire                    reset_n,

    input  wire                    event_valid,
    output wire                    event_ready,
    input  wire [7:0]              event_type,
    input  wire [15:0]             event_symbol_id,
    input  wire [63:0]             event_timestamp_ns,
    input  wire [63:0]             event_price,
    input  wire [31:0]             event_quantity,
    input  wire [7:0]              event_side,
    input  wire [31:0]             event_sequence,
    input  wire [15:0]             event_flags,

    output wire                    feature_valid,
    input  wire                    feature_ready,
    output wire [15:0]             feature_symbol_id,
    output wire [31:0]             feature_sequence,
    output wire [63:0]             feature_bid_price,
    output wire [31:0]             feature_bid_quantity,
    output wire [63:0]             feature_ask_price,
    output wire [31:0]             feature_ask_quantity,
    output wire [63:0]             feature_spread,
    output wire                    feature_spread_valid,
    output wire [63:0]             feature_midpoint,
    output wire                    feature_midpoint_valid,
    output wire signed [64:0]      feature_momentum,
    output wire                    feature_momentum_valid,
    output wire [TRADE_ACC_W-1:0]  feature_rolling_volume,
    output wire signed [32:0]      feature_imbalance_numerator,
    output wire [32:0]             feature_imbalance_denominator,
    output wire                    feature_imbalance_valid,
    output wire [VWAP_ACC_W-1:0]   feature_vwap_sum_price_quantity,
    output wire [VWAP_QTY_ACC_W-1:0] feature_vwap_sum_quantity,
    output wire                    feature_vwap_valid,
    output wire [63:0]              feature_vwap,
    output wire                    feature_vwap_quotient_valid,
    output wire signed [15:0]       feature_imbalance_normalized,
    output wire                    feature_imbalance_normalized_valid,
    output wire signed [31:0]       feature_spread_bps_x100,
    output wire                    feature_spread_bps_x100_valid,
    output wire signed [31:0]       feature_momentum_bps_x100,
    output wire                    feature_momentum_bps_x100_valid,
    output wire signed [64:0]       feature_midpoint_minus_vwap,
    output wire                    feature_midpoint_minus_vwap_valid,
    output wire signed [31:0]       feature_midpoint_minus_vwap_bps_x100,
    output wire                    feature_midpoint_minus_vwap_bps_x100_valid,

    // Internal synthesis visibility taps; these are not protocol outputs.
    output wire [31:0]             history_trade_probe,
    output wire [63:0]             history_midpoint_probe,
    output wire [95:0]             history_vwap_price_quantity_probe,
    output wire [31:0]             history_vwap_quantity_probe,

    output reg                     event_reject_pulse,
    output reg [7:0]               event_reject_reason,
    output reg [15:0]              event_reject_symbol_id,
    output reg [31:0]              event_reject_sequence
);
    localparam integer SYMBOL_ID_W = (NUM_SYMBOLS <= 1) ? 1 : $clog2(NUM_SYMBOLS);
    localparam integer TRADE_PTR_W = (TRADE_WINDOW <= 1) ? 1 : $clog2(TRADE_WINDOW);
    localparam integer TRADE_COUNT_W = (TRADE_WINDOW <= 1) ? 1 : $clog2(TRADE_WINDOW + 1);
    localparam integer MOMENTUM_COUNT_W = (MOMENTUM_WINDOW <= 1) ? 1 : $clog2(MOMENTUM_WINDOW + 1);
    localparam integer VWAP_PTR_W = (VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW);
    localparam integer VWAP_COUNT_W = (VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW + 1);

    // The selected-symbol transaction is serialized, so the per-symbol
    // working state can be stored as one packed word.  Keeping one logical
    // read/write bank avoids a NUM_SYMBOLS-wide register bank while leaving
    // the event schedule and all feature semantics unchanged.
    localparam integer STATE_BID_PRICE_LSB = 0;
    localparam integer STATE_BID_QUANTITY_LSB = STATE_BID_PRICE_LSB + 64;
    localparam integer STATE_ASK_PRICE_LSB = STATE_BID_QUANTITY_LSB + 32;
    localparam integer STATE_ASK_QUANTITY_LSB = STATE_ASK_PRICE_LSB + 64;
    localparam integer STATE_SEQUENCE_LSB = STATE_ASK_QUANTITY_LSB + 32;
    localparam integer STATE_SEQUENCE_VALID_LSB = STATE_SEQUENCE_LSB + 32;
    localparam integer STATE_BID_VALID_LSB = STATE_SEQUENCE_VALID_LSB + 1;
    localparam integer STATE_ASK_VALID_LSB = STATE_BID_VALID_LSB + 1;
    localparam integer STATE_ROLLING_VOLUME_LSB = STATE_ASK_VALID_LSB + 1;
    localparam integer STATE_TRADE_PTR_LSB = STATE_ROLLING_VOLUME_LSB + TRADE_ACC_W;
    localparam integer STATE_TRADE_COUNT_LSB = STATE_TRADE_PTR_LSB + TRADE_PTR_W;
    localparam integer STATE_MIDPOINT_PTR_LSB = STATE_TRADE_COUNT_LSB + TRADE_COUNT_W;
    localparam integer STATE_MIDPOINT_COUNT_LSB = STATE_MIDPOINT_PTR_LSB + MOMENTUM_PTR_W;
    localparam integer STATE_VWAP_SUM_PQ_LSB = STATE_MIDPOINT_COUNT_LSB + MOMENTUM_COUNT_W;
    localparam integer STATE_VWAP_SUM_Q_LSB = STATE_VWAP_SUM_PQ_LSB + VWAP_ACC_W;
    localparam integer STATE_VWAP_PTR_LSB = STATE_VWAP_SUM_Q_LSB + VWAP_QTY_ACC_W;
    localparam integer STATE_VWAP_COUNT_LSB = STATE_VWAP_PTR_LSB + VWAP_PTR_W;
    localparam integer STATE_BANK_W = STATE_VWAP_COUNT_LSB + VWAP_COUNT_W;

    localparam [4:0] IDLE           = 5'd0;
    localparam [4:0] READ_STATE     = 5'd1;
    localparam [4:0] CHECK_SEQUENCE = 5'd2;
    localparam [4:0] SET_HISTORY_ADDR = 5'd3;
    localparam [4:0] READ_HISTORY   = 5'd4;
    localparam [4:0] CAPTURE_HISTORY = 5'd5;
    localparam [4:0] MULTIPLY_TRADE = 5'd6;
    localparam [4:0] APPLY_EVENT    = 5'd7;
    localparam [4:0] FEATURE_CALC   = 5'd8;
    localparam [4:0] NORMALIZE_START = 5'd9;
    localparam [4:0] NORMALIZE_WAIT = 5'd10;
    localparam [4:0] WRITE_STATE    = 5'd11;
    localparam [4:0] WRITE_HISTORY  = 5'd12;
    localparam [4:0] OUTPUT         = 5'd13;

    reg [4:0] state = IDLE;

    // One packed logical word per symbol.  The validity bitmap gives reset
    // semantics without requiring a full-width memory clear, allowing the
    // bank to remain a compact implementation target on Gowin devices.
    (* keep = "true" *) reg [STATE_BANK_W-1:0] state_bank [0:NUM_SYMBOLS-1];
    reg state_initialized [0:NUM_SYMBOLS-1];

    // Banked circular histories. Named read signals are captured before
    // arithmetic, isolating the dynamic bank access from the feature cone.
    // Gowin reports these arrays as RAM extraction candidates, but the final
    // target mapping must be checked in the post-P&R resource report.
    (* keep = "true" *) reg [31:0] trade_quantity_history [0:NUM_SYMBOLS-1][0:TRADE_WINDOW-1];
    (* keep = "true" *) reg [63:0] midpoint_history [0:NUM_SYMBOLS-1][0:MOMENTUM_WINDOW-1];
    (* keep = "true" *) reg [95:0] vwap_price_quantity_history [0:NUM_SYMBOLS-1][0:VWAP_WINDOW-1];
    (* keep = "true" *) reg [31:0] vwap_quantity_history [0:NUM_SYMBOLS-1][0:VWAP_WINDOW-1];

    reg [15:0] event_symbol_id_reg;
    reg [SYMBOL_ID_W-1:0] event_symbol_index_reg;
    reg [7:0] event_type_reg;
    reg [63:0] event_price_reg;
    reg [31:0] event_quantity_reg;
    reg [7:0] event_side_reg;
    reg [31:0] event_sequence_reg;
    reg [15:0] event_flags_reg;
    reg [95:0] event_price_quantity_reg;

    // Registered working copy. All feature arithmetic below consumes these
    // registers, never a dynamic per-symbol array read in the same cycle.
    reg [63:0] work_bid_price;
    reg [31:0] work_bid_quantity;
    reg [63:0] work_ask_price;
    reg [31:0] work_ask_quantity;
    reg [31:0] work_last_sequence_number;
    reg work_sequence_valid;
    reg work_bid_valid;
    reg work_ask_valid;
    reg [TRADE_ACC_W-1:0] work_rolling_trade_volume;
    reg [TRADE_PTR_W-1:0] work_trade_write_ptr;
    reg [TRADE_COUNT_W-1:0] work_trade_valid_count;
    reg [MOMENTUM_PTR_W-1:0] work_midpoint_write_ptr;
    reg [MOMENTUM_COUNT_W-1:0] work_midpoint_valid_count;
    reg [VWAP_ACC_W-1:0] work_vwap_sum_price_quantity;
    reg [VWAP_QTY_ACC_W-1:0] work_vwap_sum_quantity;
    reg [VWAP_PTR_W-1:0] work_vwap_write_ptr;
    reg [VWAP_COUNT_W-1:0] work_vwap_valid_count;

    always @* begin
        work_state_word = {STATE_BANK_W{1'b0}};
        work_state_word[STATE_BID_PRICE_LSB +: 64] = work_bid_price;
        work_state_word[STATE_BID_QUANTITY_LSB +: 32] = work_bid_quantity;
        work_state_word[STATE_ASK_PRICE_LSB +: 64] = work_ask_price;
        work_state_word[STATE_ASK_QUANTITY_LSB +: 32] = work_ask_quantity;
        work_state_word[STATE_SEQUENCE_LSB +: 32] = work_last_sequence_number;
        work_state_word[STATE_SEQUENCE_VALID_LSB] = work_sequence_valid;
        work_state_word[STATE_BID_VALID_LSB] = work_bid_valid;
        work_state_word[STATE_ASK_VALID_LSB] = work_ask_valid;
        work_state_word[STATE_ROLLING_VOLUME_LSB +: TRADE_ACC_W] =
            work_rolling_trade_volume;
        work_state_word[STATE_TRADE_PTR_LSB +: TRADE_PTR_W] =
            work_trade_write_ptr;
        work_state_word[STATE_TRADE_COUNT_LSB +: TRADE_COUNT_W] =
            work_trade_valid_count;
        work_state_word[STATE_MIDPOINT_PTR_LSB +: MOMENTUM_PTR_W] =
            work_midpoint_write_ptr;
        work_state_word[STATE_MIDPOINT_COUNT_LSB +: MOMENTUM_COUNT_W] =
            work_midpoint_valid_count;
        work_state_word[STATE_VWAP_SUM_PQ_LSB +: VWAP_ACC_W] =
            work_vwap_sum_price_quantity;
        work_state_word[STATE_VWAP_SUM_Q_LSB +: VWAP_QTY_ACC_W] =
            work_vwap_sum_quantity;
        work_state_word[STATE_VWAP_PTR_LSB +: VWAP_PTR_W] =
            work_vwap_write_ptr;
        work_state_word[STATE_VWAP_COUNT_LSB +: VWAP_COUNT_W] =
            work_vwap_valid_count;
    end

    // Registered history read terms and their write addresses.
    reg [31:0] old_trade_quantity_reg;
    reg [63:0] old_midpoint_reg;
    reg [95:0] old_vwap_price_quantity_reg;
    reg [31:0] old_vwap_quantity_reg;
    reg [TRADE_PTR_W-1:0] trade_history_write_ptr_reg;
    reg [MOMENTUM_PTR_W-1:0] midpoint_history_write_ptr_reg;
    reg [VWAP_PTR_W-1:0] vwap_history_write_ptr_reg;
    reg [63:0] current_midpoint_reg;

    wire [STATE_BANK_W-1:0] selected_state_word =
        state_bank[event_symbol_index_reg];
    reg [STATE_BANK_W-1:0] work_state_word;

    wire quotes_valid;

    reg feature_valid_reg = 1'b0;
    reg [15:0] feature_symbol_id_reg;
    reg [31:0] feature_sequence_reg;
    reg [63:0] feature_bid_price_reg;
    reg [31:0] feature_bid_quantity_reg;
    reg [63:0] feature_ask_price_reg;
    reg [31:0] feature_ask_quantity_reg;
    reg [63:0] feature_spread_reg;
    reg feature_spread_valid_reg;
    reg [63:0] feature_midpoint_reg;
    reg feature_midpoint_valid_reg;
    reg signed [64:0] feature_momentum_reg;
    reg feature_momentum_valid_reg;
    reg [TRADE_ACC_W-1:0] feature_rolling_volume_reg;
    reg signed [32:0] feature_imbalance_numerator_reg;
    reg [32:0] feature_imbalance_denominator_reg;
    reg feature_imbalance_valid_reg;
    reg [VWAP_ACC_W-1:0] feature_vwap_sum_price_quantity_reg;
    reg [VWAP_QTY_ACC_W-1:0] feature_vwap_sum_quantity_reg;
    reg feature_vwap_valid_reg;
    reg [63:0] feature_vwap_reg;
    reg feature_vwap_quotient_valid_reg;
    reg signed [15:0] feature_imbalance_normalized_reg;
    reg feature_imbalance_normalized_valid_reg;
    reg signed [31:0] feature_spread_bps_x100_reg;
    reg feature_spread_bps_x100_valid_reg;
    reg signed [31:0] feature_momentum_bps_x100_reg;
    reg feature_momentum_bps_x100_valid_reg;
    reg signed [64:0] feature_midpoint_minus_vwap_reg;
    reg feature_midpoint_minus_vwap_valid_reg;
    reg signed [31:0] feature_midpoint_minus_vwap_bps_x100_reg;
    reg feature_midpoint_minus_vwap_bps_x100_valid_reg;

    wire event_accepted = event_valid && event_ready;
    wire event_symbol_valid = (event_symbol_id < NUM_SYMBOLS);
    wire [SYMBOL_ID_W-1:0] event_symbol_index = event_symbol_id[SYMBOL_ID_W-1:0];
    wire event_type_valid = (event_type == `MSG_MARKET_QUOTE) ||
                            (event_type == `MSG_MARKET_TRADE);
    wire event_side_valid = (event_side <= 8'd1);

    // These named bank reads are captured in CAPTURE_HISTORY. They retain the
    // vendor's memory inference pattern; all feature arithmetic consumes the
    // registered old_* working terms below rather than a live bank read.
    wire [31:0] trade_history_read =
        trade_quantity_history[event_symbol_index_reg][work_trade_write_ptr];
    wire [63:0] midpoint_history_read =
        midpoint_history[event_symbol_index_reg][work_midpoint_write_ptr];
    wire [95:0] vwap_price_quantity_history_read =
        vwap_price_quantity_history[event_symbol_index_reg][work_vwap_write_ptr];
    wire [31:0] vwap_quantity_history_read =
        vwap_quantity_history[event_symbol_index_reg][work_vwap_write_ptr];

    wire [TRADE_ACC_W-1:0] old_trade_quantity_ext =
        {{(TRADE_ACC_W-32){1'b0}}, old_trade_quantity_reg};
    wire [TRADE_ACC_W-1:0] event_quantity_ext =
        {{(TRADE_ACC_W-32){1'b0}}, event_quantity_reg};
    wire [TRADE_ACC_W-1:0] trade_volume_after_update =
        work_rolling_trade_volume -
        ((work_trade_valid_count >= TRADE_WINDOW) ?
            old_trade_quantity_ext : {TRADE_ACC_W{1'b0}}) + event_quantity_ext;

    wire [VWAP_ACC_W-1:0] old_vwap_price_quantity_ext =
        {{(VWAP_ACC_W-96){1'b0}}, old_vwap_price_quantity_reg};
    wire [VWAP_ACC_W-1:0] event_price_quantity_ext =
        {{(VWAP_ACC_W-96){1'b0}}, event_price_quantity_reg};
    wire [VWAP_QTY_ACC_W-1:0] old_vwap_quantity_ext =
        {{(VWAP_QTY_ACC_W-32){1'b0}}, old_vwap_quantity_reg};
    wire [VWAP_QTY_ACC_W-1:0] event_vwap_quantity_ext =
        {{(VWAP_QTY_ACC_W-32){1'b0}}, event_quantity_reg};
    wire [VWAP_ACC_W-1:0] vwap_price_quantity_after_update =
        work_vwap_sum_price_quantity -
        ((work_vwap_valid_count >= VWAP_WINDOW) ?
            old_vwap_price_quantity_ext : {VWAP_ACC_W{1'b0}}) +
        event_price_quantity_ext;
    wire [VWAP_QTY_ACC_W-1:0] vwap_quantity_after_update =
        work_vwap_sum_quantity -
        ((work_vwap_valid_count >= VWAP_WINDOW) ?
            old_vwap_quantity_ext : {VWAP_QTY_ACC_W{1'b0}}) +
        event_vwap_quantity_ext;

    assign quotes_valid = work_bid_valid && work_ask_valid &&
                          (work_ask_price >= work_bid_price);
    wire [64:0] quote_price_sum =
        {1'b0, work_bid_price} + {1'b0, work_ask_price};
    wire [63:0] current_midpoint = quote_price_sum[64:1];
    wire [63:0] current_spread = work_ask_price - work_bid_price;
    wire signed [64:0] momentum_delta =
        $signed({1'b0, current_midpoint}) - $signed({1'b0, old_midpoint_reg});
    wire signed [32:0] imbalance_numerator =
        $signed({1'b0, work_bid_quantity}) - $signed({1'b0, work_ask_quantity});
    wire [32:0] imbalance_denominator =
        {1'b0, work_bid_quantity} + {1'b0, work_ask_quantity};

    wire normalizer_start = (state == NORMALIZE_START);
    wire normalizer_done;
    wire [63:0] normalizer_vwap;
    wire normalizer_vwap_valid;
    wire signed [15:0] normalizer_imbalance_normalized;
    wire normalizer_imbalance_normalized_valid;
    wire signed [31:0] normalizer_spread_bps_x100;
    wire normalizer_spread_bps_x100_valid;
    wire signed [31:0] normalizer_momentum_bps_x100;
    wire normalizer_momentum_bps_x100_valid;
    wire signed [64:0] normalizer_midpoint_minus_vwap;
    wire normalizer_midpoint_minus_vwap_valid;
    wire signed [31:0] normalizer_midpoint_minus_vwap_bps_x100;
    wire normalizer_midpoint_minus_vwap_bps_x100_valid;

    feature_normalizer #(
        .VWAP_ACC_W(VWAP_ACC_W),
        .VWAP_QTY_ACC_W(VWAP_QTY_ACC_W),
        .BPS_SCALE(BPS_SCALE),
        .BPS_OUTPUT_SCALE(BPS_OUTPUT_SCALE),
        .IMBALANCE_FRAC_BITS(IMBALANCE_FRAC_BITS)
    ) feature_normalizer_i (
        .clk(clk),
        .reset_n(reset_n),
        .start(normalizer_start),
        .raw_spread(feature_spread_reg),
        .raw_spread_valid(feature_spread_valid_reg),
        .raw_midpoint(feature_midpoint_reg),
        .raw_midpoint_valid(feature_midpoint_valid_reg),
        .raw_momentum(feature_momentum_reg),
        .raw_momentum_valid(feature_momentum_valid_reg),
        .raw_momentum_reference(old_midpoint_reg),
        .raw_imbalance_numerator(feature_imbalance_numerator_reg),
        .raw_imbalance_denominator(feature_imbalance_denominator_reg),
        .raw_imbalance_valid(feature_imbalance_valid_reg),
        .raw_vwap_sum_price_quantity(feature_vwap_sum_price_quantity_reg),
        .raw_vwap_sum_quantity(feature_vwap_sum_quantity_reg),
        .raw_vwap_valid(feature_vwap_valid_reg),
        .done(normalizer_done),
        .vwap(normalizer_vwap),
        .vwap_valid(normalizer_vwap_valid),
        .imbalance_normalized(normalizer_imbalance_normalized),
        .imbalance_normalized_valid(normalizer_imbalance_normalized_valid),
        .spread_bps_x100(normalizer_spread_bps_x100),
        .spread_bps_x100_valid(normalizer_spread_bps_x100_valid),
        .momentum_bps_x100(normalizer_momentum_bps_x100),
        .momentum_bps_x100_valid(normalizer_momentum_bps_x100_valid),
        .midpoint_minus_vwap(normalizer_midpoint_minus_vwap),
        .midpoint_minus_vwap_valid(normalizer_midpoint_minus_vwap_valid),
        .midpoint_minus_vwap_bps_x100(normalizer_midpoint_minus_vwap_bps_x100),
        .midpoint_minus_vwap_bps_x100_valid(normalizer_midpoint_minus_vwap_bps_x100_valid)
    );

    assign event_ready = (state == IDLE) && !feature_valid_reg;
    assign feature_valid = feature_valid_reg;
    assign feature_symbol_id = feature_symbol_id_reg;
    assign feature_sequence = feature_sequence_reg;
    assign feature_bid_price = feature_bid_price_reg;
    assign feature_bid_quantity = feature_bid_quantity_reg;
    assign feature_ask_price = feature_ask_price_reg;
    assign feature_ask_quantity = feature_ask_quantity_reg;
    assign feature_spread = feature_spread_reg;
    assign feature_spread_valid = feature_spread_valid_reg;
    assign feature_midpoint = feature_midpoint_reg;
    assign feature_midpoint_valid = feature_midpoint_valid_reg;
    assign feature_momentum = feature_momentum_reg;
    assign feature_momentum_valid = feature_momentum_valid_reg;
    assign feature_rolling_volume = feature_rolling_volume_reg;
    assign feature_imbalance_numerator = feature_imbalance_numerator_reg;
    assign feature_imbalance_denominator = feature_imbalance_denominator_reg;
    assign feature_imbalance_valid = feature_imbalance_valid_reg;
    assign feature_vwap_sum_price_quantity = feature_vwap_sum_price_quantity_reg;
    assign feature_vwap_sum_quantity = feature_vwap_sum_quantity_reg;
    assign feature_vwap_valid = feature_vwap_valid_reg;
    assign feature_vwap = feature_vwap_reg;
    assign feature_vwap_quotient_valid = feature_vwap_quotient_valid_reg;
    assign feature_imbalance_normalized = feature_imbalance_normalized_reg;
    assign feature_imbalance_normalized_valid = feature_imbalance_normalized_valid_reg;
    assign feature_spread_bps_x100 = feature_spread_bps_x100_reg;
    assign feature_spread_bps_x100_valid = feature_spread_bps_x100_valid_reg;
    assign feature_momentum_bps_x100 = feature_momentum_bps_x100_reg;
    assign feature_momentum_bps_x100_valid = feature_momentum_bps_x100_valid_reg;
    assign feature_midpoint_minus_vwap = feature_midpoint_minus_vwap_reg;
    assign feature_midpoint_minus_vwap_valid = feature_midpoint_minus_vwap_valid_reg;
    assign feature_midpoint_minus_vwap_bps_x100 = feature_midpoint_minus_vwap_bps_x100_reg;
    assign feature_midpoint_minus_vwap_bps_x100_valid = feature_midpoint_minus_vwap_bps_x100_valid_reg;
    assign history_trade_probe = trade_history_read;
    assign history_midpoint_probe = midpoint_history_read;
    assign history_vwap_price_quantity_probe = vwap_price_quantity_history_read;
    assign history_vwap_quantity_probe = vwap_quantity_history_read;

    integer symbol_index;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            state <= IDLE;
            event_symbol_id_reg <= 16'd0;
            event_symbol_index_reg <= {SYMBOL_ID_W{1'b0}};
            event_type_reg <= 8'd0;
            event_price_reg <= 64'd0;
            event_quantity_reg <= 32'd0;
            event_side_reg <= 8'd0;
            event_sequence_reg <= 32'd0;
            event_flags_reg <= 16'd0;
            event_price_quantity_reg <= 96'd0;
            old_trade_quantity_reg <= 32'd0;
            old_midpoint_reg <= 64'd0;
            old_vwap_price_quantity_reg <= 96'd0;
            old_vwap_quantity_reg <= 32'd0;
            trade_history_write_ptr_reg <= {TRADE_PTR_W{1'b0}};
            midpoint_history_write_ptr_reg <= {MOMENTUM_PTR_W{1'b0}};
            vwap_history_write_ptr_reg <= {VWAP_PTR_W{1'b0}};
            current_midpoint_reg <= 64'd0;
            work_bid_price <= 64'd0;
            work_bid_quantity <= 32'd0;
            work_ask_price <= 64'd0;
            work_ask_quantity <= 32'd0;
            work_last_sequence_number <= 32'd0;
            work_sequence_valid <= 1'b0;
            work_bid_valid <= 1'b0;
            work_ask_valid <= 1'b0;
            work_rolling_trade_volume <= {TRADE_ACC_W{1'b0}};
            work_trade_write_ptr <= {TRADE_PTR_W{1'b0}};
            work_trade_valid_count <= {TRADE_COUNT_W{1'b0}};
            work_midpoint_write_ptr <= {MOMENTUM_PTR_W{1'b0}};
            work_midpoint_valid_count <= {MOMENTUM_COUNT_W{1'b0}};
            work_vwap_sum_price_quantity <= {VWAP_ACC_W{1'b0}};
            work_vwap_sum_quantity <= {VWAP_QTY_ACC_W{1'b0}};
            work_vwap_write_ptr <= {VWAP_PTR_W{1'b0}};
            work_vwap_valid_count <= {VWAP_COUNT_W{1'b0}};
            feature_valid_reg <= 1'b0;
            feature_symbol_id_reg <= 16'd0;
            feature_sequence_reg <= 32'd0;
            feature_bid_price_reg <= 64'd0;
            feature_bid_quantity_reg <= 32'd0;
            feature_ask_price_reg <= 64'd0;
            feature_ask_quantity_reg <= 32'd0;
            feature_spread_reg <= 64'd0;
            feature_spread_valid_reg <= 1'b0;
            feature_midpoint_reg <= 64'd0;
            feature_midpoint_valid_reg <= 1'b0;
            feature_momentum_reg <= 65'sd0;
            feature_momentum_valid_reg <= 1'b0;
            feature_rolling_volume_reg <= {TRADE_ACC_W{1'b0}};
            feature_imbalance_numerator_reg <= 33'sd0;
            feature_imbalance_denominator_reg <= 33'd0;
            feature_imbalance_valid_reg <= 1'b0;
            feature_vwap_sum_price_quantity_reg <= {VWAP_ACC_W{1'b0}};
            feature_vwap_sum_quantity_reg <= {VWAP_QTY_ACC_W{1'b0}};
            feature_vwap_valid_reg <= 1'b0;
            feature_vwap_reg <= 64'd0;
            feature_vwap_quotient_valid_reg <= 1'b0;
            feature_imbalance_normalized_reg <= 16'sd0;
            feature_imbalance_normalized_valid_reg <= 1'b0;
            feature_spread_bps_x100_reg <= 32'sd0;
            feature_spread_bps_x100_valid_reg <= 1'b0;
            feature_momentum_bps_x100_reg <= 32'sd0;
            feature_momentum_bps_x100_valid_reg <= 1'b0;
            feature_midpoint_minus_vwap_reg <= 65'sd0;
            feature_midpoint_minus_vwap_valid_reg <= 1'b0;
            feature_midpoint_minus_vwap_bps_x100_reg <= 32'sd0;
            feature_midpoint_minus_vwap_bps_x100_valid_reg <= 1'b0;
            event_reject_pulse <= 1'b0;
            event_reject_reason <= 8'h00;
            event_reject_symbol_id <= 16'd0;
            event_reject_sequence <= 32'd0;

            for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1) begin
                // Logical reset is represented by the bitmap; stale RAM
                // words are ignored until their symbol is written again.
                state_initialized[symbol_index] <= 1'b0;
            end
        end else begin
            event_reject_pulse <= 1'b0;

            case (state)
            IDLE: begin
                if (event_accepted) begin
                    event_reject_symbol_id <= event_symbol_id;
                    event_reject_sequence <= event_sequence;

                    if (!event_symbol_valid) begin
                        event_reject_reason <= `STATUS_BAD_SYMBOL;
                        event_reject_pulse <= 1'b1;
                    end else if (!event_type_valid) begin
                        event_reject_reason <= `STATUS_BAD_TYPE;
                        event_reject_pulse <= 1'b1;
                    end else if (!event_side_valid) begin
                        event_reject_reason <= `STATUS_BAD_SIDE;
                        event_reject_pulse <= 1'b1;
                    end else begin
                        // The one-hot decode is itself registered. Later
                        // state writes therefore do not decode packet bits
                        // directly into NUM_SYMBOLS bank enables.
                        event_symbol_id_reg <= event_symbol_id;
                        event_symbol_index_reg <= event_symbol_index;
                        event_type_reg <= event_type;
                        event_price_reg <= event_price;
                        event_quantity_reg <= event_quantity;
                        event_side_reg <= event_side;
                        event_sequence_reg <= event_sequence;
                        event_flags_reg <= event_flags;
                        state <= READ_STATE;
                    end
                end
            end

            READ_STATE: begin
                // One dynamic packed-bank read is isolated from feature
                // arithmetic.  Uninitialized words are logically zero after
                // reset, as indicated by the per-symbol bitmap.
                if (state_initialized[event_symbol_index_reg]) begin
                    work_bid_price <= selected_state_word[STATE_BID_PRICE_LSB +: 64];
                    work_bid_quantity <= selected_state_word[STATE_BID_QUANTITY_LSB +: 32];
                    work_ask_price <= selected_state_word[STATE_ASK_PRICE_LSB +: 64];
                    work_ask_quantity <= selected_state_word[STATE_ASK_QUANTITY_LSB +: 32];
                    work_last_sequence_number <= selected_state_word[STATE_SEQUENCE_LSB +: 32];
                    work_sequence_valid <= selected_state_word[STATE_SEQUENCE_VALID_LSB];
                    work_bid_valid <= selected_state_word[STATE_BID_VALID_LSB];
                    work_ask_valid <= selected_state_word[STATE_ASK_VALID_LSB];
                    work_rolling_trade_volume <=
                        selected_state_word[STATE_ROLLING_VOLUME_LSB +: TRADE_ACC_W];
                    work_trade_write_ptr <=
                        selected_state_word[STATE_TRADE_PTR_LSB +: TRADE_PTR_W];
                    work_trade_valid_count <=
                        selected_state_word[STATE_TRADE_COUNT_LSB +: TRADE_COUNT_W];
                    work_midpoint_write_ptr <=
                        selected_state_word[STATE_MIDPOINT_PTR_LSB +: MOMENTUM_PTR_W];
                    work_midpoint_valid_count <=
                        selected_state_word[STATE_MIDPOINT_COUNT_LSB +: MOMENTUM_COUNT_W];
                    work_vwap_sum_price_quantity <=
                        selected_state_word[STATE_VWAP_SUM_PQ_LSB +: VWAP_ACC_W];
                    work_vwap_sum_quantity <=
                        selected_state_word[STATE_VWAP_SUM_Q_LSB +: VWAP_QTY_ACC_W];
                    work_vwap_write_ptr <=
                        selected_state_word[STATE_VWAP_PTR_LSB +: VWAP_PTR_W];
                    work_vwap_valid_count <=
                        selected_state_word[STATE_VWAP_COUNT_LSB +: VWAP_COUNT_W];
                end else begin
                    work_bid_price <= 64'd0;
                    work_bid_quantity <= 32'd0;
                    work_ask_price <= 64'd0;
                    work_ask_quantity <= 32'd0;
                    work_last_sequence_number <= 32'd0;
                    work_sequence_valid <= 1'b0;
                    work_bid_valid <= 1'b0;
                    work_ask_valid <= 1'b0;
                    work_rolling_trade_volume <= {TRADE_ACC_W{1'b0}};
                    work_trade_write_ptr <= {TRADE_PTR_W{1'b0}};
                    work_trade_valid_count <= {TRADE_COUNT_W{1'b0}};
                    work_midpoint_write_ptr <= {MOMENTUM_PTR_W{1'b0}};
                    work_midpoint_valid_count <= {MOMENTUM_COUNT_W{1'b0}};
                    work_vwap_sum_price_quantity <= {VWAP_ACC_W{1'b0}};
                    work_vwap_sum_quantity <= {VWAP_QTY_ACC_W{1'b0}};
                    work_vwap_write_ptr <= {VWAP_PTR_W{1'b0}};
                    work_vwap_valid_count <= {VWAP_COUNT_W{1'b0}};
                end
                state <= CHECK_SEQUENCE;
            end

            CHECK_SEQUENCE: begin
                if (work_sequence_valid &&
                    (event_sequence_reg <= work_last_sequence_number)) begin
                    event_reject_reason <=
                        (event_sequence_reg == work_last_sequence_number) ?
                        `STATUS_DUPLICATE_SEQ : `STATUS_STALE_SEQ;
                    event_reject_pulse <= 1'b1;
                    state <= IDLE;
                end else begin
                    state <= SET_HISTORY_ADDR;
                end
            end

            SET_HISTORY_ADDR: begin
                trade_history_write_ptr_reg <= work_trade_write_ptr;
                midpoint_history_write_ptr_reg <= work_midpoint_write_ptr;
                vwap_history_write_ptr_reg <= work_vwap_write_ptr;
                state <= READ_HISTORY;
            end

            READ_HISTORY: begin
                // The RAM instances sample the registered addresses here.
                state <= CAPTURE_HISTORY;
            end

            CAPTURE_HISTORY: begin
                old_trade_quantity_reg <= trade_history_read;
                old_midpoint_reg <= midpoint_history_read;
                old_vwap_price_quantity_reg <= vwap_price_quantity_history_read;
                old_vwap_quantity_reg <= vwap_quantity_history_read;
                if (event_type_reg == `MSG_MARKET_TRADE)
                    state <= MULTIPLY_TRADE;
                else
                    state <= APPLY_EVENT;
            end

            MULTIPLY_TRADE: begin
                // Exactly one event-serialized 64x32 product is shared by
                // all symbols; there are no per-symbol multiplier engines.
                event_price_quantity_reg <=
                    {32'd0, event_price_reg} * {64'd0, event_quantity_reg};
                state <= APPLY_EVENT;
            end

            APPLY_EVENT: begin
                work_last_sequence_number <= event_sequence_reg;
                work_sequence_valid <= 1'b1;

                if (event_type_reg == `MSG_MARKET_QUOTE) begin
                    if (event_side_reg == 8'd0) begin
                        work_bid_price <= event_price_reg;
                        work_bid_quantity <= event_quantity_reg;
                        work_bid_valid <= 1'b1;
                    end else begin
                        work_ask_price <= event_price_reg;
                        work_ask_quantity <= event_quantity_reg;
                        work_ask_valid <= 1'b1;
                    end
                end else begin
                    work_rolling_trade_volume <= trade_volume_after_update;
                    work_trade_write_ptr <=
                        (work_trade_write_ptr == TRADE_WINDOW - 1) ?
                        {TRADE_PTR_W{1'b0}} : work_trade_write_ptr + 1'b1;
                    if (work_trade_valid_count < TRADE_WINDOW)
                        work_trade_valid_count <= work_trade_valid_count + 1'b1;
                    work_vwap_sum_price_quantity <= vwap_price_quantity_after_update;
                    work_vwap_sum_quantity <= vwap_quantity_after_update;
                    work_vwap_write_ptr <=
                        (work_vwap_write_ptr == VWAP_WINDOW - 1) ?
                        {VWAP_PTR_W{1'b0}} : work_vwap_write_ptr + 1'b1;
                    if (work_vwap_valid_count < VWAP_WINDOW)
                        work_vwap_valid_count <= work_vwap_valid_count + 1'b1;
                end
                state <= FEATURE_CALC;
            end

            FEATURE_CALC: begin
                feature_symbol_id_reg <= event_symbol_id_reg;
                feature_sequence_reg <= event_sequence_reg;
                feature_bid_price_reg <= work_bid_price;
                feature_bid_quantity_reg <= work_bid_quantity;
                feature_ask_price_reg <= work_ask_price;
                feature_ask_quantity_reg <= work_ask_quantity;
                feature_spread_valid_reg <= quotes_valid;
                feature_spread_reg <= quotes_valid ? current_spread : 64'd0;
                feature_midpoint_valid_reg <= quotes_valid;
                feature_midpoint_reg <= quotes_valid ? current_midpoint : 64'd0;
                feature_momentum_valid_reg <=
                    quotes_valid && (work_midpoint_valid_count >= MOMENTUM_WINDOW);
                feature_momentum_reg <=
                    (quotes_valid && (work_midpoint_valid_count >= MOMENTUM_WINDOW)) ?
                    momentum_delta : 65'sd0;
                feature_rolling_volume_reg <= work_rolling_trade_volume;
                feature_imbalance_valid_reg <= quotes_valid;
                feature_imbalance_numerator_reg <=
                    quotes_valid ? imbalance_numerator : 33'sd0;
                feature_imbalance_denominator_reg <=
                    quotes_valid ? imbalance_denominator : 33'd0;
                feature_vwap_sum_price_quantity_reg <= work_vwap_sum_price_quantity;
                feature_vwap_sum_quantity_reg <= work_vwap_sum_quantity;
                feature_vwap_valid_reg <=
                    (work_vwap_sum_quantity != {VWAP_QTY_ACC_W{1'b0}});
                current_midpoint_reg <= current_midpoint;

                if (quotes_valid) begin
                    work_midpoint_write_ptr <=
                        (work_midpoint_write_ptr == MOMENTUM_WINDOW - 1) ?
                        {MOMENTUM_PTR_W{1'b0}} : work_midpoint_write_ptr + 1'b1;
                    if (work_midpoint_valid_count < MOMENTUM_WINDOW)
                        work_midpoint_valid_count <= work_midpoint_valid_count + 1'b1;
                end
                state <= NORMALIZE_START;
            end

            NORMALIZE_START: begin
                state <= NORMALIZE_WAIT;
            end

            NORMALIZE_WAIT: begin
                if (normalizer_done) begin
                    feature_vwap_reg <= normalizer_vwap;
                    feature_vwap_quotient_valid_reg <= normalizer_vwap_valid;
                    feature_imbalance_normalized_reg <= normalizer_imbalance_normalized;
                    feature_imbalance_normalized_valid_reg <=
                        normalizer_imbalance_normalized_valid;
                    feature_spread_bps_x100_reg <= normalizer_spread_bps_x100;
                    feature_spread_bps_x100_valid_reg <= normalizer_spread_bps_x100_valid;
                    feature_momentum_bps_x100_reg <= normalizer_momentum_bps_x100;
                    feature_momentum_bps_x100_valid_reg <= normalizer_momentum_bps_x100_valid;
                    feature_midpoint_minus_vwap_reg <= normalizer_midpoint_minus_vwap;
                    feature_midpoint_minus_vwap_valid_reg <= normalizer_midpoint_minus_vwap_valid;
                    feature_midpoint_minus_vwap_bps_x100_reg <=
                        normalizer_midpoint_minus_vwap_bps_x100;
                    feature_midpoint_minus_vwap_bps_x100_valid_reg <=
                        normalizer_midpoint_minus_vwap_bps_x100_valid;
                    state <= WRITE_STATE;
                end
            end

            WRITE_STATE: begin
                // The address is valid and registered before the write, so
                // the whole selected-symbol word can use one memory write.
                state_bank[event_symbol_index_reg] <= work_state_word;
                state_initialized[event_symbol_index_reg] <= 1'b1;
                state <= WRITE_HISTORY;
            end

            WRITE_HISTORY: begin
                if (event_type_reg == `MSG_MARKET_TRADE) begin
                    trade_quantity_history[event_symbol_index_reg][trade_history_write_ptr_reg] <=
                        event_quantity_reg;
                    vwap_price_quantity_history[event_symbol_index_reg][vwap_history_write_ptr_reg] <=
                        event_price_quantity_reg;
                    vwap_quantity_history[event_symbol_index_reg][vwap_history_write_ptr_reg] <=
                        event_quantity_reg;
                end
                if (quotes_valid)
                    midpoint_history[event_symbol_index_reg][midpoint_history_write_ptr_reg] <=
                        current_midpoint_reg;
                state <= OUTPUT;
            end

            OUTPUT: begin
                // Assert valid only after all state/history writes have
                // committed. This keeps a ready consumer from observing the
                // same record on multiple cycles while still holding it
                // indefinitely when feature_ready is low.
                if (!feature_valid_reg) begin
                    feature_valid_reg <= 1'b1;
                end else if (feature_ready) begin
                    feature_valid_reg <= 1'b0;
                    state <= IDLE;
                end
            end

            default: state <= IDLE;
            endcase
        end
    end
endmodule
