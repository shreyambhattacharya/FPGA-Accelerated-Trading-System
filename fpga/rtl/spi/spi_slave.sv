`timescale 1ns/1ps

`include "protocol_defs.svh"

// SPI mode-0 slave. Persistent request/turnaround/response state is owned by
// posedge spi_clk. CS is only combined with reset_n to clear per-frame
// shift/count state; it never changes the persistent protocol phase.
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
    output reg                      packet_complete_toggle,

    input  wire [`PACKET_BITS-1:0] tx_packet_data,
    input  wire                     tx_packet_valid,
    output wire                     tx_packet_rd_en,
    output reg                      tx_empty_transaction_pulse
);
    localparam [1:0] WAIT_REQUEST    = 2'd0;
    localparam [1:0] WAIT_TURNAROUND = 2'd1;
    localparam [1:0] RESPONSE_READY  = 2'd2;

    reg [1:0] phase = WAIT_REQUEST;

    // Per-frame state. These registers are reset only to fixed constants when
    // CS is inactive; they do not own protocol scheduling decisions.
    reg [`PACKET_BITS-1:0] rx_shift = 0;
    reg [8:0] rx_bit_count = 0;
    reg [`PACKET_BITS-1:0] tx_shift = 0;
    reg [8:0] tx_bit_index = 0;
    reg tx_started = 1'b0;
    reg tx_has_data = 1'b0;

    // Configuration-time values are required because the Pi can hold SCLK
    // idle while the FPGA powers up and releases the shared system reset.
    initial begin
        rx_packet = 0;
        rx_packet_valid = 1'b0;
        rx_overflow_pulse = 1'b0;
        packet_complete_toggle = 1'b0;
        tx_empty_transaction_pulse = 1'b0;
    end

    // CS is used here only as a per-frame asynchronous clear. The persistent
    // phase, packet holding register, and diagnostic pulses are not assigned
    // in this block or in any CS edge-sensitive block.
    wire frame_reset_n = reset_n && !cs_n;
    wire [`PACKET_BITS-1:0] completed_frame =
        {rx_shift[`PACKET_BITS-2:0], mosi};
    wire frame_complete = (rx_bit_count == (`PACKET_BITS - 1));
    wire transaction_start = !cs_n && !tx_started && (rx_bit_count == 0);
    wire response_transaction = (phase == RESPONSE_READY);

    // The TX FIFO is read only once, at the first rising edge of the response
    // transaction. In request and turnaround phases MISO is deterministic zero.
    assign tx_packet_rd_en = transaction_start && response_transaction &&
                             tx_packet_valid;
    assign miso = !cs_n ?
                  (tx_started ?
                      (tx_has_data ? tx_shift[`PACKET_BITS-1-tx_bit_index] : 1'b0) :
                      (response_transaction && tx_packet_valid ?
                          tx_packet_data[`PACKET_BITS-1] : 1'b0)) :
                  1'b0;

    // Per-frame receive/transmit state. CS only restores fixed frame state;
    // all protocol transitions are in the persistent posedge block below.
    always @(posedge spi_clk or negedge frame_reset_n) begin
        if (!frame_reset_n) begin
            rx_shift <= 0;
            rx_bit_count <= 0;
            tx_shift <= 0;
            tx_started <= 1'b0;
            tx_has_data <= 1'b0;
        end else begin
            if (transaction_start) begin
                tx_started <= 1'b1;
                tx_has_data <= response_transaction && tx_packet_valid;
                if (response_transaction && tx_packet_valid)
                    tx_shift <= tx_packet_data;
            end

            if (rx_bit_count < `PACKET_BITS) begin
                rx_shift <= completed_frame;
                rx_bit_count <= rx_bit_count + 1'b1;
            end
        end
    end

    // MISO advances after the master samples the current bit, which preserves
    // CPOL=0/CPHA=0 timing and leaves the first bit valid before the first
    // response rising edge.
    always @(negedge spi_clk or negedge frame_reset_n) begin
        if (!frame_reset_n) begin
            tx_bit_index <= 0;
        end else if (tx_started && (tx_bit_index < (`PACKET_BITS - 1))) begin
            tx_bit_index <= tx_bit_index + 1'b1;
        end
    end

    // Persistent protocol state. The completed frame is evaluated on its final
    // rising edge, so no CS edge is needed to classify request or turnaround.
    always @(posedge spi_clk or negedge reset_n) begin
        if (!reset_n) begin
            phase <= WAIT_REQUEST;
            rx_packet <= 0;
            rx_packet_valid <= 1'b0;
            rx_overflow_pulse <= 1'b0;
            packet_complete_toggle <= 1'b0;
            tx_empty_transaction_pulse <= 1'b0;
        end else begin
            rx_overflow_pulse <= 1'b0;
            tx_empty_transaction_pulse <= 1'b0;

            if (!cs_n) begin
                if (rx_packet_valid && rx_packet_ready)
                    rx_packet_valid <= 1'b0;

                if (transaction_start && response_transaction && !tx_packet_valid)
                    tx_empty_transaction_pulse <= 1'b1;

                if (frame_complete) begin
                    rx_packet <= completed_frame;
                    packet_complete_toggle <= ~packet_complete_toggle;

                    if (rx_packet_valid && !rx_packet_ready) begin
                        rx_overflow_pulse <= 1'b1;
                    end else begin
                        rx_packet_valid <= 1'b1;
                    end

                    case (phase)
                    WAIT_REQUEST:
                        if (completed_frame != {`PACKET_BITS{1'b0}})
                            phase <= WAIT_TURNAROUND;
                    WAIT_TURNAROUND:
                        if (completed_frame == {`PACKET_BITS{1'b0}})
                            phase <= RESPONSE_READY;
                    RESPONSE_READY:
                        phase <= WAIT_REQUEST;
                    default:
                        phase <= WAIT_REQUEST;
                    endcase
                end
            end
        end
    end
endmodule
