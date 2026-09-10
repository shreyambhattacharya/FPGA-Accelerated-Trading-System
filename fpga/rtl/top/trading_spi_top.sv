`timescale 1ns/1ps

`include "protocol_defs.svh"

module trading_spi_top (
    input  wire                    clk,
    input  wire                    reset_n,
    input  wire                    spi_clk,
    input  wire                    spi_cs_n,
    input  wire                    spi_mosi,
    output wire                    spi_miso,

    output wire                    rx_fifo_full,
    output wire                    rx_fifo_empty,
    output wire                    tx_fifo_full,
    output wire                    tx_fifo_empty,
    output wire [31:0]             rx_fifo_overflow_count,
    output wire [31:0]             rx_fifo_underflow_count,
    output wire [31:0]             tx_fifo_overflow_count,
    output wire [31:0]             tx_fifo_underflow_count,
    output reg  [31:0]             packet_error_count,
    output wire                    incomplete_frame_seen
);
    // The initial milestone intentionally clocks the packet FIFOs from SPI.
    // clk is reserved for the future continuously-running control pipeline.
    wire unused_clk = clk;

    wire [`PACKET_BITS-1:0] rx_packet;
    wire rx_packet_valid;
    wire rx_packet_ready;
    wire rx_overflow_pulse;
    wire [8:0] rx_bit_count_debug;
    wire tx_packet_rd_en;
    wire [`PACKET_BITS-1:0] tx_packet_data;
    wire tx_packet_valid;
    wire tx_empty_transaction_pulse;

    wire [`PACKET_BITS-1:0] rx_fifo_data;
    wire [`PACKET_BITS-1:0] response_packet;
    wire rx_fifo_rd_en;
    wire response_wr_en;
    wire packet_error;

    wire [31:0] rx_fifo_level;
    wire [31:0] tx_fifo_level;

    spi_slave spi_slave_i (
        .reset_n(reset_n),
        .spi_clk(spi_clk),
        .cs_n(spi_cs_n),
        .mosi(spi_mosi),
        .miso(spi_miso),
        .rx_packet(rx_packet),
        .rx_packet_valid(rx_packet_valid),
        .rx_packet_ready(rx_packet_ready),
        .rx_overflow_pulse(rx_overflow_pulse),
        .rx_bit_count_debug(rx_bit_count_debug),
        .incomplete_frame_seen(incomplete_frame_seen),
        .tx_packet_data(tx_packet_data),
        .tx_packet_valid(tx_packet_valid),
        .tx_packet_rd_en(tx_packet_rd_en),
        .tx_empty_transaction_pulse(tx_empty_transaction_pulse)
    );

    packet_fifo #(.WIDTH(`PACKET_BITS), .DEPTH(4)) rx_fifo_i (
        .clk(spi_clk),
        .reset_n(reset_n),
        .wr_en(rx_packet_valid && rx_packet_ready),
        .din(rx_packet),
        .rd_en(rx_fifo_rd_en),
        .dout(rx_fifo_data),
        .full(rx_fifo_full),
        .empty(rx_fifo_empty),
        .overflow_count(rx_fifo_overflow_count),
        .underflow_count(rx_fifo_underflow_count),
        .level(rx_fifo_level)
    );

    packet_fifo #(.WIDTH(`PACKET_BITS), .DEPTH(4)) tx_fifo_i (
        .clk(spi_clk),
        .reset_n(reset_n),
        .wr_en(response_wr_en && !tx_fifo_full),
        .din(response_packet),
        .rd_en(tx_packet_rd_en),
        .dout(tx_packet_data),
        .full(tx_fifo_full),
        .empty(tx_fifo_empty),
        .overflow_count(tx_fifo_overflow_count),
        .underflow_count(tx_fifo_underflow_count),
        .level(tx_fifo_level)
    );

    loopback_engine loopback_engine_i (
        .packet_valid(!rx_fifo_empty),
        .packet_ready(),
        .packet_in(rx_fifo_data),
        .response_ready(!tx_fifo_full),
        .response_wr_en(response_wr_en),
        .response_packet(response_packet),
        .packet_error(packet_error)
    );

    assign rx_packet_ready = !rx_fifo_full;
    assign rx_fifo_rd_en = response_wr_en;
    assign tx_packet_valid = !tx_fifo_empty;

    always @(posedge spi_clk or negedge reset_n) begin
        if (!reset_n) begin
            packet_error_count <= 32'd0;
        end else if (packet_error) begin
            packet_error_count <= packet_error_count + 1'b1;
        end
    end

    // Keep the port visible in synthesis reports without using it as a clock
    // in this first packet-domain proof.
    wire unused_status = rx_overflow_pulse ^ tx_empty_transaction_pulse ^ unused_clk ^ rx_bit_count_debug[0];
endmodule
