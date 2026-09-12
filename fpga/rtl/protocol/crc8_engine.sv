`timescale 1ns/1ps

// Byte-stream CRC-8/ATM engine.
// Polynomial 0x07, initial value 0, MSB-first, no reflection, no final XOR.
// A start may coincide with the first data byte. done asserts after the clock
// edge that accepts data_last, and result holds that completed CRC.
module crc8_engine (
    input  wire       clk,
    input  wire       reset_n,
    input  wire       start,
    input  wire       data_valid,
    input  wire       data_last,
    input  wire [7:0] data_byte,
    output reg        done,
    output reg  [7:0] result
);
    reg [7:0] crc_reg = 8'h00;
    reg [7:0] next_crc;

    function automatic [7:0] crc8_next(
        input [7:0] crc_in,
        input [7:0] byte_in
    );
        reg [7:0] crc;
        integer bit_index;
        begin
            crc = crc_in ^ byte_in;
            for (bit_index = 0; bit_index < 8; bit_index = bit_index + 1) begin
                if (crc[7]) crc = (crc << 1) ^ 8'h07;
                else crc = crc << 1;
            end
            crc8_next = crc;
        end
    endfunction

    always @* begin
        next_crc = crc8_next(start ? 8'h00 : crc_reg, data_byte);
    end

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            crc_reg <= 8'h00;
            result <= 8'h00;
            done <= 1'b0;
        end else begin
            done <= 1'b0;
            if (start && !data_valid) crc_reg <= 8'h00;
            if (data_valid) begin
                crc_reg <= next_crc;
                if (data_last) begin
                    result <= next_crc;
                    done <= 1'b1;
                end
            end
        end
    end
endmodule
