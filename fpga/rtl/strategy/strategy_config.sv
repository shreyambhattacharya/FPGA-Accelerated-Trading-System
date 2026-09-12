`timescale 1ns/1ps

`include "protocol_defs.svh"

// Runtime configuration bank for the candidate-signal engine.
//
// The write interface is intentionally independent of SPI so it can be
// driven by a future CSR/packet adapter or directly by a verification bench.
// The top-level SPI design connects it to CRC-validated MSG_CONTROL packets.
// Defaults are conservative TEST / DEVELOPMENT DEFAULTS only; they are not
// strategy recommendations or profitability claims.
module strategy_config #(
    parameter integer NUM_SYMBOLS = 4,
    parameter integer TRADE_ACC_W = 32 + ((32 <= 1) ? 1 : $clog2(32))
) (
    input wire                         clk,
    input wire                         reset_n,

    input wire                         cfg_write_valid,
    output wire                        cfg_write_ready,
    input wire [7:0]                   cfg_subcommand,
    input wire [15:0]                  cfg_symbol_id,
    input wire [63:0]                  cfg_data64,
    input wire [31:0]                  cfg_data32,
    input wire [15:0]                  cfg_flags,
    output reg [7:0]                   cfg_status,

    output reg                         strategy_enable,
    output reg                         long_enable,
    output reg                         short_enable,
    output reg signed [31:0]           long_min_momentum_bps_x100,
    output reg signed [31:0]           short_max_momentum_bps_x100,
    output reg signed [31:0]           long_min_vwap_delta_bps_x100,
    output reg signed [31:0]           short_max_vwap_delta_bps_x100,
    output reg signed [15:0]           long_min_imbalance_q15,
    output reg signed [15:0]           short_max_imbalance_q15,
    output reg signed [31:0]           max_spread_bps_x100,
    output reg [TRADE_ACC_W-1:0]       min_rolling_volume,
    output reg [31:0]                  signal_cooldown_events,
    output reg [NUM_SYMBOLS-1:0]       symbol_enable,

    output wire                        slot_reset_valid,
    output wire [15:0]                 slot_reset_symbol_id,
    input wire                         slot_reset_ready
);
    // TEST / DEVELOPMENT DEFAULTS. strategy_enable intentionally starts off.
    localparam signed [31:0] DEFAULT_LONG_MOMENTUM = 32'sd100;
    localparam signed [31:0] DEFAULT_SHORT_MOMENTUM = -32'sd100;
    localparam signed [31:0] DEFAULT_LONG_VWAP_DELTA = 32'sd100;
    localparam signed [31:0] DEFAULT_SHORT_VWAP_DELTA = -32'sd100;
    localparam signed [15:0] DEFAULT_LONG_IMBALANCE = 16'sd1638;
    localparam signed [15:0] DEFAULT_SHORT_IMBALANCE = -16'sd1638;
    localparam signed [31:0] DEFAULT_MAX_SPREAD = 32'sd500;
    localparam integer DEFAULT_MIN_VOLUME = 1;
    localparam integer DEFAULT_COOLDOWN = 4;

    wire symbol_in_range = (cfg_symbol_id < NUM_SYMBOLS);
    reg slot_reset_valid_reg = 1'b0;
    reg [15:0] slot_reset_symbol_id_reg = 16'd0;

    wire command_is_global = (cfg_subcommand == `CONTROL_SET_GLOBAL_FLAGS) ||
                             (cfg_subcommand == `CONTROL_SET_LONG_MOMENTUM) ||
                             (cfg_subcommand == `CONTROL_SET_SHORT_MOMENTUM) ||
                             (cfg_subcommand == `CONTROL_SET_LONG_VWAP_DELTA) ||
                             (cfg_subcommand == `CONTROL_SET_SHORT_VWAP_DELTA) ||
                             (cfg_subcommand == `CONTROL_SET_LONG_IMBALANCE) ||
                             (cfg_subcommand == `CONTROL_SET_SHORT_IMBALANCE) ||
                             (cfg_subcommand == `CONTROL_SET_MAX_SPREAD) ||
                             (cfg_subcommand == `CONTROL_SET_MIN_VOLUME) ||
                             (cfg_subcommand == `CONTROL_SET_COOLDOWN);
    wire command_is_symbol = (cfg_subcommand == `CONTROL_SET_SYMBOL_ENABLE) ||
                             (cfg_subcommand == `CONTROL_CLEAR_SYMBOL_STATE);
    wire cfg_write_accept = cfg_write_valid && cfg_write_ready &&
                            (cfg_status == `STATUS_OK);

    assign cfg_write_ready = !slot_reset_valid_reg;
    assign slot_reset_valid = slot_reset_valid_reg;
    assign slot_reset_symbol_id = slot_reset_symbol_id_reg;

    // Status is combinational so the packet dispatcher can return an ACK
    // status in the existing loopback response path on the same handshake.
    always @* begin
        if (slot_reset_valid_reg) begin
            cfg_status = `STATUS_CONFIG_BUSY;
        end else if (command_is_global) begin
            cfg_status = `STATUS_OK;
        end else if (command_is_symbol && symbol_in_range) begin
            cfg_status = `STATUS_OK;
        end else begin
            cfg_status = `STATUS_BAD_CONTROL;
        end
    end

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            strategy_enable <= 1'b0;
            long_enable <= 1'b1;
            short_enable <= 1'b1;
            long_min_momentum_bps_x100 <= DEFAULT_LONG_MOMENTUM;
            short_max_momentum_bps_x100 <= DEFAULT_SHORT_MOMENTUM;
            long_min_vwap_delta_bps_x100 <= DEFAULT_LONG_VWAP_DELTA;
            short_max_vwap_delta_bps_x100 <= DEFAULT_SHORT_VWAP_DELTA;
            long_min_imbalance_q15 <= DEFAULT_LONG_IMBALANCE;
            short_max_imbalance_q15 <= DEFAULT_SHORT_IMBALANCE;
            max_spread_bps_x100 <= DEFAULT_MAX_SPREAD;
            min_rolling_volume <= DEFAULT_MIN_VOLUME;
            signal_cooldown_events <= DEFAULT_COOLDOWN;
            symbol_enable <= {NUM_SYMBOLS{1'b1}};
            slot_reset_valid_reg <= 1'b0;
            slot_reset_symbol_id_reg <= 16'd0;
        end else begin
            if (slot_reset_valid_reg && slot_reset_ready)
                slot_reset_valid_reg <= 1'b0;

            if (cfg_write_accept) begin
                case (cfg_subcommand)
                `CONTROL_SET_GLOBAL_FLAGS:
                    begin
                        strategy_enable <= cfg_data64[0];
                        long_enable <= cfg_data64[1];
                        short_enable <= cfg_data64[2];
                    end
                `CONTROL_SET_LONG_MOMENTUM:
                    long_min_momentum_bps_x100 <= $signed(cfg_data32);
                `CONTROL_SET_SHORT_MOMENTUM:
                    short_max_momentum_bps_x100 <= $signed(cfg_data32);
                `CONTROL_SET_LONG_VWAP_DELTA:
                    long_min_vwap_delta_bps_x100 <= $signed(cfg_data32);
                `CONTROL_SET_SHORT_VWAP_DELTA:
                    short_max_vwap_delta_bps_x100 <= $signed(cfg_data32);
                `CONTROL_SET_LONG_IMBALANCE:
                    long_min_imbalance_q15 <= $signed(cfg_data32[15:0]);
                `CONTROL_SET_SHORT_IMBALANCE:
                    short_max_imbalance_q15 <= $signed(cfg_data32[15:0]);
                `CONTROL_SET_MAX_SPREAD:
                    max_spread_bps_x100 <= $signed(cfg_data32);
                `CONTROL_SET_MIN_VOLUME:
                    min_rolling_volume <= cfg_data64[TRADE_ACC_W-1:0];
                `CONTROL_SET_COOLDOWN:
                    signal_cooldown_events <= cfg_data32;
                `CONTROL_SET_SYMBOL_ENABLE:
                    symbol_enable[cfg_symbol_id] <= cfg_data64[0];
                `CONTROL_CLEAR_SYMBOL_STATE:
                    begin
                        slot_reset_symbol_id_reg <= cfg_symbol_id;
                        slot_reset_valid_reg <= 1'b1;
                    end
                default: begin end
                endcase
            end
        end
    end
endmodule
