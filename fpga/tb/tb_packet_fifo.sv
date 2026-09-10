`timescale 1ns/1ps

module tb_packet_fifo;
    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg wr_en = 1'b0;
    reg rd_en = 1'b0;
    reg [31:0] din = 32'd0;
    wire [31:0] dout;
    wire full;
    wire empty;
    wire [31:0] overflow_count;
    wire [31:0] underflow_count;
    wire [31:0] level;

    always #5 clk = ~clk;

    packet_fifo #(.WIDTH(32), .DEPTH(2)) dut (
        .clk(clk), .reset_n(reset_n), .wr_en(wr_en), .din(din), .rd_en(rd_en),
        .dout(dout), .full(full), .empty(empty),
        .overflow_count(overflow_count), .underflow_count(underflow_count), .level(level)
    );

    task automatic write_word;
        input [31:0] value;
        begin
            @(negedge clk); din = value; wr_en = 1'b1;
            @(negedge clk); wr_en = 1'b0;
        end
    endtask

    initial begin
        repeat (2) @(posedge clk);
        reset_n = 1'b1;
        write_word(32'h11111111);
        write_word(32'h22222222);
        write_word(32'h33333333);
        if (!full) $fatal(1, "FIFO did not report full");
        if (overflow_count != 1) $fatal(1, "FIFO overflow count failed");
        if (dout != 32'h11111111) $fatal(1, "FIFO first value failed");

        @(negedge clk); rd_en = 1'b1;
        @(negedge clk); rd_en = 1'b0;
        if (dout != 32'h22222222) $fatal(1, "FIFO ordering failed");

        @(negedge clk); rd_en = 1'b1;
        @(negedge clk); rd_en = 1'b0;
        if (!empty) $fatal(1, "FIFO did not report empty");

        @(negedge clk); rd_en = 1'b1;
        @(negedge clk); rd_en = 1'b0;
        if (underflow_count != 1) $fatal(1, "FIFO underflow count failed");

        $display("tb_packet_fifo: PASS");
        $finish;
    end
endmodule
