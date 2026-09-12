`timescale 1ns/1ps

// Physical-IO-preserving wrappers used by the Gowin resource/timing matrix.
// The production top remains trading_spi_top; each wrapper fixes only the
// NUM_SYMBOLS generic so the vendor flow can build four independent variants.
`define TRADING_MATRIX_WRAPPER(MODULE_NAME, SYMBOL_COUNT)                 \
module MODULE_NAME (                                                       \
    input  wire       clk,                                                 \
    input  wire       spi_clk,                                             \
    input  wire       spi_cs_n,                                            \
    input  wire       spi_mosi,                                            \
    output wire       spi_miso,                                            \
    output wire [5:0] led                                                  \
);                                                                         \
    trading_spi_top #(.NUM_SYMBOLS(SYMBOL_COUNT)) impl (                   \
        .clk(clk), .spi_clk(spi_clk), .spi_cs_n(spi_cs_n),                 \
        .spi_mosi(spi_mosi), .spi_miso(spi_miso), .led(led)                \
    );                                                                     \
endmodule

`TRADING_MATRIX_WRAPPER(trading_spi_top_4, 4)
`TRADING_MATRIX_WRAPPER(trading_spi_top_8, 8)
`TRADING_MATRIX_WRAPPER(trading_spi_top_16, 16)
`TRADING_MATRIX_WRAPPER(trading_spi_top_32, 32)

`undef TRADING_MATRIX_WRAPPER
