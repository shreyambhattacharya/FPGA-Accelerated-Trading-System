`timescale 1ns/1ps

`include "protocol_defs.svh"

// SPI mode-0 slave. MOSI is sampled on rising SCLK; MISO advances on falling
// SCLK. CS is an asynchronous frame boundary, which lets the slave recognize
// an incomplete transaction even when the master stops clocking immediately.
module spi_slave (
    input  wire                     reset_n,
    input  wire                     spi_clk,
    input  wire                     cs_n,
    input  wire                     mosi,
    output wire                     miso,

    output reg  [`PACKET_BITS-1:0] rx_packet,
    output reg                      rx_packet_valid,
    input  wire                     rx_packet_ready,
    output reg                      rx_overflow_pulse,
    output reg                      incomplete_frame_seen,
    output reg                      packet_complete_toggle,

    input  wire [`PACKET_BITS-1:0] tx_packet_data,
    input  wire                     tx_packet_valid,
    output wire                     tx_packet_rd_en,
    output reg                      tx_empty_transaction_pulse
);
    // These configuration-time values are also the reset values. They are
    // important when the Raspberry Pi holds SCLK idle while the FPGA powers
    // up: the SPI domain may not receive a clock edge until after reset is
    // released.
    reg [`PACKET_BITS-1:0] rx_shift = 0;
    reg [8:0] rx_bit_count = 0;
    reg [`PACKET_BITS-1:0] tx_shift = 0;
    reg [8:0] tx_bit_index = 0;
    reg tx_started = 1'b0;
    reg tx_has_data = 1'b0;
    reg tx_allowed_for_frame = 1'b0;
    reg response_pending = 1'b0;

    initial begin
        rx_packet = 0;
        rx_packet_valid = 1'b0;
        rx_overflow_pulse = 1'b0;
        incomplete_frame_seen = 1'b0;
        packet_complete_toggle = 1'b0;
        tx_empty_transaction_pulse = 1'b0;
    end

    // Before the first rising edge, the FIFO head supplies MISO bit 7. At
    // that rising edge the complete TX packet is latched for the frame.
    assign miso = !cs_n ?
                  (tx_started ? (tx_has_data ? tx_shift[`PACKET_BITS-1-tx_bit_index] : 1'b0) :
                                (tx_allowed_for_frame && tx_packet_valid ?
                                    tx_packet_data[`PACKET_BITS-1] : 1'b0)) :
                  1'b0;

    // The host protocol is request / turnaround / response. A response that
    // becomes available during the turnaround frame must remain queued until
    // the following frame, so only an explicitly eligible frame can dequeue
    // the TX FIFO.
    assign tx_packet_rd_en = !cs_n && !tx_started && (rx_bit_count == 0) &&
                             tx_allowed_for_frame && tx_packet_valid;

    // Frame state is reset by CS deassertion as well as global reset. The
    // complete-packet holding register intentionally survives CS high until
    // the asynchronous RX FIFO accepts it on a later SPI clock.
    always @(posedge spi_clk or negedge reset_n or posedge cs_n) begin
        if (!reset_n) begin
            rx_shift <= 0;
            rx_packet <= 0;
            rx_packet_valid <= 1'b0;
            rx_overflow_pulse <= 1'b0;
            packet_complete_toggle <= 1'b0;
            rx_bit_count <= 0;
            tx_shift <= 0;
            tx_started <= 1'b0;
            tx_has_data <= 1'b0;
            tx_allowed_for_frame <= 1'b0;
            response_pending <= 1'b0;
            tx_empty_transaction_pulse <= 1'b0;
        end else if (cs_n) begin
            rx_shift <= 0;
            rx_bit_count <= 0;
            tx_started <= 1'b0;
            tx_has_data <= 1'b0;

            // A complete nonzero packet is a request. The next complete
            // zero-filled frame is the turnaround; only after it ends is the
            // next frame allowed to dequeue a response.
            if (response_pending && (rx_bit_count == `PACKET_BITS) &&
                (rx_packet == {`PACKET_BITS{1'b0}})) begin
                tx_allowed_for_frame <= 1'b1;
                response_pending <= 1'b0;
            end
        end else begin
            rx_overflow_pulse <= 1'b0;
            tx_empty_transaction_pulse <= 1'b0;

            if (rx_packet_valid && rx_packet_ready) rx_packet_valid <= 1'b0;

            if (!tx_started && (rx_bit_count == 0)) begin
                tx_started <= 1'b1;
                tx_allowed_for_frame <= 1'b0;
                tx_has_data <= tx_allowed_for_frame && tx_packet_valid;
                if (tx_allowed_for_frame && tx_packet_valid)
                    tx_shift <= tx_packet_data;
                else if (tx_allowed_for_frame)
                    tx_empty_transaction_pulse <= 1'b1;
            end

            if (rx_bit_count < `PACKET_BITS) begin
                rx_shift <= {rx_shift[`PACKET_BITS-2:0], mosi};
                rx_bit_count <= rx_bit_count + 1'b1;
                if (rx_bit_count == (`PACKET_BITS - 1)) begin
                    rx_packet <= {rx_shift[`PACKET_BITS-2:0], mosi};
                    packet_complete_toggle <= ~packet_complete_toggle;
                    if ({rx_shift[`PACKET_BITS-2:0], mosi} !=
                        {`PACKET_BITS{1'b0}})
                        response_pending <= 1'b1;
                    if (rx_packet_valid && !rx_packet_ready) begin
                        rx_overflow_pulse <= 1'b1;
                    end else begin
                        rx_packet_valid <= 1'b1;
                    end
                end
            end
        end
    end

    // Mode-0 transmit data changes on falling SCLK edges.
    always @(negedge spi_clk or negedge reset_n or posedge cs_n) begin
        if (!reset_n) tx_bit_index <= 0;
        else if (cs_n) tx_bit_index <= 0;
        else if (tx_started && (tx_bit_index < (`PACKET_BITS - 1)))
            tx_bit_index <= tx_bit_index + 1'b1;
    end

    // Sticky diagnostic for a CS frame that ended after a nonzero, incomplete
    // number of bits. This is not part of the latency datapath.
    always @(posedge cs_n or negedge reset_n) begin
        if (!reset_n) incomplete_frame_seen <= 1'b0;
        else if ((rx_bit_count != 0) && (rx_bit_count != `PACKET_BITS))
            incomplete_frame_seen <= 1'b1;
    end
endmodule
