`timescale 1ns/1ps

// Bit-serial restoring unsigned divider.
//
// A start while busy is ignored. For a non-zero denominator the divider takes
// exactly NUMERATOR_WIDTH clock edges after the start edge, then pulses done
// for one cycle and holds quotient. A zero denominator completes immediately
// with divide_by_zero asserted and quotient cleared.
module unsigned_divider #(
    parameter integer NUMERATOR_WIDTH = 101,
    parameter integer DENOMINATOR_WIDTH = 64,
    parameter integer QUOTIENT_WIDTH = NUMERATOR_WIDTH
) (
    input wire                         clk,
    input wire                         reset_n,
    input wire                         start,
    input wire [NUMERATOR_WIDTH-1:0]   numerator,
    input wire [DENOMINATOR_WIDTH-1:0] denominator,
    output reg                         busy,
    output reg                         done,
    output reg                         divide_by_zero,
    output reg [QUOTIENT_WIDTH-1:0]    quotient
);
    localparam integer ITERATION_WIDTH = (NUMERATOR_WIDTH <= 1) ? 1 : $clog2(NUMERATOR_WIDTH + 1);

    reg [NUMERATOR_WIDTH-1:0] numerator_shift;
    reg [QUOTIENT_WIDTH-1:0] quotient_work;
    reg [DENOMINATOR_WIDTH-1:0] denominator_reg;
    reg [DENOMINATOR_WIDTH:0] remainder_reg;
    reg [ITERATION_WIDTH-1:0] iteration_reg;

    wire [DENOMINATOR_WIDTH:0] shifted_remainder =
        {remainder_reg[DENOMINATOR_WIDTH-1:0], numerator_shift[NUMERATOR_WIDTH-1]};
    wire quotient_bit = shifted_remainder >= {1'b0, denominator_reg};
    wire [DENOMINATOR_WIDTH:0] reduced_remainder =
        quotient_bit ? shifted_remainder - {1'b0, denominator_reg} : shifted_remainder;
    wire [QUOTIENT_WIDTH-1:0] shifted_quotient =
        (quotient_work << 1) | quotient_bit;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            busy <= 1'b0;
            done <= 1'b0;
            divide_by_zero <= 1'b0;
            quotient <= {QUOTIENT_WIDTH{1'b0}};
            numerator_shift <= {NUMERATOR_WIDTH{1'b0}};
            quotient_work <= {QUOTIENT_WIDTH{1'b0}};
            denominator_reg <= {DENOMINATOR_WIDTH{1'b0}};
            remainder_reg <= {(DENOMINATOR_WIDTH + 1){1'b0}};
            iteration_reg <= {ITERATION_WIDTH{1'b0}};
        end else begin
            done <= 1'b0;
            divide_by_zero <= 1'b0;

            if (!busy) begin
                if (start) begin
                    if (denominator == {DENOMINATOR_WIDTH{1'b0}}) begin
                        quotient <= {QUOTIENT_WIDTH{1'b0}};
                        divide_by_zero <= 1'b1;
                        done <= 1'b1;
                    end else begin
                        busy <= 1'b1;
                        numerator_shift <= numerator;
                        quotient_work <= {QUOTIENT_WIDTH{1'b0}};
                        denominator_reg <= denominator;
                        remainder_reg <= {(DENOMINATOR_WIDTH + 1){1'b0}};
                        iteration_reg <= {ITERATION_WIDTH{1'b0}};
                    end
                end
            end else begin
                numerator_shift <= numerator_shift << 1;
                remainder_reg <= reduced_remainder;
                quotient_work <= shifted_quotient;

                if (iteration_reg == NUMERATOR_WIDTH - 1) begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    quotient <= shifted_quotient;
                end else begin
                    iteration_reg <= iteration_reg + 1'b1;
                end
            end
        end
    end
endmodule
