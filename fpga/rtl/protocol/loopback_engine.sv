`timescale 1ns/1ps

`include "protocol_defs.svh"

// Packet-level processing stage. It consumes one RX FIFO packet only when the
// TX FIFO can accept the generated response, keeping backpressure explicit.
module loopback_engine (
    input  wire                    packet_valid,
    output wire                    packet_ready,
    input  wire [`PACKET_BITS-1:0] packet_in,
    input  wire                    response_ready,
    output wire                    response_wr_en,
    output wire [`PACKET_BITS-1:0] response_packet,
    output wire                    packet_error
);
    wire good_sync;
    wire good_crc;
    wire good_type;
    wire accepted;

    assign good_sync = (packet_byte(packet_in, 0) == `PROTOCOL_SYNC_VERSION);
    assign good_crc  = (packet_byte(packet_in, 31) == crc8_packet(packet_in));
    assign good_type = (packet_byte(packet_in, 1) == `MSG_LOOPBACK);

    assign packet_ready = response_ready;
    assign accepted = packet_valid && response_ready;
    assign response_wr_en = accepted;
    assign packet_error = accepted && !(good_sync && good_crc && good_type);

    function automatic [`PACKET_BITS-1:0] make_response(input [`PACKET_BITS-1:0] request);
        reg [`PACKET_BITS-1:0] response;
        reg [7:0] status;
        reg [15:0] response_flags;
        integer i;
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

    assign response_packet = make_response(packet_in);
endmodule
