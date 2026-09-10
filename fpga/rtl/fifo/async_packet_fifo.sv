`timescale 1ns/1ps

// Dual-clock packet FIFO using Gray-coded pointer crossings.
// DEPTH must be a power of two and at least four.
module async_packet_fifo #(
    parameter integer WIDTH = 256,
    parameter integer DEPTH = 4
) (
    input  wire                 reset_n,
    input  wire                 wr_clk,
    input  wire                 wr_en,
    input  wire [WIDTH-1:0]     din,
    output wire                 wr_full,
    output wire [31:0]          wr_overflow_count,
    input  wire                 rd_clk,
    input  wire                 rd_en,
    output wire [WIDTH-1:0]     dout,
    output wire                 rd_empty,
    output wire [31:0]          rd_underflow_count
);
    localparam integer ADDR_WIDTH = $clog2(DEPTH);
    localparam integer PTR_WIDTH = ADDR_WIDTH + 1;

    reg [WIDTH-1:0] storage [0:DEPTH-1];
    // Configuration-time initialization mirrors the reset state. This keeps
    // the FIFO safe if one clock domain (the external SPI clock) is idle
    // while the FPGA's onboard clock releases the shared reset.
    reg [PTR_WIDTH-1:0] wr_bin = 0, wr_gray = 0, rd_bin = 0, rd_gray = 0;
    reg [PTR_WIDTH-1:0] rd_gray_sync1_wr = 0, rd_gray_sync2_wr = 0;
    reg [PTR_WIDTH-1:0] wr_gray_sync1_rd = 0, wr_gray_sync2_rd = 0;
    reg wr_full_reg = 1'b0, rd_empty_reg = 1'b1;
    reg [31:0] wr_overflow_count_reg = 0, rd_underflow_count_reg = 0;

    wire wr_fire = wr_en && !wr_full_reg;
    wire rd_fire = rd_en && !rd_empty_reg;
    wire [PTR_WIDTH-1:0] wr_bin_next = wr_bin + wr_fire;
    wire [PTR_WIDTH-1:0] rd_bin_next = rd_bin + rd_fire;
    wire [PTR_WIDTH-1:0] wr_gray_next = (wr_bin_next >> 1) ^ wr_bin_next;
    wire [PTR_WIDTH-1:0] rd_gray_next = (rd_bin_next >> 1) ^ rd_bin_next;

    // Full means the next write pointer is one buffer-length ahead of the
    // synchronized read pointer. Invert both Gray-pointer MSBs.
    wire wr_full_next = (wr_gray_next == {
        ~rd_gray_sync2_wr[PTR_WIDTH-1:PTR_WIDTH-2],
        rd_gray_sync2_wr[PTR_WIDTH-3:0]
    });
    wire rd_empty_next = (rd_gray_next == wr_gray_sync2_rd);

    assign wr_full = wr_full_reg;
    assign rd_empty = rd_empty_reg;
    assign wr_overflow_count = wr_overflow_count_reg;
    assign rd_underflow_count = rd_underflow_count_reg;
    assign dout = storage[rd_bin[ADDR_WIDTH-1:0]];

    always @(posedge wr_clk or negedge reset_n) begin
        if (!reset_n) begin
            wr_bin <= 0;
            wr_gray <= 0;
            rd_gray_sync1_wr <= 0;
            rd_gray_sync2_wr <= 0;
            wr_full_reg <= 1'b0;
            wr_overflow_count_reg <= 0;
        end else begin
            rd_gray_sync1_wr <= rd_gray;
            rd_gray_sync2_wr <= rd_gray_sync1_wr;
            if (wr_en && wr_full_reg) wr_overflow_count_reg <= wr_overflow_count_reg + 1'b1;
            if (wr_fire) storage[wr_bin[ADDR_WIDTH-1:0]] <= din;
            wr_bin <= wr_bin_next;
            wr_gray <= wr_gray_next;
            wr_full_reg <= wr_full_next;
        end
    end

    always @(posedge rd_clk or negedge reset_n) begin
        if (!reset_n) begin
            rd_bin <= 0;
            rd_gray <= 0;
            wr_gray_sync1_rd <= 0;
            wr_gray_sync2_rd <= 0;
            rd_empty_reg <= 1'b1;
            rd_underflow_count_reg <= 0;
        end else begin
            wr_gray_sync1_rd <= wr_gray;
            wr_gray_sync2_rd <= wr_gray_sync1_rd;
            if (rd_en && rd_empty_reg) rd_underflow_count_reg <= rd_underflow_count_reg + 1'b1;
            rd_bin <= rd_bin_next;
            rd_gray <= rd_gray_next;
            rd_empty_reg <= rd_empty_next;
        end
    end
endmodule
