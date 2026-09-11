`timescale 1ns/1ps

`include "protocol_defs.svh"

// Synthesis-safe SystemVerilog package for shared packet helpers. Keeping
// these helpers out of compilation-unit scope avoids Gowin EX3209 warnings
// when the project is compiled explicitly as SystemVerilog 2017.
package protocol_pkg;
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
                    if (crc[7]) crc = (crc << 1) ^ 8'h07;
                    else crc = crc << 1;
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
endpackage
