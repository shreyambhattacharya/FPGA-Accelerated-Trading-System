`timescale 1ns/1ps

`include "protocol_defs.svh"

module tb_crc8_engine;
    import protocol_pkg::*;

    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg start = 1'b0;
    reg data_valid = 1'b0;
    reg data_last = 1'b0;
    reg [7:0] data_byte = 8'h00;
    wire done;
    wire [7:0] result;

    always #5 clk = ~clk;

    crc8_engine dut (
        .clk(clk),
        .reset_n(reset_n),
        .start(start),
        .data_valid(data_valid),
        .data_last(data_last),
        .data_byte(data_byte),
        .done(done),
        .result(result)
    );

    function automatic [255:0] make_response_base;
        reg [255:0] packet;
        begin
            packet = 256'd0;
            packet[255:248] = `PROTOCOL_SYNC_VERSION;
            packet[247:240] = `MSG_STATUS;
            packet[63:56] = `STATUS_OK;
            packet[31:24] = 8'h00;
            packet[23:16] = 8'h01;
            make_response_base = packet;
        end
    endfunction

    task automatic check_packet;
        input [255:0] packet;
        input [7:0] expected;
        input integer case_id;
        integer byte_index;
        reg [7:0] reference;
        begin
            reference = crc8_packet(packet);
            if (reference !== expected) begin
                $display("FAIL case %0d: reference expected %02h, got %02h", case_id, expected, reference);
                $fatal(1);
            end

            for (byte_index = 0; byte_index < (`PACKET_BYTES - 1); byte_index = byte_index + 1) begin
                @(negedge clk);
                start = (byte_index == 0);
                data_valid = 1'b1;
                data_last = (byte_index == (`PACKET_BYTES - 2));
                data_byte = packet[`PACKET_BITS-1-byte_index*8 -: 8];
                @(posedge clk);
            end
            #1;
            if (!done || result !== expected) begin
                $display("FAIL case %0d: engine done=%b result=%02h expected=%02h", case_id, done, result, expected);
                $fatal(1);
            end

            @(negedge clk);
            start = 1'b0;
            data_valid = 1'b0;
            data_last = 1'b0;
            data_byte = 8'h00;
            @(posedge clk);
            #1;
            if (done) begin
                $display("FAIL case %0d: done did not pulse", case_id);
                $fatal(1);
            end
        end
    endtask

    reg [255:0] random_packet;
    reg [7:0] lfsr;
    integer random_case;
    integer random_byte;

    initial begin
        repeat (2) @(posedge clk);
        reset_n = 1'b1;

        // Known protocol vector, including an independently checked CRC-8/ATM value.
        check_packet(256'hA10712340102030405060708D00D0000000000000000002A0000000000000000,
                     8'hB5, 1);

        // The CRC of a 31-byte all-zero stream is zero for this CRC convention.
        check_packet(256'h0000000000000000000000000000000000000000000000000000000000000000,
                     8'h00, 2);

        // Request and response-shaped streams exercise the same engine in both directions.
        check_packet(256'hA10712340102030405060708D00D0000000000000000002A0000000000000000,
                     crc8_packet(256'hA10712340102030405060708D00D0000000000000000002A0000000000000000), 3);
        check_packet(make_response_base(), crc8_packet(make_response_base()), 4);

        // Reset in the middle of a stream, then verify that the next packet starts cleanly.
        for (random_byte = 0; random_byte < 8; random_byte = random_byte + 1) begin
            @(negedge clk);
            start = (random_byte == 0);
            data_valid = 1'b1;
            data_last = 1'b0;
            data_byte = 8'hA5 ^ random_byte[7:0];
            @(posedge clk);
        end
        @(negedge clk);
        start = 1'b0;
        data_valid = 1'b0;
        data_last = 1'b0;
        reset_n = 1'b0;
        repeat (2) @(posedge clk);
        reset_n = 1'b1;
        check_packet(256'hA10712340102030405060708D00D0000000000000000002A0000000000000000,
                     8'hB5, 5);

        // Deterministic pseudo-random coverage plus an immediate back-to-back pair.
        check_packet(256'h0000000000000000000000000000000000000000000000000000000000000000,
                     8'h00, 6);
        lfsr = 8'h1D;
        for (random_case = 0; random_case < 8; random_case = random_case + 1) begin
            random_packet = 256'd0;
            for (random_byte = 0; random_byte < (`PACKET_BYTES - 1); random_byte = random_byte + 1) begin
                random_packet[`PACKET_BITS-1-random_byte*8 -: 8] = lfsr;
                lfsr = {lfsr[6:0], lfsr[7] ^ lfsr[5] ^ lfsr[4] ^ lfsr[3]};
            end
            check_packet(random_packet, crc8_packet(random_packet), 10 + random_case);
        end

        $display("PASS: CRC engine known, zero, request/response, reset, back-to-back, and random tests");
        $finish;
    end
endmodule
