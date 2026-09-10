`timescale 1ns/1ps

module tb_async_packet_fifo;
    localparam integer DEPTH = 4;
    localparam integer COUNT = 64;

    reg reset_n = 1'b0;
    reg wr_clk = 1'b0;
    reg rd_clk = 1'b0;
    reg wr_en = 1'b0;
    reg rd_en = 1'b0;
    reg [255:0] din = 0;
    wire [255:0] dout;
    wire wr_full;
    wire rd_empty;
    wire [31:0] wr_overflow_count;
    wire [31:0] rd_underflow_count;

    always #18.5185 wr_clk = ~wr_clk; // approximately 27 MHz
    always #73.5 rd_clk = ~rd_clk;    // intentionally unrelated read clock

    async_packet_fifo #(.WIDTH(256), .DEPTH(DEPTH)) dut (
        .reset_n(reset_n),
        .wr_clk(wr_clk), .wr_en(wr_en), .din(din), .wr_full(wr_full),
        .wr_overflow_count(wr_overflow_count),
        .rd_clk(rd_clk), .rd_en(rd_en), .dout(dout), .rd_empty(rd_empty),
        .rd_underflow_count(rd_underflow_count)
    );

    reg [255:0] expected [0:COUNT-1];
    integer i;
    integer write_index;
    integer read_loop;
    integer read_index;
    reg [31:0] lfsr;

    function automatic [31:0] next_lfsr(input [31:0] state);
        begin
            next_lfsr = {state[30:0], state[31] ^ state[21] ^ state[1] ^ state[0]};
        end
    endfunction

    task automatic write_value;
        input [255:0] value;
        begin
            wait (!wr_full);
            @(negedge wr_clk);
            din = value;
            wr_en = 1'b1;
            @(negedge wr_clk);
            wr_en = 1'b0;
        end
    endtask

    task automatic read_expected;
        input integer expected_index;
        begin
            wait (!rd_empty);
            @(negedge rd_clk);
            if (dout !== expected[expected_index]) $fatal(1, "async FIFO data/order mismatch at %0d", read_index);
            rd_en = 1'b1;
            @(negedge rd_clk);
            rd_en = 1'b0;
            read_index = read_index + 1;
        end
    endtask

    task automatic writer_sequence;
        reg [255:0] value;
        begin
            lfsr = 32'h1ACE_B00C;
            for (write_index = 0; write_index < COUNT; write_index = write_index + 1) begin
                lfsr = next_lfsr(lfsr);
                value = {192'h0, lfsr, write_index[31:0]};
                expected[write_index] = value;
                write_value(value);
                repeat (write_index % 3) @(negedge wr_clk);
            end
        end
    endtask

    task automatic reader_sequence;
        begin
            read_index = 0;
            for (read_loop = 0; read_loop < COUNT; read_loop = read_loop + 1) begin
                read_expected(read_loop);
                repeat (read_loop % 4) @(negedge rd_clk);
            end
        end
    endtask

    initial begin
        #300;
        reset_n = 1'b1;
        #100;

        if (!rd_empty) $fatal(1, "FIFO not empty after reset");
        if (wr_full) $fatal(1, "FIFO full after reset");

        // Explicit underflow attempt.
        @(negedge rd_clk); rd_en = 1'b1;
        @(negedge rd_clk); rd_en = 1'b0;
        if (rd_underflow_count != 1) $fatal(1, "underflow counter failed");

        // Fill, verify full, then attempt one extra write.
        for (i = 0; i < DEPTH; i = i + 1) begin
            write_value({224'h0, i[31:0]});
        end
        if (!wr_full) $fatal(1, "FIFO did not become full");
        @(negedge wr_clk); din = 256'hDEAD; wr_en = 1'b1;
        @(negedge wr_clk); wr_en = 1'b0;
        if (wr_overflow_count != 1) $fatal(1, "overflow counter failed");

        // Reset while traffic is resident; both pointer domains must restart.
        #10 reset_n = 1'b0;
        #200 reset_n = 1'b1;
        #300;
        if (!rd_empty || wr_full) $fatal(1, "reset did not clear async FIFO");

        fork
            writer_sequence();
            reader_sequence();
        join

        #500;
        if (read_index != COUNT) $fatal(1, "reader did not consume all packets");
        if (!rd_empty) $fatal(1, "FIFO not empty after randomized sequence");
        if (wr_overflow_count != 0 || rd_underflow_count != 0)
            $fatal(1, "unexpected traffic overflow/underflow");

        $display("tb_async_packet_fifo: PASS");
        $finish;
    end
endmodule
