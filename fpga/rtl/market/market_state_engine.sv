`timescale 1ns/1ps

`include "protocol_defs.svh"

// Parameterized per-symbol market-state and feature engine.
//
// The engine is entirely in the clk27 domain and communicates with the
// dispatcher using a flat valid/ready event interface. Histories are indexed
// by symbol and use circular pointers; reset clears only control/valid state,
// so the history memories remain inference-friendly.
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

    localparam [2:0] IDLE          = 3'd0;
    localparam [2:0] UPDATE_QUOTE  = 3'd1;
    localparam [2:0] UPDATE_TRADE  = 3'd2;
    localparam [2:0] FEATURE_CALC  = 3'd3;
    localparam [2:0] OUTPUT        = 3'd4;

    reg [2:0] state = IDLE;

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
    reg [31:0] trade_quantity_history [0:NUM_SYMBOLS-1][0:TRADE_WINDOW-1];
    reg [TRADE_PTR_W-1:0] trade_write_ptr [0:NUM_SYMBOLS-1];
    reg [TRADE_COUNT_W-1:0] trade_valid_count [0:NUM_SYMBOLS-1];

    reg [63:0] midpoint_history [0:NUM_SYMBOLS-1][0:MOMENTUM_WINDOW-1];
    reg [MOMENTUM_PTR_W-1:0] midpoint_write_ptr [0:NUM_SYMBOLS-1];
    reg [MOMENTUM_COUNT_W-1:0] midpoint_valid_count [0:NUM_SYMBOLS-1];

    reg [95:0] vwap_price_quantity_history [0:NUM_SYMBOLS-1][0:VWAP_WINDOW-1];
    reg [31:0] vwap_quantity_history [0:NUM_SYMBOLS-1][0:VWAP_WINDOW-1];
    reg [VWAP_ACC_W-1:0] vwap_sum_price_quantity [0:NUM_SYMBOLS-1];
    reg [VWAP_QTY_ACC_W-1:0] vwap_sum_quantity [0:NUM_SYMBOLS-1];
    reg [VWAP_PTR_W-1:0] vwap_write_ptr [0:NUM_SYMBOLS-1];
    reg [VWAP_COUNT_W-1:0] vwap_valid_count [0:NUM_SYMBOLS-1];

    reg [15:0] event_symbol_id_reg;
    reg [SYMBOL_ID_W-1:0] event_symbol_index_reg;
    reg [63:0] event_timestamp_reg;
    reg [63:0] event_price_reg;
    reg [31:0] event_quantity_reg;
    reg [7:0] event_side_reg;
    reg [31:0] event_sequence_reg;
    reg [15:0] event_flags_reg;

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

    // Protect the state-array reads with the symbol guard. The dispatcher
    // normally filters this case, but the engine remains safe as a standalone
    // normalized-event endpoint too.
    wire sequence_valid_for_event = event_symbol_valid ?
                                    sequence_valid[event_symbol_index] : 1'b0;
    wire [31:0] last_sequence_for_event = event_symbol_valid ?
                                           last_sequence_number[event_symbol_index] : 32'd0;
    wire sequence_is_stale = sequence_valid_for_event &&
                             (event_sequence <= last_sequence_for_event);
    wire sequence_is_duplicate = sequence_is_stale &&
                                 (event_sequence == last_sequence_for_event);

    wire trade_window_full = (trade_valid_count[event_symbol_index_reg] >= TRADE_WINDOW);
    wire [31:0] old_trade_quantity =
        trade_quantity_history[event_symbol_index_reg][trade_write_ptr[event_symbol_index_reg]];
    wire [TRADE_ACC_W-1:0] old_trade_quantity_ext =
        {{(TRADE_ACC_W-32){1'b0}}, old_trade_quantity};
    wire [TRADE_ACC_W-1:0] event_quantity_ext =
        {{(TRADE_ACC_W-32){1'b0}}, event_quantity_reg};
    wire [TRADE_ACC_W-1:0] trade_volume_after_update =
        rolling_trade_volume[event_symbol_index_reg] -
        (trade_window_full ? old_trade_quantity_ext : {TRADE_ACC_W{1'b0}}) +
        event_quantity_ext;

    wire vwap_window_full = (vwap_valid_count[event_symbol_index_reg] >= VWAP_WINDOW);
    wire [95:0] event_price_quantity =
        {32'd0, event_price_reg} * {64'd0, event_quantity_reg};
    wire [95:0] old_vwap_price_quantity =
        vwap_price_quantity_history[event_symbol_index_reg][vwap_write_ptr[event_symbol_index_reg]];
    wire [31:0] old_vwap_quantity =
        vwap_quantity_history[event_symbol_index_reg][vwap_write_ptr[event_symbol_index_reg]];
    wire [VWAP_ACC_W-1:0] old_vwap_price_quantity_ext =
        {{(VWAP_ACC_W-96){1'b0}}, old_vwap_price_quantity};
    wire [VWAP_ACC_W-1:0] event_price_quantity_ext =
        {{(VWAP_ACC_W-96){1'b0}}, event_price_quantity};
    wire [VWAP_QTY_ACC_W-1:0] old_vwap_quantity_ext =
        {{(VWAP_QTY_ACC_W-32){1'b0}}, old_vwap_quantity};
    wire [VWAP_QTY_ACC_W-1:0] event_vwap_quantity_ext =
        {{(VWAP_QTY_ACC_W-32){1'b0}}, event_quantity_reg};
    wire [VWAP_ACC_W-1:0] vwap_price_quantity_after_update =
        vwap_sum_price_quantity[event_symbol_index_reg] -
        (vwap_window_full ? old_vwap_price_quantity_ext : {VWAP_ACC_W{1'b0}}) +
        event_price_quantity_ext;
    wire [VWAP_QTY_ACC_W-1:0] vwap_quantity_after_update =
        vwap_sum_quantity[event_symbol_index_reg] -
        (vwap_window_full ? old_vwap_quantity_ext : {VWAP_QTY_ACC_W{1'b0}}) +
        event_vwap_quantity_ext;

    wire quotes_valid = bid_valid[event_symbol_index_reg] &&
                        ask_valid[event_symbol_index_reg] &&
                        (best_ask_price[event_symbol_index_reg] >=
                         best_bid_price[event_symbol_index_reg]);
    wire [64:0] quote_price_sum =
        {1'b0, best_bid_price[event_symbol_index_reg]} +
        {1'b0, best_ask_price[event_symbol_index_reg]};
    wire [63:0] current_midpoint = quote_price_sum[64:1];
    wire [63:0] current_spread =
        best_ask_price[event_symbol_index_reg] - best_bid_price[event_symbol_index_reg];
    wire midpoint_history_full =
        (midpoint_valid_count[event_symbol_index_reg] >= MOMENTUM_WINDOW);
    wire [63:0] old_midpoint =
        midpoint_history[event_symbol_index_reg][midpoint_write_ptr[event_symbol_index_reg]];
    wire signed [64:0] momentum_delta =
        $signed({1'b0, current_midpoint}) - $signed({1'b0, old_midpoint});
    wire signed [32:0] imbalance_numerator =
        $signed({1'b0, best_bid_quantity[event_symbol_index_reg]}) -
        $signed({1'b0, best_ask_quantity[event_symbol_index_reg]});
    wire [32:0] imbalance_denominator =
        {1'b0, best_bid_quantity[event_symbol_index_reg]} +
        {1'b0, best_ask_quantity[event_symbol_index_reg]};

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

    integer symbol_index;
    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            state <= IDLE;
            event_symbol_id_reg <= 16'd0;
            event_symbol_index_reg <= {SYMBOL_ID_W{1'b0}};
            event_timestamp_reg <= 64'd0;
            event_price_reg <= 64'd0;
            event_quantity_reg <= 32'd0;
            event_side_reg <= 8'd0;
            event_sequence_reg <= 32'd0;
            event_flags_reg <= 16'd0;
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
                    end else if (sequence_is_stale) begin
                        event_reject_reason <= sequence_is_duplicate ?
                                               `STATUS_DUPLICATE_SEQ : `STATUS_STALE_SEQ;
                        event_reject_pulse <= 1'b1;
                    end else begin
                        event_symbol_id_reg <= event_symbol_id;
                        event_timestamp_reg <= event_timestamp_ns;
                        event_price_reg <= event_price;
                        event_quantity_reg <= event_quantity;
                        event_side_reg <= event_side;
                        event_sequence_reg <= event_sequence;
                        event_flags_reg <= event_flags;
                        sequence_valid[event_symbol_index] <= 1'b1;
                        last_sequence_number[event_symbol_index] <= event_sequence;
                        last_timestamp_ns[event_symbol_index] <= event_timestamp_ns;
                        event_symbol_index_reg <= event_symbol_index;
                        if (event_type == `MSG_MARKET_QUOTE)
                            state <= UPDATE_QUOTE;
                        else
                            state <= UPDATE_TRADE;
                    end
                end
            end

            UPDATE_QUOTE: begin
                if (event_side_reg == 8'd0) begin
                    best_bid_price[event_symbol_index_reg] <= event_price_reg;
                    best_bid_quantity[event_symbol_index_reg] <= event_quantity_reg;
                    bid_valid[event_symbol_index_reg] <= 1'b1;
                end else begin
                    best_ask_price[event_symbol_index_reg] <= event_price_reg;
                    best_ask_quantity[event_symbol_index_reg] <= event_quantity_reg;
                    ask_valid[event_symbol_index_reg] <= 1'b1;
                end
                state <= FEATURE_CALC;
            end

            UPDATE_TRADE: begin
                last_trade_price[event_symbol_index_reg] <= event_price_reg;
                last_trade_quantity[event_symbol_index_reg] <= event_quantity_reg;
                last_trade_side[event_symbol_index_reg] <= event_side_reg;
                trade_valid[event_symbol_index_reg] <= 1'b1;

                rolling_trade_volume[event_symbol_index_reg] <= trade_volume_after_update;
                trade_quantity_history[event_symbol_index_reg][trade_write_ptr[event_symbol_index_reg]] <=
                    event_quantity_reg;
                if (trade_write_ptr[event_symbol_index_reg] == TRADE_WINDOW - 1)
                    trade_write_ptr[event_symbol_index_reg] <= {TRADE_PTR_W{1'b0}};
                else
                    trade_write_ptr[event_symbol_index_reg] <=
                        trade_write_ptr[event_symbol_index_reg] + 1'b1;
                if (trade_valid_count[event_symbol_index_reg] < TRADE_WINDOW)
                    trade_valid_count[event_symbol_index_reg] <=
                        trade_valid_count[event_symbol_index_reg] + 1'b1;

                vwap_sum_price_quantity[event_symbol_index_reg] <= vwap_price_quantity_after_update;
                vwap_sum_quantity[event_symbol_index_reg] <= vwap_quantity_after_update;
                vwap_price_quantity_history[event_symbol_index_reg][vwap_write_ptr[event_symbol_index_reg]] <=
                    event_price_quantity;
                vwap_quantity_history[event_symbol_index_reg][vwap_write_ptr[event_symbol_index_reg]] <=
                    event_quantity_reg;
                if (vwap_write_ptr[event_symbol_index_reg] == VWAP_WINDOW - 1)
                    vwap_write_ptr[event_symbol_index_reg] <= {VWAP_PTR_W{1'b0}};
                else
                    vwap_write_ptr[event_symbol_index_reg] <=
                        vwap_write_ptr[event_symbol_index_reg] + 1'b1;
                if (vwap_valid_count[event_symbol_index_reg] < VWAP_WINDOW)
                    vwap_valid_count[event_symbol_index_reg] <=
                        vwap_valid_count[event_symbol_index_reg] + 1'b1;
                state <= FEATURE_CALC;
            end

            FEATURE_CALC: begin
                feature_valid_reg <= 1'b1;
                feature_symbol_id_reg <= event_symbol_id_reg;
                feature_sequence_reg <= event_sequence_reg;
                feature_bid_price_reg <= best_bid_price[event_symbol_index_reg];
                feature_bid_quantity_reg <= best_bid_quantity[event_symbol_index_reg];
                feature_ask_price_reg <= best_ask_price[event_symbol_index_reg];
                feature_ask_quantity_reg <= best_ask_quantity[event_symbol_index_reg];
                feature_spread_valid_reg <= quotes_valid;
                feature_spread_reg <= quotes_valid ? current_spread : 64'd0;
                feature_midpoint_valid_reg <= quotes_valid;
                feature_midpoint_reg <= quotes_valid ? current_midpoint : 64'd0;
                feature_momentum_valid_reg <= quotes_valid && midpoint_history_full;
                feature_momentum_reg <= (quotes_valid && midpoint_history_full) ?
                                        momentum_delta : 65'sd0;
                feature_rolling_volume_reg <= rolling_trade_volume[event_symbol_index_reg];
                feature_imbalance_valid_reg <= quotes_valid;
                feature_imbalance_numerator_reg <= quotes_valid ? imbalance_numerator : 33'sd0;
                feature_imbalance_denominator_reg <= quotes_valid ? imbalance_denominator : 33'd0;
                feature_vwap_sum_price_quantity_reg <=
                    vwap_sum_price_quantity[event_symbol_index_reg];
                feature_vwap_sum_quantity_reg <= vwap_sum_quantity[event_symbol_index_reg];
                feature_vwap_valid_reg <=
                    (vwap_sum_quantity[event_symbol_index_reg] != {VWAP_QTY_ACC_W{1'b0}});

                if (quotes_valid) begin
                    midpoint_history[event_symbol_index_reg][midpoint_write_ptr[event_symbol_index_reg]] <=
                        current_midpoint;
                    if (midpoint_write_ptr[event_symbol_index_reg] == MOMENTUM_WINDOW - 1)
                        midpoint_write_ptr[event_symbol_index_reg] <= {MOMENTUM_PTR_W{1'b0}};
                    else
                        midpoint_write_ptr[event_symbol_index_reg] <=
                            midpoint_write_ptr[event_symbol_index_reg] + 1'b1;
                    if (midpoint_valid_count[event_symbol_index_reg] < MOMENTUM_WINDOW)
                        midpoint_valid_count[event_symbol_index_reg] <=
                            midpoint_valid_count[event_symbol_index_reg] + 1'b1;
                end
                state <= OUTPUT;
            end

            OUTPUT: begin
                if (feature_valid_reg && feature_ready) begin
                    feature_valid_reg <= 1'b0;
                    state <= IDLE;
                end
            end

            default: state <= IDLE;
            endcase
        end
    end
endmodule
