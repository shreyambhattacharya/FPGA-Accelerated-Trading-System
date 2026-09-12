`timescale 1ns/1ps

// Deterministic self-checking verification for the reusable divider used by
// VWAP and all normalized feature ratios.
module tb_unsigned_divider;
    localparam integer NUMERATOR_WIDTH = 101;
    localparam integer DENOMINATOR_WIDTH = 64;

    reg clk = 1'b0;
    reg reset_n = 1'b0;
    reg start = 1'b0;
    reg [NUMERATOR_WIDTH-1:0] numerator = {NUMERATOR_WIDTH{1'b0}};
    reg [DENOMINATOR_WIDTH-1:0] denominator = {DENOMINATOR_WIDTH{1'b0}};
    wire busy;
    wire done;
    wire divide_by_zero;
    wire [NUMERATOR_WIDTH-1:0] quotient;
    integer check_count;
    integer random_index;
    reg [31:0] random_word;
    reg [100:0] random_numerator;
    reg [63:0] random_denominator;
    reg [100:0] expected_quotient;

    always #5 clk = ~clk;

    unsigned_divider #(
        .NUMERATOR_WIDTH(NUMERATOR_WIDTH),
        .DENOMINATOR_WIDTH(DENOMINATOR_WIDTH),
        .QUOTIENT_WIDTH(NUMERATOR_WIDTH)
    ) dut (
        .clk(clk), .reset_n(reset_n), .start(start),
        .numerator(numerator), .denominator(denominator),
        .busy(busy), .done(done), .divide_by_zero(divide_by_zero),
        .quotient(quotient)
    );

    task automatic check_division;
        input [100:0] numerator_value;
        input [63:0] denominator_value;
        begin
            while (busy) begin @(posedge clk); #1; end
            @(negedge clk);
            numerator = numerator_value;
            denominator = denominator_value;
            start = 1'b1;
            @(posedge clk); #1;
            start = 1'b0;
            if (denominator_value == 64'd0) begin
                if (!done || !divide_by_zero || quotient !== 101'd0)
                    $fatal(1, "divide-by-zero behavior failed");
            end else begin
                wait (done);
                expected_quotient = numerator_value / denominator_value;
                if (divide_by_zero || quotient !== expected_quotient)
                    $fatal(1, "division mismatch n=%h d=%h got=%h expected=%h",
                           numerator_value, denominator_value, quotient, expected_quotient);
            end
            check_count = check_count + 1;
        end
    endtask

    initial begin
        #12;
        reset_n = 1'b1;
        check_count = 0;

        check_division(101'd0, 64'd1);
        check_division(101'd1, 64'd1);
        check_division({101{1'b1}}, 64'd1);
        check_division({101{1'b1}}, {64{1'b1}});
        check_division(101'd7, 64'd19);
        check_division(101'd100, 64'd10);
        check_division(101'd101, 64'd10);
        check_division(101'd37, 64'd1000);
        check_division(101'd12345, 64'd0);

        // A start while busy must not replace the in-flight operation.
        @(negedge clk);
        numerator = 101'd1000003;
        denominator = 64'd97;
        start = 1'b1;
        @(posedge clk); #1;
        start = 1'b0;
        @(negedge clk);
        numerator = 101'd9;
        denominator = 64'd2;
        start = 1'b1;
        @(posedge clk); #1;
        start = 1'b0;
        wait (done);
        if (quotient !== (101'd1000003 / 64'd97))
            $fatal(1, "start-while-busy replaced the operation");
        check_count = check_count + 1;

        // Reset while busy must return to a clean idle state.
        @(negedge clk);
        numerator = {101{1'b1}};
        denominator = 64'd3;
        start = 1'b1;
        @(posedge clk); #1;
        start = 1'b0;
        #30;
        reset_n = 1'b0;
        #2;
        if (busy || done || quotient !== 101'd0)
            $fatal(1, "reset while busy did not clear divider");
        reset_n = 1'b1;

        // Fixed-seed deterministic random coverage, including denominators
        // representative of VWAP, imbalance, and normalized bps operands.
        random_word = 32'h1357_9BDF;
        for (random_index = 0; random_index < 5000; random_index = random_index + 1) begin
            random_word = {random_word[30:0], random_word[31] ^ random_word[21] ^
                           random_word[1] ^ random_word[0]};
            random_numerator = {random_word, random_word ^ 32'hA5A5_5A5A,
                                random_word + random_index, random_word ^ 32'hC3C3_3C3C,
                                random_word[4:0]};
            random_denominator = {random_word, random_word ^ 32'h5A5A_A5A5};
            if (random_denominator == 64'd0)
                random_denominator = 64'd1;
            check_division(random_numerator, random_denominator);
        end

        $display("tb_unsigned_divider: PASS cases=%0d", check_count);
        $finish;
    end
endmodule
