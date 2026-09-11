`timescale 1ns/1ps

`include "protocol_defs.svh"

// Synchronous packet-processing stage in the 27 MHz system-clock domain.
// The response register provides one packet of backpressure between the
// asynchronous RX and TX FIFOs.
module loopback_engine (
    input  wire                    clk,
    input  wire                    reset_n,
    input  wire                    packet_valid,
    output wire                    packet_ready,
    input  wire [`PACKET_BITS-1:0] packet_in,
    input  wire                    response_ready,
    output wire                    response_valid,
    output reg  [`PACKET_BITS-1:0] response_packet,
    output reg                     packet_error_pulse
);
    import protocol_pkg::*;
    reg response_valid_reg;
    wire packet_is_filler = (packet_in == {`PACKET_BITS{1'b0}});
    wire accepted = packet_valid && packet_ready;

    // The host uses all-zero clocking packets for the turnaround and response
    // transfers. They are deliberately consumed without generating a status
    // packet or error, so the three-transfer protocol does not fill the TX
    // FIFO with responses to its own dummy clocks.
    assign packet_ready = packet_is_filler || !response_valid_reg || response_ready;
    assign response_valid = response_valid_reg;

    function automatic [`PACKET_BITS-1:0] make_response(input [`PACKET_BITS-1:0] request);
        reg [`PACKET_BITS-1:0] response;
        reg [7:0] status;
        reg [15:0] response_flags;
        begin
            response = request;
            response = set_packet_byte(response, 0, `PROTOCOL_SYNC_VERSION);
            response = set_packet_byte(response, 1, `MSG_STATUS);

            if (packet_byte(request, 0) != `PROTOCOL_SYNC_VERSION) begin
                status = `STATUS_BAD_SYNC;
                response_flags = 16'h0002;
            end else if (packet_byte(request, 31) != crc8_packet(request)) begin
                status = `STATUS_BAD_CHECKSUM;
                response_flags = 16'h0004;
            end else if (packet_byte(request, 1) != `MSG_LOOPBACK) begin
                status = `STATUS_BAD_TYPE;
                response_flags = 16'h0008;
            end else begin
                status = `STATUS_OK;
                response_flags = 16'h0001;
            end

            response = set_packet_byte(response, 24, status);
            response = set_packet_byte(response, 29, response_flags[15:8]);
            response = set_packet_byte(response, 30, response_flags[7:0]);
            response = set_packet_byte(response, 31, 8'h00);
            response = set_packet_byte(response, 31, crc8_packet(response));
            make_response = response;
        end
    endfunction

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            response_valid_reg <= 1'b0;
            response_packet <= {`PACKET_BITS{1'b0}};
            packet_error_pulse <= 1'b0;
        end else begin
            packet_error_pulse <= 1'b0;
            if (response_valid_reg && response_ready) response_valid_reg <= 1'b0;
            if (accepted && !packet_is_filler) begin
                response_packet <= make_response(packet_in);
                response_valid_reg <= 1'b1;
                packet_error_pulse <=
                    (packet_byte(packet_in, 0) != `PROTOCOL_SYNC_VERSION) ||
                    (packet_byte(packet_in, 31) != crc8_packet(packet_in)) ||
                    (packet_byte(packet_in, 1) != `MSG_LOOPBACK);
            end
        end
    end
endmodule
