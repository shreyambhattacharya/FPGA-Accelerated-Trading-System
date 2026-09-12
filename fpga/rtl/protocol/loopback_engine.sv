`timescale 1ns/1ps

`include "protocol_defs.svh"

// Synchronous packet-processing stage in the 27 MHz system-clock domain.
// Request and response CRCs are streamed through crc8_engine one byte per
// cycle, avoiding a 31-byte combinational CRC chain.
module loopback_engine (
    input  wire                    clk,
    input  wire                    reset_n,
    input  wire                    packet_valid,
    output wire                    packet_ready,
    input  wire [`PACKET_BITS-1:0] packet_in,
    input  wire                    response_ready,
    output wire                    response_valid,
    output reg  [`PACKET_BITS-1:0] response_packet,
    output reg                     packet_error_pulse
);
    localparam [2:0] IDLE             = 3'd0;
    localparam [2:0] CRC_REQUEST      = 3'd1;
    localparam [2:0] VALIDATE_REQUEST = 3'd2;
    localparam [2:0] BUILD_RESPONSE   = 3'd3;
    localparam [2:0] CRC_RESPONSE     = 3'd4;
    localparam [2:0] WRITE_RESPONSE   = 3'd5;
    localparam [2:0] OUTPUT_RESPONSE  = 3'd6;

    reg [2:0] state = IDLE;
    reg [`PACKET_BITS-1:0] request_reg = 0;
    reg [`PACKET_BITS-1:0] response_reg = 0;
    reg [5:0] byte_index = 0;
    reg [7:0] request_crc_result = 0;
    reg [7:0] response_crc_result = 0;
    reg [7:0] status_reg = `STATUS_OK;
    reg [15:0] response_flags_reg = 16'h0001;
    reg response_valid_reg = 1'b0;

    wire packet_is_filler = (packet_in == {`PACKET_BITS{1'b0}});
    wire accepted = packet_valid && packet_ready;
    wire request_sync_ok = request_reg[255:248] == `PROTOCOL_SYNC_VERSION;
    wire request_crc_ok = request_reg[7:0] == request_crc_result;
    wire request_type_ok = request_reg[247:240] == `MSG_LOOPBACK;
    wire request_valid = request_sync_ok && request_crc_ok && request_type_ok;
    wire crc_data_valid = (state == CRC_REQUEST || state == CRC_RESPONSE) &&
                          !crc_done;
    wire crc_data_last = (byte_index == 6'd30);
    wire [7:0] crc_data_byte =
        (state == CRC_REQUEST) ? request_reg[255-byte_index*8 -: 8] :
                                 response_reg[255-byte_index*8 -: 8];
    wire crc_start = crc_data_valid && (byte_index == 0);
    wire crc_done;
    wire [7:0] crc_result;

    // A filler packet is consumed immediately. A non-filler request is
    // accepted only when the sequential processing FSM is idle.
    assign packet_ready = (state == IDLE);
    assign response_valid = response_valid_reg;

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

    function automatic [`PACKET_BITS-1:0] build_response_base(
        input [`PACKET_BITS-1:0] request,
        input [7:0] status,
        input [15:0] response_flags
    );
        reg [`PACKET_BITS-1:0] next_response;
        begin
            next_response = request;
            next_response[255:248] = `PROTOCOL_SYNC_VERSION;
            next_response[247:240] = `MSG_STATUS;
            next_response[63:56] = status;
            next_response[23:16] = response_flags[15:8];
            next_response[15:8] = response_flags[7:0];
            next_response[7:0] = 8'h00;
            build_response_base = next_response;
        end
    endfunction

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            state <= IDLE;
            request_reg <= 0;
            response_reg <= 0;
            response_packet <= 0;
            byte_index <= 0;
            request_crc_result <= 0;
            response_crc_result <= 0;
            status_reg <= `STATUS_OK;
            response_flags_reg <= 16'h0001;
            response_valid_reg <= 1'b0;
            packet_error_pulse <= 1'b0;
        end else begin
            packet_error_pulse <= 1'b0;

            if (response_valid_reg && response_ready) begin
                response_valid_reg <= 1'b0;
                state <= IDLE;
            end

            case (state)
            IDLE: begin
                if (accepted && !packet_is_filler) begin
                    request_reg <= packet_in;
                    byte_index <= 0;
                    state <= CRC_REQUEST;
                end
            end

            CRC_REQUEST: begin
                if (crc_done) begin
                    request_crc_result <= crc_result;
                    state <= VALIDATE_REQUEST;
                end else if (crc_data_valid && !crc_data_last) begin
                    byte_index <= byte_index + 1'b1;
                end
            end

            VALIDATE_REQUEST: begin
                if (!request_valid) begin
                    if (!request_sync_ok) begin
                        status_reg <= `STATUS_BAD_SYNC;
                        response_flags_reg <= 16'h0002;
                    end else if (!request_crc_ok) begin
                        status_reg <= `STATUS_BAD_CHECKSUM;
                        response_flags_reg <= 16'h0004;
                    end else begin
                        status_reg <= `STATUS_BAD_TYPE;
                        response_flags_reg <= 16'h0008;
                    end
                end else begin
                    status_reg <= `STATUS_OK;
                    response_flags_reg <= 16'h0001;
                end
                packet_error_pulse <= !request_valid;
                state <= BUILD_RESPONSE;
            end

            BUILD_RESPONSE: begin
                response_reg <= build_response_base(
                    request_reg, status_reg, response_flags_reg);
                byte_index <= 0;
                state <= CRC_RESPONSE;
            end

            CRC_RESPONSE: begin
                if (crc_done) begin
                    response_crc_result <= crc_result;
                    state <= WRITE_RESPONSE;
                end else if (crc_data_valid && !crc_data_last) begin
                    byte_index <= byte_index + 1'b1;
                end
            end

            WRITE_RESPONSE: begin
                response_reg[7:0] <= response_crc_result;
                response_packet <= {response_reg[255:8], response_crc_result};
                response_valid_reg <= 1'b1;
                state <= OUTPUT_RESPONSE;
            end

            OUTPUT_RESPONSE: begin
                // Hold the response and apply backpressure until the TX FIFO
                // accepts it. The response register is never recomputed here.
                if (response_valid_reg && response_ready) begin
                    response_valid_reg <= 1'b0;
                    state <= IDLE;
                end
            end

            default: state <= IDLE;
            endcase
        end
    end
endmodule
