`timescale 1ns/1ps

// Deterministic configuration-time reset sequencer for the continuously
// running Tang Nano 20K 27 MHz clock. No Raspberry Pi reset wire is required.
module power_on_reset #(
    parameter integer RELEASE_CYCLES = 64
) (
    input  wire clk,
    output wire reset_n
);
    localparam integer COUNT_WIDTH = (RELEASE_CYCLES <= 2) ? 1 : $clog2(RELEASE_CYCLES);

    // Gowin configuration initializes FPGA registers. The 27 MHz clock then
    // keeps reset asserted for a deterministic number of cycles.
    reg [COUNT_WIDTH-1:0] count = 0;
    reg reset_n_reg = 1'b0;

    assign reset_n = reset_n_reg;

    always @(posedge clk) begin
        if (!reset_n_reg) begin
            if (count == RELEASE_CYCLES - 1) reset_n_reg <= 1'b1;
            else count <= count + 1'b1;
        end
    end
endmodule
