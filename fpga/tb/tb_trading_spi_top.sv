`timescale 1ns/1ps

`include "protocol_defs.svh"

module tb_trading_spi_top;
    import protocol_pkg::*;
    reg clk = 1'b0;
    reg spi_clk = 1'b0;
    reg spi_cs_n = 1'b1;
    reg spi_mosi = 1'b0;
    wire spi_miso;
    wire [5:0] led;

    // 27 MHz system clock and unrelated 5 MHz SPI clock.
    always #18.5185 clk = ~clk;

    trading_spi_top dut (
        .clk(clk),
        .spi_clk(spi_clk),
        .spi_cs_n(spi_cs_n),
        .spi_mosi(spi_mosi),
        .spi_miso(spi_miso),
        .led(led)
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

    function automatic [255:0] make_market_quote;
        input [15:0] symbol_value;
        input [63:0] price_value;
        input [31:0] quantity_value;
        input [7:0] side_value;
        input [31:0] seq_value;
        reg [255:0] packet;
        begin
            packet = 256'd0;
            packet[255:248] = `PROTOCOL_SYNC_VERSION;
            packet[247:240] = `MSG_MARKET_QUOTE;
            packet[239:224] = symbol_value;
            packet[223:160] = 64'h0000000000000001;
            packet[159:96]  = price_value;
            packet[95:64]   = quantity_value;
            packet[63:56]   = side_value;
            packet[55:24]   = seq_value;
            packet[23:8]    = 16'h0000;
            packet[7:0]     = crc8_packet(packet);
            make_market_quote = packet;
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
        output first_sample;
        output last_sample;
        integer bit_index;
        begin
            receive = 256'd0;
            first_sample = 1'b0;
            last_sample = 1'b0;
            spi_cs_n = 1'b0;
            #40;
            for (bit_index = 0; bit_index < 256; bit_index = bit_index + 1) begin
                spi_mosi = transmit[255-bit_index];
                #80;
                spi_clk = 1'b1;
                #20;
                receive[255-bit_index] = spi_miso;
                if (bit_index == 0) first_sample = spi_miso;
                if (bit_index == 255) last_sample = spi_miso;
                #80;
                spi_clk = 1'b0;
                #20;
            end
            spi_mosi = 1'b0;
            spi_cs_n = 1'b1;
            #100;
        end
    endtask

    task automatic exchange_loopback;
        input [255:0] request;
        output [255:0] response;
        output first_response_bit;
        output last_response_bit;
        reg [255:0] ignored;
        reg ignored_first;
        reg ignored_last;
        reg [2:0] tx_read_before;
        begin
            tx_read_before = dut.tx_fifo_i.rd_bin;
            spi_transfer(request, ignored, ignored_first, ignored_last);
            if (dut.tx_fifo_i.rd_bin !== tx_read_before)
                $fatal(1, "TX FIFO dequeued during request transaction");
            spi_transfer(256'd0, ignored, ignored_first, ignored_last);
            if (dut.tx_fifo_i.rd_bin !== tx_read_before)
                $fatal(1, "TX FIFO dequeued during turnaround transaction");
            spi_transfer(256'd0, response, first_response_bit, last_response_bit);
            if (dut.tx_fifo_i.rd_bin !== (tx_read_before + 3'd1))
                $fatal(1, "TX FIFO did not dequeue exactly one response");
        end
    endtask

    task automatic pulse_internal_reset;
        begin
            spi_cs_n = 1'b1;
            spi_clk = 1'b0;
            spi_mosi = 1'b0;
            force dut.reset_n_int = 1'b0;
            #100;
            release dut.reset_n_int;
            #200;
        end
    endtask

    reg [255:0] request;
    reg [255:0] response;
    reg first_response_bit;
    reg last_response_bit;
    reg [255:0] empty_response;
    reg empty_first_bit;
    reg empty_last_bit;
    integer seq_index;

    initial begin
        wait (dut.reset_n_int === 1'b1);
        #500;

        // A response-phase transaction with an empty TX FIFO must return a
        // deterministic all-zero frame rather than X data or a stale packet.
        force dut.spi_slave_i.phase = 2'd2;
        spi_transfer(256'd0, empty_response, empty_first_bit, empty_last_bit);
        release dut.spi_slave_i.phase;
        force dut.spi_slave_i.phase = 2'd0;
        #1;
        release dut.spi_slave_i.phase;
        if (empty_response !== 256'd0) $fatal(1, "empty TX FIFO was not deterministic");

        // Reset while CS is high between frames. The next request must still
        // complete normally, proving persistent state has a single reset owner.
        pulse_internal_reset();

        request = make_request(32'd17, `MSG_LOOPBACK, `PROTOCOL_SYNC_VERSION);
        exchange_loopback(request, response, first_response_bit, last_response_bit);
        if (byte_of(response, 0) !== `PROTOCOL_SYNC_VERSION) $fatal(1, "valid response sync failed");
        if (byte_of(response, 1) !== `MSG_STATUS) $fatal(1, "valid response type failed");
        if (byte_of(response, 24) !== `STATUS_OK) $fatal(1, "valid loopback status failed");
        if (response[55:24] !== 32'd17) $fatal(1, "valid loopback sequence failed");
        if (byte_of(response, 31) !== crc8_packet(response)) $fatal(1, "valid response CRC failed");
        if (first_response_bit !== 1'b1) $fatal(1, "first response bit was not byte 0 MSB");
        if (last_response_bit !== response[0]) $fatal(1, "last response bit was not CRC LSB");

        // A valid market quote uses the same SPI/FIFO/CRC ingress path but is
        // consumed by the normalized event pipeline and produces no loopback
        // response packet.
        request = make_market_quote(16'd0, 64'd450000000, 32'd100, 8'd0, 32'd1);
        spi_transfer(request, empty_response, empty_first_bit, empty_last_bit);
        spi_transfer(256'd0, empty_response, empty_first_bit, empty_last_bit);
        spi_transfer(256'd0, response, empty_first_bit, empty_last_bit);
        if (dut.market_state_engine_i.best_bid_price[0] !== 64'd450000000 ||
            dut.market_state_engine_i.best_bid_quantity[0] !== 32'd100 ||
            dut.market_state_engine_i.bid_valid[0] !== 1'b1)
            $fatal(1, "market quote did not reach state engine through top");

        for (seq_index = 18; seq_index < 23; seq_index = seq_index + 1) begin
            request = make_request(seq_index, `MSG_LOOPBACK, `PROTOCOL_SYNC_VERSION);
            exchange_loopback(request, response, first_response_bit, last_response_bit);
            if (byte_of(response, 24) !== `STATUS_OK) $fatal(1, "sequential status failed");
            if (response[55:24] !== seq_index) $fatal(1, "sequential sequence failed");
        end

        request = make_request(32'd99, `MSG_LOOPBACK, `PROTOCOL_SYNC_VERSION);
        request[7:0] = request[7:0] ^ 8'h01;
        exchange_loopback(request, response, first_response_bit, last_response_bit);
        if (byte_of(response, 24) !== `STATUS_BAD_CHECKSUM) $fatal(1, "bad CRC status failed");
        if (response[55:24] !== 32'd99) $fatal(1, "bad CRC sequence failed");

        request = make_request(32'd100, 8'h55, `PROTOCOL_SYNC_VERSION);
        exchange_loopback(request, response, first_response_bit, last_response_bit);
        if (byte_of(response, 24) !== `STATUS_BAD_TYPE) $fatal(1, "bad type status failed");

        request = make_request(32'd101, `MSG_LOOPBACK, 8'h00);
        exchange_loopback(request, response, first_response_bit, last_response_bit);
        if (byte_of(response, 24) !== `STATUS_BAD_SYNC) $fatal(1, "bad sync status failed");

        // An incomplete CS frame is discarded by the per-frame reset. The
        // production datapath intentionally has no asynchronous diagnostic
        // state; the next complete request must still work.
        spi_cs_n = 1'b0;
        repeat (8) begin
            spi_mosi = 1'b0;
            #80; spi_clk = 1'b1; #20; spi_clk = 1'b0; #20;
        end
        spi_cs_n = 1'b1;
        #100;
        request = make_request(32'd102, `MSG_LOOPBACK, `PROTOCOL_SYNC_VERSION);
        exchange_loopback(request, response, first_response_bit, last_response_bit);
        if (byte_of(response, 24) !== `STATUS_OK) $fatal(1, "incomplete frame poisoned next request");
        if (response[55:24] !== 32'd102) $fatal(1, "post-incomplete sequence failed");

        if (dut.packet_error_count !== 3) $fatal(1, "packet error counter counted filler or missed malformed packet");
        if (led[0] !== 1'b1 && led[0] !== 1'b0) $fatal(1, "heartbeat LED unknown");
        $display("tb_trading_spi_top: PASS");
        $finish;
    end
endmodule
