`timescale 1ns/1ps

// Shared sequential normalization scheduler for one raw market feature
// record. All divisions use one unsigned_divider instance. Signed ratios use
// sign/magnitude handling around that unsigned datapath and saturate at the
// output format limits.
module feature_normalizer #(
    parameter integer VWAP_ACC_W = 101,
    parameter integer VWAP_QTY_ACC_W = 37,
    parameter integer BPS_SCALE = 10000,
    parameter integer BPS_OUTPUT_SCALE = 100,
    parameter integer IMBALANCE_FRAC_BITS = 15
) (
    input wire                         clk,
    input wire                         reset_n,
    input wire                         start,

    input wire [63:0]                  raw_spread,
    input wire                         raw_spread_valid,
    input wire [63:0]                  raw_midpoint,
    input wire                         raw_midpoint_valid,
    input wire signed [64:0]           raw_momentum,
    input wire                         raw_momentum_valid,
    input wire [63:0]                  raw_momentum_reference,
    input wire signed [32:0]           raw_imbalance_numerator,
    input wire [32:0]                  raw_imbalance_denominator,
    input wire                         raw_imbalance_valid,
    input wire [VWAP_ACC_W-1:0]        raw_vwap_sum_price_quantity,
    input wire [VWAP_QTY_ACC_W-1:0]    raw_vwap_sum_quantity,
    input wire                         raw_vwap_valid,

    output reg                         done,
    output reg [63:0]                  vwap,
    output reg                         vwap_valid,
    output reg signed [15:0]           imbalance_normalized,
    output reg                         imbalance_normalized_valid,
    output reg signed [31:0]           spread_bps_x100,
    output reg                         spread_bps_x100_valid,
    output reg signed [31:0]           momentum_bps_x100,
    output reg                         momentum_bps_x100_valid,
    output reg signed [64:0]           midpoint_minus_vwap,
    output reg                         midpoint_minus_vwap_valid,
    output reg signed [31:0]           midpoint_minus_vwap_bps_x100,
    output reg                         midpoint_minus_vwap_bps_x100_valid
);
    localparam integer BPS_NUM_SCALE = BPS_SCALE * BPS_OUTPUT_SCALE;
    localparam integer BPS_SCALE_W = (BPS_NUM_SCALE <= 1) ? 1 : $clog2(BPS_NUM_SCALE + 1);
    localparam integer IMBALANCE_NUM_W = 33 + IMBALANCE_FRAC_BITS;
    localparam integer DIV_NUM_W_A = (VWAP_ACC_W > IMBALANCE_NUM_W) ? VWAP_ACC_W : IMBALANCE_NUM_W;
    localparam integer DIV_NUM_W = (DIV_NUM_W_A > 65) ? DIV_NUM_W_A : 65;
    localparam integer DIV_DEN_W = 64;

    localparam [2:0] OP_VWAP = 3'd0;
    localparam [2:0] OP_IMBALANCE = 3'd1;
    localparam [2:0] OP_SPREAD = 3'd2;
    localparam [2:0] OP_MOMENTUM = 3'd3;
    localparam [2:0] OP_VWAP_DELTA = 3'd4;

    localparam [2:0] N_IDLE = 3'd0;
    localparam [2:0] N_START = 3'd1;
    localparam [2:0] N_SCALE = 3'd2;
    localparam [2:0] N_DIVIDE = 3'd3;
    localparam [2:0] N_WAIT = 3'd4;

    localparam [BPS_SCALE_W-1:0] BPS_NUM_SCALE_VALUE = BPS_NUM_SCALE;
    localparam [DIV_NUM_W-1:0] IMBALANCE_POS_MAX = 32767;
    localparam [DIV_NUM_W-1:0] IMBALANCE_NEG_MAG_MAX = 32768;
    localparam [DIV_NUM_W-1:0] SIGNED_BPS_POS_MAX = 64'd2147483647;
    localparam [DIV_NUM_W-1:0] SIGNED_BPS_NEG_MAG_MAX = 64'd2147483648;

    reg [2:0] normalizer_state = N_IDLE;
    reg [2:0] operation_index;
    reg [63:0] raw_spread_reg;
    reg raw_spread_valid_reg;
    reg [63:0] raw_midpoint_reg;
    reg raw_midpoint_valid_reg;
    reg signed [64:0] raw_momentum_reg;
    reg raw_momentum_valid_reg;
    reg [63:0] raw_momentum_reference_reg;
    reg signed [32:0] raw_imbalance_numerator_reg;
    reg [32:0] raw_imbalance_denominator_reg;
    reg raw_imbalance_valid_reg;
    reg [VWAP_ACC_W-1:0] raw_vwap_sum_price_quantity_reg;
    reg [VWAP_QTY_ACC_W-1:0] raw_vwap_sum_quantity_reg;
    reg raw_vwap_valid_reg;

    reg raw_vwap_result_valid_reg;
    reg [63:0] vwap_reg;
    reg operation_valid_reg;
    reg prepared_negative_reg;
    reg [64:0] scale_input_reg;
    reg [DIV_NUM_W-1:0] prepared_numerator_reg;
    reg [DIV_DEN_W-1:0] prepared_denominator_reg;

    wire raw_momentum_negative = raw_momentum_reg[64];
    wire [64:0] raw_momentum_magnitude = raw_momentum_negative ?
        (~raw_momentum_reg + 1'b1) : raw_momentum_reg;
    // The production scale is 10,000 * 100 = 1,000,000.  Spell that
    // constant as shifts/adds so the default Tang build stays in fabric
    // instead of consuming a wide DSP multiplier.  The multiply remains as
    // the generic fallback for other parameter values.
    function automatic [DIV_NUM_W-1:0] scale_bps_magnitude;
        input [64:0] magnitude;
        reg [DIV_NUM_W-1:0] magnitude_ext;
        begin
            magnitude_ext = {{(DIV_NUM_W-65){1'b0}}, magnitude};
            if (BPS_NUM_SCALE == 1000000) begin
                scale_bps_magnitude = (magnitude_ext << 6) +
                                      (magnitude_ext << 9) +
                                      (magnitude_ext << 14) +
                                      (magnitude_ext << 16) +
                                      (magnitude_ext << 17) +
                                      (magnitude_ext << 18) +
                                      (magnitude_ext << 19);
            end else begin
                scale_bps_magnitude = magnitude_ext * BPS_NUM_SCALE_VALUE;
            end
        end
    endfunction

    wire signed [64:0] midpoint_vwap_delta_wire =
        $signed({1'b0, raw_midpoint_reg}) - $signed({1'b0, vwap_reg});
    wire midpoint_vwap_delta_negative = midpoint_vwap_delta_wire[64];
    wire [64:0] midpoint_vwap_delta_magnitude = midpoint_vwap_delta_negative ?
        (~midpoint_vwap_delta_wire + 1'b1) : midpoint_vwap_delta_wire;

    wire imbalance_negative = raw_imbalance_numerator_reg[32];
    wire [32:0] imbalance_magnitude = imbalance_negative ?
        (~raw_imbalance_numerator_reg + 1'b1) : raw_imbalance_numerator_reg;
    wire [IMBALANCE_NUM_W-1:0] imbalance_numerator_small =
        {imbalance_magnitude, {IMBALANCE_FRAC_BITS{1'b0}}};

    reg operation_valid;
    reg divider_negative;
    reg [DIV_DEN_W-1:0] divider_denominator;

    always @* begin
        operation_valid = 1'b0;
        divider_negative = 1'b0;
        divider_denominator = {DIV_DEN_W{1'b0}};

        case (operation_index)
        OP_VWAP: begin
            operation_valid = raw_vwap_valid_reg &&
                              (raw_vwap_sum_quantity_reg != {VWAP_QTY_ACC_W{1'b0}});
            divider_denominator = {{(DIV_DEN_W-VWAP_QTY_ACC_W){1'b0}}, raw_vwap_sum_quantity_reg};
        end
        OP_IMBALANCE: begin
            operation_valid = raw_imbalance_valid_reg &&
                              (raw_imbalance_denominator_reg != 33'd0);
            divider_negative = imbalance_negative;
            divider_denominator = {{(DIV_DEN_W-33){1'b0}}, raw_imbalance_denominator_reg};
        end
        OP_SPREAD: begin
            operation_valid = raw_spread_valid_reg && raw_midpoint_valid_reg &&
                              (raw_midpoint_reg != 64'd0);
            divider_denominator = raw_midpoint_reg;
        end
        OP_MOMENTUM: begin
            operation_valid = raw_momentum_valid_reg &&
                              (raw_momentum_reference_reg != 64'd0);
            divider_negative = raw_momentum_negative;
            divider_denominator = raw_momentum_reference_reg;
        end
        OP_VWAP_DELTA: begin
            operation_valid = raw_midpoint_valid_reg && raw_vwap_result_valid_reg &&
                              (vwap_reg != 64'd0);
            divider_negative = midpoint_vwap_delta_negative;
            divider_denominator = vwap_reg;
        end
        default: begin
            operation_valid = 1'b0;
        end
        endcase
    end

    wire divider_start = (normalizer_state == N_DIVIDE) && operation_valid_reg;
    wire divider_busy;
    wire divider_done;
    wire divider_divide_by_zero;
    wire [DIV_NUM_W-1:0] divider_quotient;

    unsigned_divider #(
        .NUMERATOR_WIDTH(DIV_NUM_W),
        .DENOMINATOR_WIDTH(DIV_DEN_W),
        .QUOTIENT_WIDTH(DIV_NUM_W)
    ) shared_divider (
        .clk(clk),
        .reset_n(reset_n),
        .start(divider_start),
        .numerator(prepared_numerator_reg),
        .denominator(prepared_denominator_reg),
        .busy(divider_busy),
        .done(divider_done),
        .divide_by_zero(divider_divide_by_zero),
        .quotient(divider_quotient)
    );

    function automatic signed [15:0] saturate_imbalance;
        input [DIV_NUM_W-1:0] magnitude;
        input negative;
        begin
            if (negative) begin
                if (magnitude >= IMBALANCE_NEG_MAG_MAX)
                    saturate_imbalance = -16'sd32768;
                else
                    saturate_imbalance = -$signed(magnitude[15:0]);
            end else if (magnitude > IMBALANCE_POS_MAX) begin
                saturate_imbalance = 16'sd32767;
            end else begin
                saturate_imbalance = $signed(magnitude[15:0]);
            end
        end
    endfunction

    function automatic signed [31:0] saturate_bps;
        input [DIV_NUM_W-1:0] magnitude;
        input negative;
        begin
            if (negative) begin
                if (magnitude >= SIGNED_BPS_NEG_MAG_MAX)
                    saturate_bps = -32'sd2147483648;
                else
                    saturate_bps = -$signed(magnitude[31:0]);
            end else if (magnitude > SIGNED_BPS_POS_MAX) begin
                saturate_bps = 32'sd2147483647;
            end else begin
                saturate_bps = $signed(magnitude[31:0]);
            end
        end
    endfunction

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            normalizer_state <= N_IDLE;
            operation_index <= OP_VWAP;
            raw_spread_reg <= 64'd0;
            raw_spread_valid_reg <= 1'b0;
            raw_midpoint_reg <= 64'd0;
            raw_midpoint_valid_reg <= 1'b0;
            raw_momentum_reg <= 65'sd0;
            raw_momentum_valid_reg <= 1'b0;
            raw_momentum_reference_reg <= 64'd0;
            raw_imbalance_numerator_reg <= 33'sd0;
            raw_imbalance_denominator_reg <= 33'd0;
            raw_imbalance_valid_reg <= 1'b0;
            raw_vwap_sum_price_quantity_reg <= {VWAP_ACC_W{1'b0}};
            raw_vwap_sum_quantity_reg <= {VWAP_QTY_ACC_W{1'b0}};
            raw_vwap_valid_reg <= 1'b0;
            raw_vwap_result_valid_reg <= 1'b0;
            vwap_reg <= 64'd0;
            operation_valid_reg <= 1'b0;
            prepared_negative_reg <= 1'b0;
            scale_input_reg <= 65'd0;
            prepared_numerator_reg <= {DIV_NUM_W{1'b0}};
            prepared_denominator_reg <= {DIV_DEN_W{1'b0}};
            done <= 1'b0;
            vwap <= 64'd0;
            vwap_valid <= 1'b0;
            imbalance_normalized <= 16'sd0;
            imbalance_normalized_valid <= 1'b0;
            spread_bps_x100 <= 32'sd0;
            spread_bps_x100_valid <= 1'b0;
            momentum_bps_x100 <= 32'sd0;
            momentum_bps_x100_valid <= 1'b0;
            midpoint_minus_vwap <= 65'sd0;
            midpoint_minus_vwap_valid <= 1'b0;
            midpoint_minus_vwap_bps_x100 <= 32'sd0;
            midpoint_minus_vwap_bps_x100_valid <= 1'b0;
        end else begin
            done <= 1'b0;

            case (normalizer_state)
            N_IDLE: begin
                if (start) begin
                    raw_spread_reg <= raw_spread;
                    raw_spread_valid_reg <= raw_spread_valid;
                    raw_midpoint_reg <= raw_midpoint;
                    raw_midpoint_valid_reg <= raw_midpoint_valid;
                    raw_momentum_reg <= raw_momentum;
                    raw_momentum_valid_reg <= raw_momentum_valid;
                    raw_momentum_reference_reg <= raw_momentum_reference;
                    raw_imbalance_numerator_reg <= raw_imbalance_numerator;
                    raw_imbalance_denominator_reg <= raw_imbalance_denominator;
                    raw_imbalance_valid_reg <= raw_imbalance_valid;
                    raw_vwap_sum_price_quantity_reg <= raw_vwap_sum_price_quantity;
                    raw_vwap_sum_quantity_reg <= raw_vwap_sum_quantity;
                    raw_vwap_valid_reg <= raw_vwap_valid;
                    raw_vwap_result_valid_reg <= 1'b0;
                    vwap_reg <= 64'd0;
                    operation_valid_reg <= 1'b0;
                    vwap <= 64'd0;
                    vwap_valid <= 1'b0;
                    imbalance_normalized <= 16'sd0;
                    imbalance_normalized_valid <= 1'b0;
                    spread_bps_x100 <= 32'sd0;
                    spread_bps_x100_valid <= 1'b0;
                    momentum_bps_x100 <= 32'sd0;
                    momentum_bps_x100_valid <= 1'b0;
                    midpoint_minus_vwap <= 65'sd0;
                    midpoint_minus_vwap_valid <= 1'b0;
                    midpoint_minus_vwap_bps_x100 <= 32'sd0;
                    midpoint_minus_vwap_bps_x100_valid <= 1'b0;
                    operation_index <= OP_VWAP;
                    normalizer_state <= N_START;
                end
            end

            N_START: begin
                // The raw delta is defined even when VWAP is zero; its
                // normalized ratio is skipped because division by zero is not
                // meaningful.
                if ((operation_index == OP_VWAP_DELTA) && raw_midpoint_valid_reg &&
                    raw_vwap_result_valid_reg) begin
                    midpoint_minus_vwap <= midpoint_vwap_delta_wire;
                    midpoint_minus_vwap_valid <= 1'b1;
                end

                if (operation_valid) begin
                    prepared_denominator_reg <= divider_denominator;
                    prepared_negative_reg <= divider_negative;
                    operation_valid_reg <= 1'b1;
                    if (operation_index == OP_VWAP) begin
                        prepared_numerator_reg <=
                            {{(DIV_NUM_W-VWAP_ACC_W){1'b0}}, raw_vwap_sum_price_quantity_reg};
                        normalizer_state <= N_DIVIDE;
                    end else if (operation_index == OP_IMBALANCE) begin
                        prepared_numerator_reg <=
                            {{(DIV_NUM_W-IMBALANCE_NUM_W){1'b0}}, imbalance_numerator_small};
                        normalizer_state <= N_DIVIDE;
                    end else begin
                        case (operation_index)
                        OP_SPREAD: scale_input_reg <= {1'b0, raw_spread_reg};
                        OP_MOMENTUM: scale_input_reg <= raw_momentum_magnitude;
                        OP_VWAP_DELTA: scale_input_reg <= midpoint_vwap_delta_magnitude;
                        default: scale_input_reg <= 65'd0;
                        endcase
                        normalizer_state <= N_SCALE;
                    end
                end else if (operation_index == OP_VWAP_DELTA) begin
                    done <= 1'b1;
                    operation_valid_reg <= 1'b0;
                    normalizer_state <= N_IDLE;
                end else begin
                    operation_valid_reg <= 1'b0;
                    operation_index <= operation_index + 1'b1;
                end
            end

            N_SCALE: begin
                prepared_numerator_reg <= scale_bps_magnitude(scale_input_reg);
                operation_valid_reg <= 1'b1;
                normalizer_state <= N_DIVIDE;
            end

            N_DIVIDE: begin
                normalizer_state <= N_WAIT;
            end

            N_WAIT: begin
                if (divider_done) begin
                    case (operation_index)
                    OP_VWAP: begin
                        vwap_reg <= divider_quotient[63:0];
                        vwap <= divider_quotient[63:0];
                        vwap_valid <= 1'b1;
                        raw_vwap_result_valid_reg <= 1'b1;
                    end
                    OP_IMBALANCE: begin
                        imbalance_normalized <=
                            saturate_imbalance(divider_quotient, prepared_negative_reg);
                        imbalance_normalized_valid <= !divider_divide_by_zero;
                    end
                    OP_SPREAD: begin
                        spread_bps_x100 <= saturate_bps(divider_quotient, 1'b0);
                        spread_bps_x100_valid <= !divider_divide_by_zero;
                    end
                    OP_MOMENTUM: begin
                        momentum_bps_x100 <=
                            saturate_bps(divider_quotient, prepared_negative_reg);
                        momentum_bps_x100_valid <= !divider_divide_by_zero;
                    end
                    OP_VWAP_DELTA: begin
                        midpoint_minus_vwap_bps_x100 <=
                            saturate_bps(divider_quotient, prepared_negative_reg);
                        midpoint_minus_vwap_bps_x100_valid <= !divider_divide_by_zero;
                    end
                    default: begin end
                    endcase

                    if (operation_index == OP_VWAP_DELTA) begin
                        done <= 1'b1;
                        operation_valid_reg <= 1'b0;
                        normalizer_state <= N_IDLE;
                    end else begin
                        operation_valid_reg <= 1'b0;
                        operation_index <= operation_index + 1'b1;
                        normalizer_state <= N_START;
                    end
                end
            end

            default: normalizer_state <= N_IDLE;
            endcase
        end
    end

endmodule
