`ifndef PROTOCOL_DEFS_SVH
`define PROTOCOL_DEFS_SVH

`define PACKET_BYTES 32
`define PACKET_BITS  256

`define PROTOCOL_SYNC_VERSION 8'hA1

`define MSG_MARKET_QUOTE 8'h01
`define MSG_MARKET_TRADE 8'h02
`define MSG_CONTROL      8'h03
`define MSG_HEARTBEAT    8'h04
`define MSG_SIGNAL       8'h05
`define MSG_STATUS       8'h06
`define MSG_LOOPBACK     8'h07

`define STATUS_OK             8'h01
`define STATUS_BAD_SYNC       8'hE1
`define STATUS_BAD_CHECKSUM   8'hE2
`define STATUS_BAD_TYPE       8'hE3

// CRC-8/ATM: poly 0x07, init 0, MSB-first, no reflection/final XOR.
function automatic [7:0] crc8_packet(input [`PACKET_BITS-1:0] packet);
    reg [7:0] crc;
    reg [7:0] current_byte;
    integer byte_index;
    integer bit_index;
    begin
        crc = 8'h00;
        for (byte_index = 0; byte_index < (`PACKET_BYTES - 1); byte_index = byte_index + 1) begin
            current_byte = packet[`PACKET_BITS-1-byte_index*8 -: 8];
            crc = crc ^ current_byte;
            for (bit_index = 0; bit_index < 8; bit_index = bit_index + 1) begin
                if (crc[7]) begin
                    crc = (crc << 1) ^ 8'h07;
                end else begin
                    crc = crc << 1;
                end
            end
        end
        crc8_packet = crc;
    end
endfunction

function automatic [7:0] packet_byte(
    input [`PACKET_BITS-1:0] packet,
    input integer index
);
    begin
        packet_byte = packet[`PACKET_BITS-1-index*8 -: 8];
    end
endfunction

function automatic [`PACKET_BITS-1:0] set_packet_byte(
    input [`PACKET_BITS-1:0] packet,
    input integer index,
    input [7:0] value
);
    reg [`PACKET_BITS-1:0] next_packet;
    begin
        next_packet = packet;
        next_packet[`PACKET_BITS-1-index*8 -: 8] = value;
        set_packet_byte = next_packet;
    end
endfunction

`endif
