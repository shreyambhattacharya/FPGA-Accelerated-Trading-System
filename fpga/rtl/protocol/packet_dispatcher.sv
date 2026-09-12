`timescale 1ns/1ps

`include "protocol_defs.svh"

// System-clock packet classifier and normalized-event producer.
//
// The dispatcher is deliberately independent of SPI timing. It consumes one
// complete packet from the system-domain RX FIFO, validates the packet CRC,
// and then routes LOOPBACK packets to loopback_engine, QUOTE/TRADE packets to
// the market event interface, and CONTROL packets to the runtime strategy
// configuration interface. Unsupported packets continue to the loopback
// status path so the existing diagnostic response remains intact.
module packet_dispatcher #(
    parameter integer NUM_SYMBOLS = 4
) (
    input  wire                    clk,
    input  wire                    reset_n,
    input  wire                    packet_valid,
    output wire                    packet_ready,
    input  wire [`PACKET_BITS-1:0] packet_in,

    output wire                    loopback_valid,
    input  wire                    loopback_ready,
    output wire [`PACKET_BITS-1:0] loopback_packet,
    output wire                    loopback_status_override_valid,
    output wire [7:0]              loopback_status_override,

    output wire                    event_valid,
    input  wire                    event_ready,
    output wire [7:0]              event_type,
    output wire [15:0]             event_symbol_id,
    output wire [63:0]             event_timestamp_ns,
    output wire [63:0]             event_price,
    output wire [31:0]             event_quantity,
    output wire [7:0]              event_side,
    output wire [31:0]             event_sequence,
    output wire [15:0]             event_flags,

    output wire                    control_valid,
    input  wire                    control_ready,
    output wire [7:0]              control_subcommand,
    output wire [15:0]             control_symbol_id,
    output wire [63:0]             control_data64,
    output wire [31:0]             control_data32,
    output wire [15:0]             control_flags,
    input  wire [7:0]              control_status,

    output reg                     dispatch_error_pulse,
    output reg [7:0]               dispatch_error_reason,
    output reg [15:0]              dispatch_error_symbol_id,
    output reg [31:0]              dispatch_error_sequence
);
    localparam [2:0] IDLE          = 3'd0;
    localparam [2:0] CRC_PACKET    = 3'd1;
    localparam [2:0] CLASSIFY      = 3'd2;
    localparam [2:0] ROUTE_EVENT   = 3'd3;
    localparam [2:0] ROUTE_LOOPBACK = 3'd4;
    localparam [2:0] ROUTE_CONTROL  = 3'd5;

    reg [2:0] state = IDLE;
    reg [`PACKET_BITS-1:0] packet_reg = 0;
    reg [5:0] byte_index = 0;
    reg [7:0] crc_result_reg = 0;
    reg loopback_valid_reg = 1'b0;
    reg loopback_status_override_valid_reg = 1'b0;
    reg [7:0] loopback_status_override_reg = 8'h00;
    reg event_valid_reg = 1'b0;
    reg [7:0] event_type_reg;
    reg [15:0] event_symbol_id_reg;
    reg [63:0] event_timestamp_ns_reg;
    reg [63:0] event_price_reg;
    reg [31:0] event_quantity_reg;
    reg [7:0] event_side_reg;
    reg [31:0] event_sequence_reg;
    reg [15:0] event_flags_reg;
    reg control_valid_reg = 1'b0;
    reg [7:0] control_subcommand_reg;
    reg [15:0] control_symbol_id_reg;
    reg [63:0] control_data64_reg;
    reg [31:0] control_data32_reg;
    reg [15:0] control_flags_reg;

    wire packet_is_filler = (packet_in == {`PACKET_BITS{1'b0}});
    wire accepted = packet_valid && packet_ready;
    wire crc_data_valid = (state == CRC_PACKET) && !crc_done;
    wire crc_data_last = (byte_index == 6'd30);
    wire [7:0] crc_data_byte = packet_reg[255-byte_index*8 -: 8];
    wire crc_start = crc_data_valid && (byte_index == 0);
    wire crc_done;
    wire [7:0] crc_result;
    wire packet_sync_ok = packet_reg[255:248] == `PROTOCOL_SYNC_VERSION;
    wire packet_crc_ok = packet_reg[7:0] == crc_result_reg;
    wire packet_symbol_valid = packet_reg[239:224] < NUM_SYMBOLS;

    assign packet_ready = (state == IDLE);
    assign loopback_valid = loopback_valid_reg;
    assign loopback_packet = packet_reg;
    assign loopback_status_override_valid = loopback_status_override_valid_reg;
    assign loopback_status_override = loopback_status_override_reg;
    assign event_valid = event_valid_reg;
    assign event_type = event_type_reg;
    assign event_symbol_id = event_symbol_id_reg;
    assign event_timestamp_ns = event_timestamp_ns_reg;
    assign event_price = event_price_reg;
    assign event_quantity = event_quantity_reg;
    assign event_side = event_side_reg;
    assign event_sequence = event_sequence_reg;
    assign event_flags = event_flags_reg;
    assign control_valid = control_valid_reg;
    assign control_subcommand = control_subcommand_reg;
    assign control_symbol_id = control_symbol_id_reg;
    assign control_data64 = control_data64_reg;
    assign control_data32 = control_data32_reg;
    assign control_flags = control_flags_reg;

    crc8_engine crc8_engine_i (
        .clk(clk),
        .reset_n(reset_n),
        .start(crc_start),
        .data_valid(crc_data_valid),
        .data_last(crc_data_last),
        .data_byte(crc_data_byte),
        .done(crc_done),
        .result(crc_result)
    );

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            state <= IDLE;
            packet_reg <= 0;
            byte_index <= 0;
            crc_result_reg <= 0;
            loopback_valid_reg <= 1'b0;
            loopback_status_override_valid_reg <= 1'b0;
            loopback_status_override_reg <= 8'h00;
            event_valid_reg <= 1'b0;
            event_type_reg <= 8'h00;
            event_symbol_id_reg <= 16'h0000;
            event_timestamp_ns_reg <= 64'h0000000000000000;
            event_price_reg <= 64'h0000000000000000;
            event_quantity_reg <= 32'h00000000;
            event_side_reg <= 8'h00;
            event_sequence_reg <= 32'h00000000;
            event_flags_reg <= 16'h0000;
            control_valid_reg <= 1'b0;
            control_subcommand_reg <= 8'h00;
            control_symbol_id_reg <= 16'h0000;
            control_data64_reg <= 64'h0000000000000000;
            control_data32_reg <= 32'h00000000;
            control_flags_reg <= 16'h0000;
            dispatch_error_pulse <= 1'b0;
            dispatch_error_reason <= 8'h00;
            dispatch_error_symbol_id <= 16'h0000;
            dispatch_error_sequence <= 32'h00000000;
        end else begin
            dispatch_error_pulse <= 1'b0;

            case (state)
            IDLE: begin
                if (accepted) begin
                    if (packet_is_filler) begin
                        // All-zero frames are transport clocking filler.
                        state <= IDLE;
                    end else begin
                        packet_reg <= packet_in;
                        byte_index <= 0;
                        state <= CRC_PACKET;
                    end
                end
            end

            CRC_PACKET: begin
                if (crc_done) begin
                    crc_result_reg <= crc_result;
                    state <= CLASSIFY;
                end else if (crc_data_valid && !crc_data_last) begin
                    byte_index <= byte_index + 1'b1;
                end
            end

            CLASSIFY: begin
                loopback_status_override_valid_reg <= 1'b0;
                loopback_status_override_reg <= 8'h00;

                if (!packet_sync_ok || !packet_crc_ok) begin
                    // Let loopback_engine preserve the existing bad-sync or
                    // bad-CRC status semantics for malformed packets.
                    loopback_valid_reg <= 1'b1;
                    state <= ROUTE_LOOPBACK;
                end else if (packet_reg[247:240] == `MSG_LOOPBACK) begin
                    loopback_valid_reg <= 1'b1;
                    state <= ROUTE_LOOPBACK;
                end else if ((packet_reg[247:240] == `MSG_MARKET_QUOTE) ||
                             (packet_reg[247:240] == `MSG_MARKET_TRADE)) begin
                    if (!packet_symbol_valid) begin
                        // Reuse the existing status response path while also
                        // exposing an explicit dispatcher error reason.
                        loopback_status_override_valid_reg <= 1'b1;
                        loopback_status_override_reg <= `STATUS_BAD_SYMBOL;
                        loopback_valid_reg <= 1'b1;
                        dispatch_error_pulse <= 1'b1;
                        dispatch_error_reason <= `STATUS_BAD_SYMBOL;
                        dispatch_error_symbol_id <= packet_reg[239:224];
                        dispatch_error_sequence <= packet_reg[55:24];
                        state <= ROUTE_LOOPBACK;
                    end else begin
                        event_type_reg <= packet_reg[247:240];
                        event_symbol_id_reg <= packet_reg[239:224];
                        event_timestamp_ns_reg <= packet_reg[223:160];
                        event_price_reg <= packet_reg[159:96];
                        event_quantity_reg <= packet_reg[95:64];
                        event_side_reg <= packet_reg[63:56];
                        event_sequence_reg <= packet_reg[55:24];
                        event_flags_reg <= packet_reg[23:8];
                        event_valid_reg <= 1'b1;
                        state <= ROUTE_EVENT;
                    end
                end else if (packet_reg[247:240] == `MSG_CONTROL) begin
                    control_subcommand_reg <= packet_reg[63:56];
                    control_symbol_id_reg <= packet_reg[239:224];
                    control_data64_reg <= packet_reg[159:96];
                    control_data32_reg <= packet_reg[95:64];
                    control_flags_reg <= packet_reg[23:8];
                    control_valid_reg <= 1'b1;
                    state <= ROUTE_CONTROL;
                end else begin
                    // Unsupported types receive STATUS_BAD_TYPE from the
                    // existing loopback engine.
                    loopback_valid_reg <= 1'b1;
                    state <= ROUTE_LOOPBACK;
                end
            end

            ROUTE_EVENT: begin
                if (event_valid_reg && event_ready) begin
                    event_valid_reg <= 1'b0;
                    state <= IDLE;
                end
            end

            ROUTE_CONTROL: begin
                if (control_valid_reg && control_ready) begin
                    control_valid_reg <= 1'b0;
                    // A control write is acknowledged as a status response
                    // through the existing loopback/CRC response path.
                    loopback_status_override_valid_reg <= 1'b1;
                    loopback_status_override_reg <= control_status;
                    loopback_valid_reg <= 1'b1;
                    state <= ROUTE_LOOPBACK;
                end
            end

            ROUTE_LOOPBACK: begin
                if (loopback_valid_reg && loopback_ready) begin
                    loopback_valid_reg <= 1'b0;
                    loopback_status_override_valid_reg <= 1'b0;
                    state <= IDLE;
                end
            end

            default: state <= IDLE;
            endcase
        end
    end
endmodule
