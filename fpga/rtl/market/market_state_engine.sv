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
    parameter integer VWAP_QTY_ACC_W = 32 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW))
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

    localparam [3:0] IDLE           = 4'd0;
    localparam [3:0] READ_STATE     = 4'd1;
    localparam [3:0] CHECK_SEQUENCE = 4'd2;
    localparam [3:0] SET_HISTORY_ADDR = 4'd3;
    localparam [3:0] READ_HISTORY   = 4'd4;
    localparam [3:0] CAPTURE_HISTORY = 4'd5;
    localparam [3:0] MULTIPLY_TRADE = 4'd6;
    localparam [3:0] APPLY_EVENT    = 4'd7;
    localparam [3:0] FEATURE_CALC   = 4'd8;
    localparam [3:0] WRITE_STATE    = 4'd9;
    localparam [3:0] WRITE_HISTORY  = 4'd10;
    localparam [3:0] OUTPUT         = 4'd11;

    reg [3:0] state = IDLE;

    // Per-symbol state bank. These values are intentionally registers: the
    // working read/modify/write transaction needs all fields together, while
    // the larger circular histories below remain separate inference targets.
    reg [63:0] best_bid_price [0:NUM_SYMBOLS-1];
    reg [31:0] best_bid_quantity [0:NUM_SYMBOLS-1];
    reg [63:0] best_ask_price [0:NUM_SYMBOLS-1];
    reg [31:0] best_ask_quantity [0:NUM_SYMBOLS-1];
    reg [63:0] last_trade_price [0:NUM_SYMBOLS-1];
    reg [31:0] last_trade_quantity [0:NUM_SYMBOLS-1];
    reg [7:0] last_trade_side [0:NUM_SYMBOLS-1];
    reg [63:0] last_timestamp_ns [0:NUM_SYMBOLS-1];
    reg [31:0] last_sequence_number [0:NUM_SYMBOLS-1];
    reg sequence_valid [0:NUM_SYMBOLS-1];
    reg bid_valid [0:NUM_SYMBOLS-1];
    reg ask_valid [0:NUM_SYMBOLS-1];
    reg trade_valid [0:NUM_SYMBOLS-1];

    reg [TRADE_ACC_W-1:0] rolling_trade_volume [0:NUM_SYMBOLS-1];
    reg [TRADE_PTR_W-1:0] trade_write_ptr [0:NUM_SYMBOLS-1];
    reg [TRADE_COUNT_W-1:0] trade_valid_count [0:NUM_SYMBOLS-1];
    reg [MOMENTUM_PTR_W-1:0] midpoint_write_ptr [0:NUM_SYMBOLS-1];
    reg [MOMENTUM_COUNT_W-1:0] midpoint_valid_count [0:NUM_SYMBOLS-1];
    reg [VWAP_ACC_W-1:0] vwap_sum_price_quantity [0:NUM_SYMBOLS-1];
    reg [VWAP_QTY_ACC_W-1:0] vwap_sum_quantity [0:NUM_SYMBOLS-1];
    reg [VWAP_PTR_W-1:0] vwap_write_ptr [0:NUM_SYMBOLS-1];
    reg [VWAP_COUNT_W-1:0] vwap_valid_count [0:NUM_SYMBOLS-1];

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
    reg [NUM_SYMBOLS-1:0] event_symbol_onehot_reg;
    reg [7:0] event_type_reg;
    reg [63:0] event_timestamp_reg;
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
    reg [63:0] work_last_trade_price;
    reg [31:0] work_last_trade_quantity;
    reg [7:0] work_last_trade_side;
    reg [63:0] work_last_timestamp_ns;
    reg [31:0] work_last_sequence_number;
    reg work_sequence_valid;
    reg work_bid_valid;
    reg work_ask_valid;
    reg work_trade_valid;
    reg [TRADE_ACC_W-1:0] work_rolling_trade_volume;
    reg [TRADE_PTR_W-1:0] work_trade_write_ptr;
    reg [TRADE_COUNT_W-1:0] work_trade_valid_count;
    reg [MOMENTUM_PTR_W-1:0] work_midpoint_write_ptr;
    reg [MOMENTUM_COUNT_W-1:0] work_midpoint_valid_count;
    reg [VWAP_ACC_W-1:0] work_vwap_sum_price_quantity;
    reg [VWAP_QTY_ACC_W-1:0] work_vwap_sum_quantity;
    reg [VWAP_PTR_W-1:0] work_vwap_write_ptr;
    reg [VWAP_COUNT_W-1:0] work_vwap_valid_count;

    // Registered history read terms and their write addresses.
    reg [31:0] old_trade_quantity_reg;
    reg [63:0] old_midpoint_reg;
    reg [95:0] old_vwap_price_quantity_reg;
    reg [31:0] old_vwap_quantity_reg;
    reg [TRADE_PTR_W-1:0] trade_history_write_ptr_reg;
    reg [MOMENTUM_PTR_W-1:0] midpoint_history_write_ptr_reg;
    reg [VWAP_PTR_W-1:0] vwap_history_write_ptr_reg;
    reg [63:0] current_midpoint_reg;

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
            event_symbol_onehot_reg <= {NUM_SYMBOLS{1'b0}};
            event_type_reg <= 8'd0;
            event_timestamp_reg <= 64'd0;
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
            work_last_trade_price <= 64'd0;
            work_last_trade_quantity <= 32'd0;
            work_last_trade_side <= 8'd0;
            work_last_timestamp_ns <= 64'd0;
            work_last_sequence_number <= 32'd0;
            work_sequence_valid <= 1'b0;
            work_bid_valid <= 1'b0;
            work_ask_valid <= 1'b0;
            work_trade_valid <= 1'b0;
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
            event_reject_pulse <= 1'b0;
            event_reject_reason <= 8'h00;
            event_reject_symbol_id <= 16'd0;
            event_reject_sequence <= 32'd0;

            for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1) begin
                best_bid_price[symbol_index] <= 64'd0;
                best_bid_quantity[symbol_index] <= 32'd0;
                best_ask_price[symbol_index] <= 64'd0;
                best_ask_quantity[symbol_index] <= 32'd0;
                last_trade_price[symbol_index] <= 64'd0;
                last_trade_quantity[symbol_index] <= 32'd0;
                last_trade_side[symbol_index] <= 8'd0;
                last_timestamp_ns[symbol_index] <= 64'd0;
                last_sequence_number[symbol_index] <= 32'd0;
                sequence_valid[symbol_index] <= 1'b0;
                bid_valid[symbol_index] <= 1'b0;
                ask_valid[symbol_index] <= 1'b0;
                trade_valid[symbol_index] <= 1'b0;
                rolling_trade_volume[symbol_index] <= {TRADE_ACC_W{1'b0}};
                trade_write_ptr[symbol_index] <= {TRADE_PTR_W{1'b0}};
                trade_valid_count[symbol_index] <= {TRADE_COUNT_W{1'b0}};
                midpoint_write_ptr[symbol_index] <= {MOMENTUM_PTR_W{1'b0}};
                midpoint_valid_count[symbol_index] <= {MOMENTUM_COUNT_W{1'b0}};
                vwap_sum_price_quantity[symbol_index] <= {VWAP_ACC_W{1'b0}};
                vwap_sum_quantity[symbol_index] <= {VWAP_QTY_ACC_W{1'b0}};
                vwap_write_ptr[symbol_index] <= {VWAP_PTR_W{1'b0}};
                vwap_valid_count[symbol_index] <= {VWAP_COUNT_W{1'b0}};
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
                        event_symbol_onehot_reg <=
                            ({{(NUM_SYMBOLS-1){1'b0}}, 1'b1} << event_symbol_index);
                        event_type_reg <= event_type;
                        event_timestamp_reg <= event_timestamp_ns;
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
                // One dynamic bank read is isolated from feature arithmetic.
                work_bid_price <= best_bid_price[event_symbol_index_reg];
                work_bid_quantity <= best_bid_quantity[event_symbol_index_reg];
                work_ask_price <= best_ask_price[event_symbol_index_reg];
                work_ask_quantity <= best_ask_quantity[event_symbol_index_reg];
                work_last_trade_price <= last_trade_price[event_symbol_index_reg];
                work_last_trade_quantity <= last_trade_quantity[event_symbol_index_reg];
                work_last_trade_side <= last_trade_side[event_symbol_index_reg];
                work_last_timestamp_ns <= last_timestamp_ns[event_symbol_index_reg];
                work_last_sequence_number <= last_sequence_number[event_symbol_index_reg];
                work_sequence_valid <= sequence_valid[event_symbol_index_reg];
                work_bid_valid <= bid_valid[event_symbol_index_reg];
                work_ask_valid <= ask_valid[event_symbol_index_reg];
                work_trade_valid <= trade_valid[event_symbol_index_reg];
                work_rolling_trade_volume <= rolling_trade_volume[event_symbol_index_reg];
                work_trade_write_ptr <= trade_write_ptr[event_symbol_index_reg];
                work_trade_valid_count <= trade_valid_count[event_symbol_index_reg];
                work_midpoint_write_ptr <= midpoint_write_ptr[event_symbol_index_reg];
                work_midpoint_valid_count <= midpoint_valid_count[event_symbol_index_reg];
                work_vwap_sum_price_quantity <= vwap_sum_price_quantity[event_symbol_index_reg];
                work_vwap_sum_quantity <= vwap_sum_quantity[event_symbol_index_reg];
                work_vwap_write_ptr <= vwap_write_ptr[event_symbol_index_reg];
                work_vwap_valid_count <= vwap_valid_count[event_symbol_index_reg];
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
                work_last_timestamp_ns <= event_timestamp_reg;

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
                    work_last_trade_price <= event_price_reg;
                    work_last_trade_quantity <= event_quantity_reg;
                    work_last_trade_side <= event_side_reg;
                    work_trade_valid <= 1'b1;
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
                state <= WRITE_STATE;
            end

            WRITE_STATE: begin
                // The symbol select is a registered one-hot value. This is a
                // deliberate pipeline boundary for the NUM_SYMBOLS bank.
                for (symbol_index = 0; symbol_index < NUM_SYMBOLS; symbol_index = symbol_index + 1) begin
                    if (event_symbol_onehot_reg[symbol_index]) begin
                        best_bid_price[symbol_index] <= work_bid_price;
                        best_bid_quantity[symbol_index] <= work_bid_quantity;
                        best_ask_price[symbol_index] <= work_ask_price;
                        best_ask_quantity[symbol_index] <= work_ask_quantity;
                        last_trade_price[symbol_index] <= work_last_trade_price;
                        last_trade_quantity[symbol_index] <= work_last_trade_quantity;
                        last_trade_side[symbol_index] <= work_last_trade_side;
                        last_timestamp_ns[symbol_index] <= work_last_timestamp_ns;
                        last_sequence_number[symbol_index] <= work_last_sequence_number;
                        sequence_valid[symbol_index] <= work_sequence_valid;
                        bid_valid[symbol_index] <= work_bid_valid;
                        ask_valid[symbol_index] <= work_ask_valid;
                        trade_valid[symbol_index] <= work_trade_valid;
                        rolling_trade_volume[symbol_index] <= work_rolling_trade_volume;
                        trade_write_ptr[symbol_index] <= work_trade_write_ptr;
                        trade_valid_count[symbol_index] <= work_trade_valid_count;
                        midpoint_write_ptr[symbol_index] <= work_midpoint_write_ptr;
                        midpoint_valid_count[symbol_index] <= work_midpoint_valid_count;
                        vwap_sum_price_quantity[symbol_index] <= work_vwap_sum_price_quantity;
                        vwap_sum_quantity[symbol_index] <= work_vwap_sum_quantity;
                        vwap_write_ptr[symbol_index] <= work_vwap_write_ptr;
                        vwap_valid_count[symbol_index] <= work_vwap_valid_count;
                    end
                end
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
