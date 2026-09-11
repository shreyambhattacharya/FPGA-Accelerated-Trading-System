`timescale 1ns/1ps

`include "protocol_defs.svh"

// Tang Nano 20K hardware top level.
//
// clk is the onboard 27 MHz oscillator. SPI traffic remains in spi_clk; only
// Gray-coded FIFO pointers cross between that domain and clk. All packet
// validation and loopback processing runs synchronously at 27 MHz.
module trading_spi_top (
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
    wire system_response_valid;
    wire [`PACKET_BITS-1:0] system_response_packet;
    wire system_packet_error;

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

    assign rx_packet_ready = !rx_fifo_wr_full;
    assign rx_fifo_wr_en = rx_packet_valid && rx_packet_ready;
    assign tx_packet_valid = !tx_fifo_rd_empty;

    loopback_engine loopback_engine_i (
        .clk(clk),
        .reset_n(reset_n_int),
        .packet_valid(!rx_fifo_rd_empty),
        .packet_ready(system_packet_ready),
        .packet_in(rx_fifo_data),
        .response_ready(!tx_fifo_wr_full),
        .response_valid(system_response_valid),
        .response_packet(system_response_packet),
        .packet_error_pulse(system_packet_error)
    );

    assign rx_fifo_rd_en = !rx_fifo_rd_empty && system_packet_ready;
    assign tx_fifo_wr_en = system_response_valid && !tx_fifo_wr_full;

    reg [31:0] packet_error_count;
    always @(posedge clk or negedge reset_n_int) begin
        if (!reset_n_int) packet_error_count <= 32'd0;
        else if (system_packet_error) packet_error_count <= packet_error_count + 1'b1;
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

            if (packet_error_count != 0) error_seen <= 1'b1;
        end
    end

    assign led[0] = ~heartbeat_state;
    assign led[1] = (activity_hold_count == 0);
    assign led[2] = ~error_seen;
    assign led[5:3] = 3'b111;
endmodule
