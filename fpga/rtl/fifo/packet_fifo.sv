`timescale 1ns/1ps

module packet_fifo #(
    parameter integer WIDTH = 256,
    parameter integer DEPTH = 4
) (
    input  wire                 clk,
    input  wire                 reset_n,
    input  wire                 wr_en,
    input  wire [WIDTH-1:0]     din,
    input  wire                 rd_en,
    output wire [WIDTH-1:0]     dout,
    output wire                 full,
    output wire                 empty,
    output wire [31:0]          overflow_count,
    output wire [31:0]          underflow_count,
    output wire [31:0]          level
);
    localparam integer PTR_WIDTH = (DEPTH <= 2) ? 1 : $clog2(DEPTH);
    localparam integer COUNT_WIDTH = $clog2(DEPTH + 1);

    reg [WIDTH-1:0] storage [0:DEPTH-1];
    reg [PTR_WIDTH-1:0] write_ptr;
    reg [PTR_WIDTH-1:0] read_ptr;
    reg [COUNT_WIDTH-1:0] count;
    reg [31:0] overflow_count_reg;
    reg [31:0] underflow_count_reg;

    assign dout = storage[read_ptr];
    assign full = (count == DEPTH);
    assign empty = (count == 0);
    assign level = count;
    assign overflow_count = overflow_count_reg;
    assign underflow_count = underflow_count_reg;

    function automatic [PTR_WIDTH-1:0] next_ptr(input [PTR_WIDTH-1:0] ptr);
        begin
            if (ptr == DEPTH-1) begin
                next_ptr = {PTR_WIDTH{1'b0}};
            end else begin
                next_ptr = ptr + 1'b1;
            end
        end
    endfunction

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            write_ptr         <= {PTR_WIDTH{1'b0}};
            read_ptr          <= {PTR_WIDTH{1'b0}};
            count             <= {COUNT_WIDTH{1'b0}};
            overflow_count_reg <= 32'd0;
            underflow_count_reg <= 32'd0;
        end else begin
            if (wr_en && full) begin
                overflow_count_reg <= overflow_count_reg + 1'b1;
            end
            if (rd_en && empty) begin
                underflow_count_reg <= underflow_count_reg + 1'b1;
            end

            if (wr_en && !full) begin
                storage[write_ptr] <= din;
                write_ptr <= next_ptr(write_ptr);
            end
            if (rd_en && !empty) begin
                read_ptr <= next_ptr(read_ptr);
            end

            case ({wr_en && !full, rd_en && !empty})
                2'b10: count <= count + 1'b1;
                2'b01: count <= count - 1'b1;
                default: count <= count;
            endcase
        end
    end
endmodule
