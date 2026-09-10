`timescale 1ns/1ps

`include "protocol_defs.svh"

// SPI mode-0 slave. The Pi is master and supplies SCLK/CS.
// MOSI is sampled on rising SCLK; the MISO bit index advances on falling SCLK.
// A packet becomes valid only after exactly 256 sampled bits.
module spi_slave (
    input  wire                    reset_n,
    input  wire                    spi_clk,
    input  wire                    cs_n,
    input  wire                    mosi,
    output wire                    miso,

    output reg  [`PACKET_BITS-1:0] rx_packet,
    output reg                     rx_packet_valid,
    input  wire                    rx_packet_ready,
    output reg                     rx_overflow_pulse,
    output reg  [8:0]              rx_bit_count_debug,
    output reg                     incomplete_frame_seen,

    input  wire [`PACKET_BITS-1:0] tx_packet_data,
    input  wire                    tx_packet_valid,
    output wire                    tx_packet_rd_en,
    output reg                     tx_empty_transaction_pulse
);
    reg [`PACKET_BITS-1:0] rx_shift;
    reg [8:0] rx_bit_count;

    reg [`PACKET_BITS-1:0] tx_shift;
    reg [8:0] tx_bit_index;
    reg tx_started;
    reg tx_has_data;

    // Before the first rising edge, the first MISO bit is visible directly
    // from the FIFO head. Thereafter only the latched packet is used.
    assign miso = !cs_n ?
                  (tx_started ? (tx_has_data ? tx_shift[`PACKET_BITS-1-tx_bit_index] : 1'b0) :
                                (tx_packet_valid ? tx_packet_data[`PACKET_BITS-1] : 1'b0)) :
                  1'b0;

    assign tx_packet_rd_en = (!cs_n && !tx_started && (rx_bit_count == 0) && tx_packet_valid);

    // RX state and transaction-start TX latch. CS is used as an asynchronous
    // frame reset so a master may deassert CS without supplying extra clocks.
    always @(posedge spi_clk or negedge reset_n or posedge cs_n) begin
        if (!reset_n) begin
            rx_shift                 <= {`PACKET_BITS{1'b0}};
            rx_packet                <= {`PACKET_BITS{1'b0}};
            rx_packet_valid          <= 1'b0;
            rx_overflow_pulse        <= 1'b0;
            rx_bit_count              <= 9'd0;
            rx_bit_count_debug       <= 9'd0;
            tx_shift                 <= {`PACKET_BITS{1'b0}};
            tx_started               <= 1'b0;
            tx_has_data              <= 1'b0;
            tx_empty_transaction_pulse <= 1'b0;
        end else if (cs_n) begin
            rx_shift           <= {`PACKET_BITS{1'b0}};
            rx_bit_count       <= 9'd0;
            rx_bit_count_debug <= 9'd0;
            tx_started         <= 1'b0;
            tx_has_data        <= 1'b0;
        end else begin
            rx_overflow_pulse          <= 1'b0;
            tx_empty_transaction_pulse <= 1'b0;

            if (rx_packet_valid && rx_packet_ready) begin
                rx_packet_valid <= 1'b0;
            end

            if (!tx_started && (rx_bit_count == 0)) begin
                tx_started  <= 1'b1;
                tx_has_data <= tx_packet_valid;
                if (!tx_packet_valid) begin
                    tx_empty_transaction_pulse <= 1'b1;
                end else begin
                    tx_shift <= tx_packet_data;
                end
            end

            if (rx_bit_count < `PACKET_BITS) begin
                rx_shift           <= {rx_shift[`PACKET_BITS-2:0], mosi};
                rx_bit_count       <= rx_bit_count + 1'b1;
                rx_bit_count_debug <= rx_bit_count + 1'b1;
                if (rx_bit_count == (`PACKET_BITS - 1)) begin
                    rx_packet <= {rx_shift[`PACKET_BITS-2:0], mosi};
                    if (rx_packet_valid && !rx_packet_ready) begin
                        rx_overflow_pulse <= 1'b1;
                    end else begin
                        rx_packet_valid <= 1'b1;
                    end
                end
            end
        end
    end

    // TX advances only on the falling edge, as required by SPI mode 0.
    always @(negedge spi_clk or negedge reset_n or posedge cs_n) begin
        if (!reset_n) begin
            tx_bit_index <= 9'd0;
        end else if (cs_n) begin
            tx_bit_index <= 9'd0;
        end else if (tx_started && (tx_bit_index < (`PACKET_BITS - 1))) begin
            tx_bit_index <= tx_bit_index + 1'b1;
        end
    end

    // This is a diagnostic sticky bit. It is clocked by CS because an
    // incomplete frame can end without another SCLK edge.
    always @(posedge cs_n or negedge reset_n) begin
        if (!reset_n) begin
            incomplete_frame_seen <= 1'b0;
        end else if ((rx_bit_count != 0) && (rx_bit_count != `PACKET_BITS)) begin
            incomplete_frame_seen <= 1'b1;
        end
    end
endmodule
