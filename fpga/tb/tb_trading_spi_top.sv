`timescale 1ns/1ps

`include "protocol_defs.svh"

module tb_trading_spi_top;
    reg clk = 1'b0;
    reg reset_n = 1'b1;
    reg spi_clk = 1'b0;
    reg spi_cs_n = 1'b1;
    reg spi_mosi = 1'b0;
    wire spi_miso;

    wire rx_fifo_full;
    wire rx_fifo_empty;
    wire tx_fifo_full;
    wire tx_fifo_empty;
    wire [31:0] rx_fifo_overflow_count;
    wire [31:0] rx_fifo_underflow_count;
    wire [31:0] tx_fifo_overflow_count;
    wire [31:0] tx_fifo_underflow_count;
    wire [31:0] packet_error_count;
    wire incomplete_frame_seen;

    always #5 clk = ~clk;

    trading_spi_top dut (
        .clk(clk),
        .reset_n(reset_n),
        .spi_clk(spi_clk),
        .spi_cs_n(spi_cs_n),
        .spi_mosi(spi_mosi),
        .spi_miso(spi_miso),
        .rx_fifo_full(rx_fifo_full),
        .rx_fifo_empty(rx_fifo_empty),
        .tx_fifo_full(tx_fifo_full),
        .tx_fifo_empty(tx_fifo_empty),
        .rx_fifo_overflow_count(rx_fifo_overflow_count),
        .rx_fifo_underflow_count(rx_fifo_underflow_count),
        .tx_fifo_overflow_count(tx_fifo_overflow_count),
        .tx_fifo_underflow_count(tx_fifo_underflow_count),
        .packet_error_count(packet_error_count),
        .incomplete_frame_seen(incomplete_frame_seen)
    );

    function automatic [255:0] make_request;
        input [31:0] seq_value;
        input [7:0] message_type;
        input [7:0] sync_byte;
        reg [255:0] packet;
        begin
            packet = 256'd0;
            packet[255:248] = sync_byte;
            packet[247:240] = message_type;
            packet[239:224] = 16'h1234;
            packet[223:160] = 64'h0102030405060708;
            packet[159:96]  = 64'hD00D000000000000 | seq_value;
            packet[95:64]   = 32'h0000002A;
            packet[63:56]   = 8'h00;
            packet[55:24]   = seq_value;
            packet[23:8]    = 16'h0000;
            packet[7:0]     = crc8_packet(packet);
            make_request = packet;
        end
    endfunction

    function automatic [7:0] byte_of;
        input [255:0] packet;
        input integer index;
        begin
            byte_of = packet[255-index*8 -: 8];
        end
    endfunction

    task automatic spi_transfer;
        input [255:0] transmit;
        output [255:0] receive;
        integer bit_index;
        begin
            receive = 256'd0;
            spi_cs_n = 1'b0;
            #2;
            for (bit_index = 0; bit_index < 256; bit_index = bit_index + 1) begin
                spi_mosi = transmit[255-bit_index];
                #1;
                spi_clk = 1'b1;
                #1;
                receive[255-bit_index] = spi_miso;
                #1;
                spi_clk = 1'b0;
                #1;
            end
            spi_mosi = 1'b0;
            spi_cs_n = 1'b1;
            #2;
        end
    endtask

    task automatic exchange_loopback;
        input [255:0] request;
        output [255:0] response;
        reg [255:0] ignored;
        begin
            spi_transfer(request, ignored);
            spi_transfer(256'd0, ignored);
            spi_transfer(256'd0, response);
        end
    endtask

    reg [255:0] request;
    reg [255:0] response;
    reg [255:0] ignored;
    integer seq_index;

    initial begin
        // Create a real reset transition in simulation. SPI SCLK is idle while
        // reset is asserted, so relying on an initial value would not clock
        // the FIFO reset flops.
        #1;
        reset_n = 1'b0;
        repeat (2) @(posedge clk);
        reset_n = 1'b1;
        #2;

        request = make_request(32'd17, `MSG_LOOPBACK, `PROTOCOL_SYNC_VERSION);
        exchange_loopback(request, response);
        if (byte_of(response, 0) !== `PROTOCOL_SYNC_VERSION) $fatal(1, "valid response sync failed");
        if (byte_of(response, 1) !== `MSG_STATUS) $fatal(1, "valid response type failed");
        if (byte_of(response, 24) !== `STATUS_OK) $fatal(1, "valid loopback status failed");
        if (response[55:24] !== 32'd17) $fatal(1, "valid loopback sequence failed");
        if (byte_of(response, 31) !== crc8_packet(response)) $fatal(1, "valid response CRC failed");

        for (seq_index = 18; seq_index < 23; seq_index = seq_index + 1) begin
            request = make_request(seq_index, `MSG_LOOPBACK, `PROTOCOL_SYNC_VERSION);
            exchange_loopback(request, response);
            if (byte_of(response, 24) !== `STATUS_OK) $fatal(1, "sequential loopback status failed");
            if (response[55:24] !== seq_index) $fatal(1, "sequential sequence preservation failed");
        end

        request = make_request(32'd99, `MSG_LOOPBACK, `PROTOCOL_SYNC_VERSION);
        request[7:0] = request[7:0] ^ 8'h01;
        exchange_loopback(request, response);
        if (byte_of(response, 24) !== `STATUS_BAD_CHECKSUM) $fatal(1, "bad CRC status failed");
        if (response[55:24] !== 32'd99) $fatal(1, "bad CRC sequence preservation failed");
        if (byte_of(response, 31) !== crc8_packet(response)) $fatal(1, "bad CRC response CRC failed");

        request = make_request(32'd100, 8'h55, `PROTOCOL_SYNC_VERSION);
        exchange_loopback(request, response);
        if (byte_of(response, 24) !== `STATUS_BAD_TYPE) $fatal(1, "bad type status failed");

        request = make_request(32'd101, `MSG_LOOPBACK, 8'h00);
        exchange_loopback(request, response);
        if (byte_of(response, 24) !== `STATUS_BAD_SYNC) $fatal(1, "bad sync status failed");

        // End a transaction before a complete packet. The sticky diagnostic
        // must survive CS deassertion and later complete transfers.
        spi_cs_n = 1'b0;
        repeat (8) begin
            spi_mosi = 1'b0;
            #1; spi_clk = 1'b1; #1; spi_clk = 1'b0; #1;
        end
        spi_cs_n = 1'b1;
        #2;
        if (!incomplete_frame_seen) $fatal(1, "incomplete frame was not detected");

        // Status counters must have observed the malformed complete packets.
        if (packet_error_count < 3) $fatal(1, "packet error counter did not increment");
        if (rx_fifo_full) $fatal(1, "RX FIFO unexpectedly full");
        if (tx_fifo_full) $fatal(1, "TX FIFO unexpectedly full");

        $display("tb_trading_spi_top: PASS");
        $finish;
    end
endmodule
