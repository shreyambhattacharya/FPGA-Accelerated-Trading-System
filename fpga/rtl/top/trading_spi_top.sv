`timescale 1ns/1ps

`include "protocol_defs.svh"

// Tang Nano 20K hardware top level.
//
// clk is the onboard 27 MHz oscillator. SPI traffic remains in spi_clk; only
// Gray-coded FIFO pointers cross between that domain and clk. All packet
// validation, loopback processing, and market-state updates run synchronously
// at 27 MHz.
module trading_spi_top #(
    parameter integer NUM_SYMBOLS = 4,
    parameter integer TRADE_WINDOW = 32,
    parameter integer MOMENTUM_WINDOW = 16,
    parameter integer VWAP_WINDOW = 32,
    parameter integer PRICE_SCALE = 1000000,
    parameter integer BPS_SCALE = 10000,
    parameter integer BPS_OUTPUT_SCALE = 100,
    parameter integer IMBALANCE_FRAC_BITS = 15
) (
    input  wire       clk,
    input  wire       spi_clk,
    input  wire       spi_cs_n,
    input  wire       spi_mosi,
    output wire       spi_miso,
    output wire [5:0] led
);
    wire reset_n_int;

    power_on_reset #(.RELEASE_CYCLES(64)) reset_i (
        .clk(clk),
        .reset_n(reset_n_int)
    );

    wire [`PACKET_BITS-1:0] rx_packet;
    wire rx_packet_valid;
    wire rx_packet_ready;
    wire rx_overflow_pulse;
    wire packet_complete_toggle;
    wire tx_packet_rd_en;
    wire [`PACKET_BITS-1:0] tx_packet_data;
    wire tx_packet_valid;
    wire tx_empty_transaction_pulse;

    wire [`PACKET_BITS-1:0] rx_fifo_data;
    wire rx_fifo_wr_full;
    wire rx_fifo_rd_empty;
    wire rx_fifo_wr_en;
    wire rx_fifo_rd_en;
    wire [31:0] rx_fifo_overflow_count;
    wire [31:0] rx_fifo_underflow_count;

    wire [`PACKET_BITS-1:0] tx_fifo_data;
    wire tx_fifo_wr_full;
    wire tx_fifo_rd_empty;
    wire tx_fifo_wr_en;
    wire [31:0] tx_fifo_overflow_count;
    wire [31:0] tx_fifo_underflow_count;

    wire system_packet_ready;
    wire dispatcher_loopback_valid;
    wire dispatcher_loopback_ready;
    wire [`PACKET_BITS-1:0] dispatcher_loopback_packet;
    wire dispatcher_status_override_valid;
    wire [7:0] dispatcher_status_override;
    wire dispatcher_event_valid;
    wire dispatcher_event_ready;
    wire [7:0] dispatcher_event_type;
    wire [15:0] dispatcher_event_symbol_id;
    wire [63:0] dispatcher_event_timestamp_ns;
    wire [63:0] dispatcher_event_price;
    wire [31:0] dispatcher_event_quantity;
    wire [7:0] dispatcher_event_side;
    wire [31:0] dispatcher_event_sequence;
    wire [15:0] dispatcher_event_flags;
    wire dispatcher_control_valid;
    wire dispatcher_control_ready;
    wire [7:0] dispatcher_control_subcommand;
    wire [15:0] dispatcher_control_symbol_id;
    wire [63:0] dispatcher_control_data64;
    wire [31:0] dispatcher_control_data32;
    wire [15:0] dispatcher_control_flags;
    wire [7:0] dispatcher_control_status;
    wire dispatcher_error_pulse;
    wire [7:0] dispatcher_error_reason;
    wire [15:0] dispatcher_error_symbol_id;
    wire [31:0] dispatcher_error_sequence;

    localparam integer TRADE_ACC_W = 32 + ((TRADE_WINDOW <= 1) ? 1 : $clog2(TRADE_WINDOW));
    localparam integer VWAP_ACC_W = 96 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));
    localparam integer VWAP_QTY_ACC_W = 32 + ((VWAP_WINDOW <= 1) ? 1 : $clog2(VWAP_WINDOW));
    localparam integer SIGNAL_BUS_W = 16 + 32 + 2 + 4 + 16;
    localparam integer MARKET_FEATURE_BUS_W = 16 + 32 + 64 + 32 + 64 + 32 +
                                              64 + 1 + 64 + 1 + 65 + 1 +
                                              TRADE_ACC_W + 33 + 33 + 1 +
                                              VWAP_ACC_W + VWAP_QTY_ACC_W + 1 +
                                              64 + 1 + 16 + 1 + 32 + 1 + 32 + 1 +
                                              65 + 1 + 32 + 1 +
                                              32 + 64 + 96 + 32 + SIGNAL_BUS_W;
    wire market_feature_valid;
    wire market_feature_ready;
    wire [15:0] market_feature_symbol_id;
    wire [31:0] market_feature_sequence;
    wire [63:0] market_feature_bid_price;
    wire [31:0] market_feature_bid_quantity;
    wire [63:0] market_feature_ask_price;
    wire [31:0] market_feature_ask_quantity;
    wire [63:0] market_feature_spread;
    wire market_feature_spread_valid;
    wire [63:0] market_feature_midpoint;
    wire market_feature_midpoint_valid;
    wire signed [64:0] market_feature_momentum;
    wire market_feature_momentum_valid;
    wire [TRADE_ACC_W-1:0] market_feature_rolling_volume;
    wire signed [32:0] market_feature_imbalance_numerator;
    wire [32:0] market_feature_imbalance_denominator;
    wire market_feature_imbalance_valid;
    wire [VWAP_ACC_W-1:0] market_feature_vwap_sum_price_quantity;
    wire [VWAP_QTY_ACC_W-1:0] market_feature_vwap_sum_quantity;
    wire market_feature_vwap_valid;
    wire [63:0] market_feature_vwap;
    wire market_feature_vwap_quotient_valid;
    wire signed [15:0] market_feature_imbalance_normalized;
    wire market_feature_imbalance_normalized_valid;
    wire signed [31:0] market_feature_spread_bps_x100;
    wire market_feature_spread_bps_x100_valid;
    wire signed [31:0] market_feature_momentum_bps_x100;
    wire market_feature_momentum_bps_x100_valid;
    wire signed [64:0] market_feature_midpoint_minus_vwap;
    wire market_feature_midpoint_minus_vwap_valid;
    wire signed [31:0] market_feature_midpoint_minus_vwap_bps_x100;
    wire market_feature_midpoint_minus_vwap_bps_x100_valid;
    wire [31:0] market_history_trade_probe;
    wire [63:0] market_history_midpoint_probe;
    wire [95:0] market_history_vwap_price_quantity_probe;
    wire [31:0] market_history_vwap_quantity_probe;
    wire market_reject_pulse;
    wire [7:0] market_reject_reason;
    wire [15:0] market_reject_symbol_id;
    wire [31:0] market_reject_sequence;
    wire system_response_valid;
    wire [`PACKET_BITS-1:0] system_response_packet;
    wire system_packet_error;

    wire strategy_enable;
    wire long_enable;
    wire short_enable;
    wire signed [31:0] long_min_momentum_bps_x100;
    wire signed [31:0] short_max_momentum_bps_x100;
    wire signed [31:0] long_min_vwap_delta_bps_x100;
    wire signed [31:0] short_max_vwap_delta_bps_x100;
    wire signed [15:0] long_min_imbalance_q15;
    wire signed [15:0] short_max_imbalance_q15;
    wire signed [31:0] max_spread_bps_x100;
    wire [TRADE_ACC_W-1:0] min_rolling_volume;
    wire [31:0] signal_cooldown_events;
    wire [NUM_SYMBOLS-1:0] symbol_enable;
    wire config_slot_reset_valid;
    wire [15:0] config_slot_reset_symbol_id;
    wire config_slot_reset_ready;
    wire market_slot_reset_ready;
    wire signal_slot_reset_ready;

    wire signal_feature_ready;
    wire signal_valid;
    wire signal_ready = 1'b1;
    wire [15:0] signal_symbol_id;
    wire [31:0] signal_sequence;
    wire [1:0] signal_action;
    wire [3:0] signal_score;
    wire [15:0] signal_reason_bits;

    assign config_slot_reset_ready = market_slot_reset_ready &&
                                     signal_slot_reset_ready;
    assign market_feature_ready = signal_feature_ready;

    // The feature bus is the handoff point for the next CSR/host result
    // stage. Keep it in the synthesized top even while the physical SPI
    // response path remains diagnostic-only in this milestone.
    (* keep = "true" *) wire [MARKET_FEATURE_BUS_W-1:0] market_feature_bus;
    assign market_feature_bus = {
        market_feature_symbol_id, market_feature_sequence,
        market_feature_bid_price, market_feature_bid_quantity,
        market_feature_ask_price, market_feature_ask_quantity,
        market_feature_spread, market_feature_spread_valid,
        market_feature_midpoint, market_feature_midpoint_valid,
        market_feature_momentum, market_feature_momentum_valid,
        market_feature_rolling_volume, market_feature_imbalance_numerator,
        market_feature_imbalance_denominator, market_feature_imbalance_valid,
        market_feature_vwap_sum_price_quantity,
        market_feature_vwap_sum_quantity, market_feature_vwap_valid,
        market_feature_vwap, market_feature_vwap_quotient_valid,
        market_feature_imbalance_normalized, market_feature_imbalance_normalized_valid,
        market_feature_spread_bps_x100, market_feature_spread_bps_x100_valid,
        market_feature_momentum_bps_x100, market_feature_momentum_bps_x100_valid,
        market_feature_midpoint_minus_vwap, market_feature_midpoint_minus_vwap_valid,
        market_feature_midpoint_minus_vwap_bps_x100,
        market_feature_midpoint_minus_vwap_bps_x100_valid,
        market_history_trade_probe, market_history_midpoint_probe,
        market_history_vwap_price_quantity_probe, market_history_vwap_quantity_probe,
        signal_symbol_id, signal_sequence, signal_action, signal_score,
        signal_reason_bits
    };

    spi_slave spi_slave_i (
        .reset_n(reset_n_int),
        .spi_clk(spi_clk),
        .cs_n(spi_cs_n),
        .mosi(spi_mosi),
        .miso(spi_miso),
        .rx_packet(rx_packet),
        .rx_packet_valid(rx_packet_valid),
        .rx_packet_ready(rx_packet_ready),
        .rx_overflow_pulse(rx_overflow_pulse),
        .packet_complete_toggle(packet_complete_toggle),
        .tx_packet_data(tx_packet_data),
        .tx_packet_valid(tx_packet_valid),
        .tx_packet_rd_en(tx_packet_rd_en),
        .tx_empty_transaction_pulse(tx_empty_transaction_pulse)
    );

    async_packet_fifo #(.WIDTH(`PACKET_BITS), .DEPTH(4)) rx_fifo_i (
        .reset_n(reset_n_int),
        .wr_clk(spi_clk),
        .wr_en(rx_fifo_wr_en),
        .din(rx_packet),
        .wr_full(rx_fifo_wr_full),
        .wr_overflow_count(rx_fifo_overflow_count),
        .rd_clk(clk),
        .rd_en(rx_fifo_rd_en),
        .dout(rx_fifo_data),
        .rd_empty(rx_fifo_rd_empty),
        .rd_underflow_count(rx_fifo_underflow_count)
    );

    async_packet_fifo #(.WIDTH(`PACKET_BITS), .DEPTH(4)) tx_fifo_i (
        .reset_n(reset_n_int),
        .wr_clk(clk),
        .wr_en(tx_fifo_wr_en),
        .din(system_response_packet),
        .wr_full(tx_fifo_wr_full),
        .wr_overflow_count(tx_fifo_overflow_count),
        .rd_clk(spi_clk),
        .rd_en(tx_packet_rd_en),
        .dout(tx_packet_data),
        .rd_empty(tx_fifo_rd_empty),
        .rd_underflow_count(tx_fifo_underflow_count)
    );

    strategy_config #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
        .TRADE_ACC_W(TRADE_ACC_W)
    ) strategy_config_i (
        .clk(clk),
        .reset_n(reset_n_int),
        .cfg_write_valid(dispatcher_control_valid),
        .cfg_write_ready(dispatcher_control_ready),
        .cfg_subcommand(dispatcher_control_subcommand),
        .cfg_symbol_id(dispatcher_control_symbol_id),
        .cfg_data64(dispatcher_control_data64),
        .cfg_data32(dispatcher_control_data32),
        .cfg_flags(dispatcher_control_flags),
        .cfg_status(dispatcher_control_status),
        .strategy_enable(strategy_enable),
        .long_enable(long_enable),
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
        .slot_reset_valid(config_slot_reset_valid),
        .slot_reset_symbol_id(config_slot_reset_symbol_id),
        .slot_reset_ready(config_slot_reset_ready)
    );

    assign rx_packet_ready = !rx_fifo_wr_full;
    assign rx_fifo_wr_en = rx_packet_valid && rx_packet_ready;
    assign tx_packet_valid = !tx_fifo_rd_empty;

    packet_dispatcher #(.NUM_SYMBOLS(NUM_SYMBOLS)) packet_dispatcher_i (
        .clk(clk),
        .reset_n(reset_n_int),
        .packet_valid(!rx_fifo_rd_empty),
        .packet_ready(system_packet_ready),
        .packet_in(rx_fifo_data),
        .loopback_valid(dispatcher_loopback_valid),
        .loopback_ready(dispatcher_loopback_ready),
        .loopback_packet(dispatcher_loopback_packet),
        .loopback_status_override_valid(dispatcher_status_override_valid),
        .loopback_status_override(dispatcher_status_override),
        .event_valid(dispatcher_event_valid),
        .event_ready(dispatcher_event_ready),
        .event_type(dispatcher_event_type),
        .event_symbol_id(dispatcher_event_symbol_id),
        .event_timestamp_ns(dispatcher_event_timestamp_ns),
        .event_price(dispatcher_event_price),
        .event_quantity(dispatcher_event_quantity),
        .event_side(dispatcher_event_side),
        .event_sequence(dispatcher_event_sequence),
        .event_flags(dispatcher_event_flags),
        .control_valid(dispatcher_control_valid),
        .control_ready(dispatcher_control_ready),
        .control_subcommand(dispatcher_control_subcommand),
        .control_symbol_id(dispatcher_control_symbol_id),
        .control_data64(dispatcher_control_data64),
        .control_data32(dispatcher_control_data32),
        .control_flags(dispatcher_control_flags),
        .control_status(dispatcher_control_status),
        .dispatch_error_pulse(dispatcher_error_pulse),
        .dispatch_error_reason(dispatcher_error_reason),
        .dispatch_error_symbol_id(dispatcher_error_symbol_id),
        .dispatch_error_sequence(dispatcher_error_sequence)
    );

    loopback_engine loopback_engine_i (
        .clk(clk),
        .reset_n(reset_n_int),
        .packet_valid(dispatcher_loopback_valid),
        .packet_ready(dispatcher_loopback_ready),
        .packet_in(dispatcher_loopback_packet),
        .response_ready(!tx_fifo_wr_full),
        .status_override_valid(dispatcher_status_override_valid),
        .status_override(dispatcher_status_override),
        .response_valid(system_response_valid),
        .response_packet(system_response_packet),
        .packet_error_pulse(system_packet_error)
    );

    market_state_engine #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
        .TRADE_WINDOW(TRADE_WINDOW),
        .MOMENTUM_WINDOW(MOMENTUM_WINDOW),
        .VWAP_WINDOW(VWAP_WINDOW),
        .PRICE_SCALE(PRICE_SCALE),
        .BPS_SCALE(BPS_SCALE),
        .BPS_OUTPUT_SCALE(BPS_OUTPUT_SCALE),
        .IMBALANCE_FRAC_BITS(IMBALANCE_FRAC_BITS)
    ) market_state_engine_i (
        .clk(clk),
        .reset_n(reset_n_int),
        .event_valid(dispatcher_event_valid),
        .event_ready(dispatcher_event_ready),
        .event_type(dispatcher_event_type),
        .event_symbol_id(dispatcher_event_symbol_id),
        .event_timestamp_ns(dispatcher_event_timestamp_ns),
        .event_price(dispatcher_event_price),
        .event_quantity(dispatcher_event_quantity),
        .event_side(dispatcher_event_side),
        .event_sequence(dispatcher_event_sequence),
        .event_flags(dispatcher_event_flags),
        .slot_reset_valid(config_slot_reset_valid),
        .slot_reset_symbol_id(config_slot_reset_symbol_id),
        .slot_reset_ready(market_slot_reset_ready),
        .feature_valid(market_feature_valid),
        .feature_ready(market_feature_ready),
        .feature_symbol_id(market_feature_symbol_id),
        .feature_sequence(market_feature_sequence),
        .feature_bid_price(market_feature_bid_price),
        .feature_bid_quantity(market_feature_bid_quantity),
        .feature_ask_price(market_feature_ask_price),
        .feature_ask_quantity(market_feature_ask_quantity),
        .feature_spread(market_feature_spread),
        .feature_spread_valid(market_feature_spread_valid),
        .feature_midpoint(market_feature_midpoint),
        .feature_midpoint_valid(market_feature_midpoint_valid),
        .feature_momentum(market_feature_momentum),
        .feature_momentum_valid(market_feature_momentum_valid),
        .feature_rolling_volume(market_feature_rolling_volume),
        .feature_imbalance_numerator(market_feature_imbalance_numerator),
        .feature_imbalance_denominator(market_feature_imbalance_denominator),
        .feature_imbalance_valid(market_feature_imbalance_valid),
        .feature_vwap_sum_price_quantity(market_feature_vwap_sum_price_quantity),
        .feature_vwap_sum_quantity(market_feature_vwap_sum_quantity),
        .feature_vwap_valid(market_feature_vwap_valid),
        .feature_vwap(market_feature_vwap),
        .feature_vwap_quotient_valid(market_feature_vwap_quotient_valid),
        .feature_imbalance_normalized(market_feature_imbalance_normalized),
        .feature_imbalance_normalized_valid(market_feature_imbalance_normalized_valid),
        .feature_spread_bps_x100(market_feature_spread_bps_x100),
        .feature_spread_bps_x100_valid(market_feature_spread_bps_x100_valid),
        .feature_momentum_bps_x100(market_feature_momentum_bps_x100),
        .feature_momentum_bps_x100_valid(market_feature_momentum_bps_x100_valid),
        .feature_midpoint_minus_vwap(market_feature_midpoint_minus_vwap),
        .feature_midpoint_minus_vwap_valid(market_feature_midpoint_minus_vwap_valid),
        .feature_midpoint_minus_vwap_bps_x100(market_feature_midpoint_minus_vwap_bps_x100),
        .feature_midpoint_minus_vwap_bps_x100_valid(market_feature_midpoint_minus_vwap_bps_x100_valid),
        .history_trade_probe(market_history_trade_probe),
        .history_midpoint_probe(market_history_midpoint_probe),
        .history_vwap_price_quantity_probe(market_history_vwap_price_quantity_probe),
        .history_vwap_quantity_probe(market_history_vwap_quantity_probe),
        .event_reject_pulse(market_reject_pulse),
        .event_reject_reason(market_reject_reason),
        .event_reject_symbol_id(market_reject_symbol_id),
        .event_reject_sequence(market_reject_sequence)
    );

    signal_engine #(
        .NUM_SYMBOLS(NUM_SYMBOLS),
        .TRADE_ACC_W(TRADE_ACC_W)
    ) signal_engine_i (
        .clk(clk),
        .reset_n(reset_n_int),
        .feature_valid(market_feature_valid),
        .feature_ready(signal_feature_ready),
        .feature_symbol_id(market_feature_symbol_id),
        .feature_sequence(market_feature_sequence),
        .feature_spread_bps_x100(market_feature_spread_bps_x100),
        .feature_spread_valid(market_feature_spread_bps_x100_valid),
        .feature_momentum_bps_x100(market_feature_momentum_bps_x100),
        .feature_momentum_valid(market_feature_momentum_bps_x100_valid),
        .feature_imbalance_q15(market_feature_imbalance_normalized),
        .feature_imbalance_valid(market_feature_imbalance_normalized_valid),
        .feature_vwap(market_feature_vwap),
        .feature_vwap_valid(market_feature_vwap_quotient_valid),
        .feature_midpoint_minus_vwap(market_feature_midpoint_minus_vwap),
        .feature_midpoint_minus_vwap_bps_x100(
            market_feature_midpoint_minus_vwap_bps_x100),
        .feature_vwap_delta_valid(
            market_feature_midpoint_minus_vwap_bps_x100_valid),
        .feature_rolling_volume(market_feature_rolling_volume),
        .strategy_enable(strategy_enable),
        .long_enable(long_enable),
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
        .slot_reset_valid(config_slot_reset_valid),
        .slot_reset_symbol_id(config_slot_reset_symbol_id),
        .slot_reset_ready(signal_slot_reset_ready),
        .signal_valid(signal_valid),
        .signal_ready(signal_ready),
        .signal_symbol_id(signal_symbol_id),
        .signal_sequence(signal_sequence),
        .signal_action(signal_action),
        .signal_score(signal_score),
        .signal_reason_bits(signal_reason_bits)
    );

    assign rx_fifo_rd_en = !rx_fifo_rd_empty && system_packet_ready;
    assign tx_fifo_wr_en = system_response_valid && !tx_fifo_wr_full;

    reg [31:0] packet_error_count;
    reg [31:0] dispatch_error_count;
    reg [31:0] market_reject_count;
    always @(posedge clk or negedge reset_n_int) begin
        if (!reset_n_int) begin
            packet_error_count <= 32'd0;
            dispatch_error_count <= 32'd0;
            market_reject_count <= 32'd0;
        end else begin
            if (system_packet_error) packet_error_count <= packet_error_count + 1'b1;
            if (dispatcher_error_pulse)
                dispatch_error_count <= dispatch_error_count + 1'b1;
            if (market_reject_pulse)
                market_reject_count <= market_reject_count + 1'b1;
        end
    end

    // LED0: active-low heartbeat. LED1: active-low activity pulse. LED2:
    // active-low sticky packet/CRC error. Remaining LEDs are off.
    reg [24:0] heartbeat_count;
    reg heartbeat_state;
    reg activity_sync1, activity_sync2, activity_seen;
    reg [20:0] activity_hold_count;
    reg error_seen;

    always @(posedge clk or negedge reset_n_int) begin
        if (!reset_n_int) begin
            heartbeat_count <= 0;
            heartbeat_state <= 1'b0;
            activity_sync1 <= 1'b0;
            activity_sync2 <= 1'b0;
            activity_seen <= 1'b0;
            activity_hold_count <= 0;
            error_seen <= 1'b0;
        end else begin
            if (heartbeat_count == 25'd13_499_999) begin
                heartbeat_count <= 0;
                heartbeat_state <= ~heartbeat_state;
            end else heartbeat_count <= heartbeat_count + 1'b1;

            activity_sync1 <= packet_complete_toggle;
            activity_sync2 <= activity_sync1;
            activity_seen <= activity_sync2;
            if (activity_sync2 != activity_seen)
                activity_hold_count <= 21'd1_350_000; // approximately 50 ms
            else if (activity_hold_count != 0)
                activity_hold_count <= activity_hold_count - 1'b1;

            if ((packet_error_count != 0) ||
                (dispatch_error_count != 0) ||
                (market_reject_count != 0)) error_seen <= 1'b1;
        end
    end

    assign led[0] = ~heartbeat_state;
    assign led[1] = (activity_hold_count == 0);
    assign led[2] = ~error_seen;
    assign led[5:3] = 3'b111;
endmodule
