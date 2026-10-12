// SPDX-License-Identifier: GPL-2.0-or-later
// YUV 4:2:0 -> RGB scaler + overlay layer for the v0.15 core. See docs/FPGA-YUV-v0.15.md; tools/yuv_ref.py is the
// bit-exact model.
//
// * Rows of the decoded picture are fetched from DDR one line ahead into small row buffers
//   (row r lives in buffer r mod 4; each plane buffer is split into an even-pixel and an odd-pixel bank).
// * A fully pipelined bilinear interpolator + colour matrix produces one pixel per `ce_pix` (every 3 clocks).
// * An optional 320x240 overlay (the GUI / on-screen display) is read from DDR a row at a time and drawn over the
//   picture with 2x pixel doubling; overlay pixels whose luma is 0 are transparent.
// * The DDR port is shared: this module asks for it with `want` and drives the bus only while `grant` is high.
module yuv_scaler #(
    parameter [28:0] BASE = 29'h06000000
)(
    input  wire        clk, reset,
    input  wire        ce_pix,
    input  wire [9:0]  px,                 // pixel x of the current line, valid at ce_pix
    input  wire [8:0]  ny,                 // coordinates of the NEXT line (wrap aware)
    input  wire        nfield, nil,
    // frame configuration, latched when a new picture is committed
    input  wire        cfg_load,
    input  wire [2:0]  cfg_slot,
    input  wire [2:0]  cfg_flags,
    input  wire [63:0] cfg_w1, cfg_w2, cfg_w3, cfg_w4, cfg_w5,
    output reg         have_frame = 0,
    // shared DDR master (single outstanding burst read)
    output reg         want = 0,
    input  wire        grant,
    input  wire        busy, valid,
    input  wire [63:0] dout,
    output reg  [28:0] addr = 0,
    output reg  [7:0]  burst = 0,
    output reg         rd = 0,
    // picture out: LAT clocks after the ce_pix of the pixel
    output reg  [23:0] rgb = 0
);
    // ------------------------------------------------------------------ configuration
    reg [9:0]  src_w = 0;  reg [8:0] src_h = 0;
    reg [9:0]  cw = 0;     reg [8:0] chh = 0;
    reg [6:0]  ystride8 = 0, cstride8 = 0, ybeats = 0, cbeats = 0;
    reg [9:0]  dx0 = 0, dw = 0;
    reg [8:0]  dy0 = 0, dh = 0;
    reg [31:0] hstep = 0, vstep = 0, sx0 = 0, sy0 = 0;
    reg [2:0]  flags = 0;
    reg [28:0] slot_beats = 0;
    reg        ov_en = 0;
    reg [28:0] ov_beats = 0;
    reg        invalidate = 0;

    always @(posedge clk) begin
        invalidate <= 0;
        if (cfg_load) begin
            src_w <= cfg_w1[9:0];        src_h <= cfg_w1[24:16];
            cw    <= {1'b0, cfg_w1[9:1]}; chh   <= {1'b0, cfg_w1[24:17]};
            ystride8 <= cfg_w1[41:35];   cstride8 <= cfg_w1[57:51];
            ybeats <= (cfg_w1[9:0] + 10'd7) >> 3;
            cbeats <= ({1'b0, cfg_w1[9:1]} + 10'd7) >> 3;
            dx0 <= cfg_w2[9:0];  dw <= cfg_w2[25:16];
            dy0 <= cfg_w2[40:32]; dh <= cfg_w2[56:48];
            hstep <= cfg_w3[31:0]; vstep <= cfg_w3[63:32];
            sx0 <= cfg_w4[31:0];   sy0 <= cfg_w4[63:32];
            flags <= cfg_flags;
            slot_beats <= BASE + 29'd512 + {10'd0, cfg_slot, 16'd0};
            ov_en <= cfg_w5[0];
            ov_beats <= BASE + 29'h80200 + {13'd0, cfg_w5[2:1], 14'd0};     // overlay buffers: 3 x 128 KiB after the slots
            have_frame <= 1;
            invalidate <= 1;
        end
        if (reset) have_frame <= 0;
    end

    // ------------------------------------------------------------------ row buffers
    // plane 0 = Y, 1 = U, 2 = V ; buffer 0..3 ; bank 0 = even pixels, 1 = odd pixels
    reg  [6:0]  wa_r = 0;
    reg  [31:0] wd_e, wd_o;
    reg  [63:0] wd_raw;
    reg  [3:0]  we_y = 0, we_u = 0, we_v = 0;
    reg  [1:0]  we_oy = 0, we_ou = 0, we_ov = 0;
    reg  [6:0]  ra_ye = 0, ra_yo = 0, ra_ce = 0, ra_co = 0;     // read addresses (pipeline stage 4)
    reg  [5:0]  ra_oy = 0, ra_oc = 0;
    wire [31:0] q_ye [0:3], q_yo [0:3], q_ue [0:3], q_uo [0:3], q_ve [0:3], q_vo [0:3];
    wire [63:0] q_oy [0:1], q_ou [0:1], q_ov [0:1];
    genvar gi;
    generate
        for (gi = 0; gi < 4; gi = gi + 1) begin : bufs
            linebuf ye (.clk(clk), .we(we_y[gi]), .wa(wa_r), .wd(wd_e), .ra(ra_ye), .rq(q_ye[gi]));
            linebuf yo (.clk(clk), .we(we_y[gi]), .wa(wa_r), .wd(wd_o), .ra(ra_yo), .rq(q_yo[gi]));
            linebuf ue (.clk(clk), .we(we_u[gi]), .wa(wa_r), .wd(wd_e), .ra(ra_ce), .rq(q_ue[gi]));
            linebuf uo (.clk(clk), .we(we_u[gi]), .wa(wa_r), .wd(wd_o), .ra(ra_co), .rq(q_uo[gi]));
            linebuf ve (.clk(clk), .we(we_v[gi]), .wa(wa_r), .wd(wd_e), .ra(ra_ce), .rq(q_ve[gi]));
            linebuf vo (.clk(clk), .we(we_v[gi]), .wa(wa_r), .wd(wd_o), .ra(ra_co), .rq(q_vo[gi]));
        end
        for (gi = 0; gi < 2; gi = gi + 1) begin : ovbufs
            linebuf64 oy (.clk(clk), .we(we_oy[gi]), .wa(wa_r[5:0]), .wd(wd_raw), .ra(ra_oy), .rq(q_oy[gi]));
            linebuf64 ou (.clk(clk), .we(we_ou[gi]), .wa(wa_r[5:0]), .wd(wd_raw), .ra(ra_oc), .rq(q_ou[gi]));
            linebuf64 ov (.clk(clk), .we(we_ov[gi]), .wa(wa_r[5:0]), .wd(wd_raw), .ra(ra_oc), .rq(q_ov[gi]));
        end
    endgenerate

    // ------------------------------------------------------------------ next-line job and prefetch
    localparam [3:0] S_IDLE=0, S_D=1, S_DY=2, S_PROD=3, S_SY=4, S_R0=5, S_R1=6, S_C1=7,
                     S_PICK=8, S_CHK=9, S_MUL=10, S_ADDR=11, S_WAIT=12, S_DONE=13, S_FIN=14;
    reg [3:0]  st = S_IDLE;
    reg [8:0]  d9 = 0, dyl = 0;
    reg        jv = 0, nact = 0;
    reg [31:0] prod = 0, sy = 0, csy = 0;
    reg [9:0]  r0 = 0, r1 = 0, c0 = 0, c1 = 0;
    reg [7:0]  fy = 0, fcy = 0;
    reg [2:0]  fi = 0;
    reg [9:0]  frow = 0;
    reg [1:0]  fkind = 0;            // 0 video Y row, 1 video chroma row, 2 overlay Y row, 3 overlay chroma row
    reg [2:0]  fplane = 0;           // 0 Y, 1 U, 2 V, 3 overlay Y, 4 overlay U, 5 overlay V
    reg [20:0] ofs = 0;
    reg [6:0]  beat = 0;
    reg [6:0]  nbeats = 0;
    reg [9:0]  yid [0:3];  reg [3:0] yv = 0;
    reg [9:0]  cid [0:3];  reg [3:0] cv = 0;
    reg [7:0]  oyid [0:1]; reg [1:0] oyv = 0;
    reg [6:0]  ocid [0:1]; reg [1:0] ocv = 0;
    // job promoted at the start of a line
    reg        nxt_ready = 0, nxt_valid = 0, nxt_act = 0;
    reg [1:0]  nxt_y0 = 0, nxt_y1 = 0, nxt_c0 = 0, nxt_c1 = 0;
    reg        nxt_ovy = 0, nxt_ovc = 0;
    reg [7:0]  nxt_fy = 0, nxt_fcy = 0;
    reg [8:0]  nxt_d = 0;
    reg        cur_valid = 0, cur_act = 0;
    reg [1:0]  cur_y0 = 0, cur_y1 = 0, cur_c0 = 0, cur_c1 = 0;
    reg        cur_ovy = 0, cur_ovc = 0;
    reg [7:0]  cur_fy = 0, cur_fcy = 0;
    reg [8:0]  cur_d = 0;

    reg rd_sent = 0;                                 // a command has been accepted for this burst
    wire line_start = ce_pix && px == 10'd0;
    always @(posedge clk) begin
        if (line_start) begin
            cur_valid <= nxt_ready && nxt_valid && have_frame;
            cur_act <= nxt_ready && nxt_act && have_frame;
            cur_y0 <= nxt_y0; cur_y1 <= nxt_y1; cur_c0 <= nxt_c0; cur_c1 <= nxt_c1;
            cur_ovy <= nxt_ovy; cur_ovc <= nxt_ovc;
            cur_fy <= nxt_fy; cur_fcy <= nxt_fcy; cur_d <= nxt_d;
        end
        if (reset) begin cur_valid <= 0; cur_act <= 0; end
    end

    wire [8:0] dnext = nil ? {ny[7:0], nfield} : {ny[7:0], 1'b0};
    wire need_y  = !(yv[frow[1:0]] && yid[frow[1:0]] == frow);
    wire need_c  = !(cv[frow[1:0]] && cid[frow[1:0]] == frow);
    wire need_oy = !(oyv[frow[0]] && oyid[frow[0]] == frow[7:0]);
    wire need_oc = !(ocv[frow[0]] && ocid[frow[0]] == frow[6:0]);
    wire need_row = (fkind == 2'd0) ? need_y : (fkind == 2'd1) ? need_c : (fkind == 2'd2) ? need_oy : need_oc;
    wire skip_row = (fkind < 2'd2) ? !jv : !ov_en;
    wire [9:0] y_last = {1'b0, src_h} - 10'd1;
    wire [9:0] c_last = {1'b0, chh} - 10'd1;
    wire [31:0] csy_full = {1'b0, sy[31:1]};
    wire [6:0] row_stride8 = (fplane == 3'd0) ? ystride8 : (fplane == 3'd3) ? 7'd40 : (fplane >= 3'd4) ? 7'd20 : cstride8;

    always @(posedge clk) begin
        if (invalidate) begin yv <= 0; cv <= 0; oyv <= 0; ocv <= 0; end
        case (st)
            S_IDLE: begin
                // a new line starts: its successor's job is computed and fetched while it is displayed
                if (line_start) begin nxt_ready <= 0; st <= S_D; end
            end
            S_D: begin
                d9 <= dnext;
                nact <= have_frame && ny < 9'd240;
                jv <= have_frame && ny < 9'd240 && dnext >= dy0 && dnext < ({1'b0, dy0} + {1'b0, dh});
                st <= S_DY;
            end
            S_DY: begin
                dyl <= d9 - dy0;
                st <= S_PROD;
            end
            S_PROD: begin
                prod <= dyl * vstep;
                st <= S_SY;
            end
            S_SY: begin
                sy <= sy0 + prod;
                st <= S_R0;
            end
            S_R0: begin
                r0 <= (sy[31:16] >= {7'd0, src_h}) ? y_last : sy[25:16];
                csy <= (csy_full < 32'h4000) ? 32'd0 : csy_full - 32'h4000;
                fy <= sy[15:8];
                st <= S_R1;
            end
            S_R1: begin
                r1 <= ({1'b0, r0} + 11'd1 >= {2'd0, src_h}) ? y_last : r0 + 10'd1;
                c0 <= (csy[31:16] >= {7'd0, chh}) ? c_last : csy[25:16];
                fcy <= csy[15:8];
                st <= S_C1;
            end
            S_C1: begin
                c1 <= ({1'b0, c0} + 11'd1 >= {2'd0, chh}) ? c_last : c0 + 10'd1;
                fi <= 0;
                if (!nact) begin nxt_valid <= 0; nxt_act <= 0; st <= S_FIN; end
                else st <= S_PICK;
            end
            S_PICK: begin                           // next row to make sure of: video Y r0, r1, chroma c0, c1; overlay Y, chroma
                case (fi)
                    0: begin frow <= r0; fkind <= 2'd0; end
                    1: begin frow <= r1; fkind <= 2'd0; end
                    2: begin frow <= c0; fkind <= 2'd1; end
                    3: begin frow <= c1; fkind <= 2'd1; end
                    4: begin frow <= {2'b00, d9[8:1]}; fkind <= 2'd2; end
                    default: begin frow <= {3'b000, d9[8:2]}; fkind <= 2'd3; end
                endcase
                st <= S_CHK;
            end
            S_CHK: begin
                if (fi == 3'd6) st <= S_DONE;
                else if (skip_row || !need_row) begin fi <= fi + 3'd1; st <= S_PICK; end
                else begin
                    fplane <= (fkind == 2'd0) ? 3'd0 : (fkind == 2'd1) ? 3'd1 : (fkind == 2'd2) ? 3'd3 : 3'd4;
                    st <= S_MUL;
                end
            end
            S_MUL: begin
                ofs <= frow * row_stride8;
                st <= S_ADDR;
            end
            S_ADDR: begin
                // beat address of the row: video Y at +0x100, U at +0x4C000, V at +0x60000 bytes of the slot;
                // overlay Y at +0, U at +0x13000, V at +0x18000 bytes of the overlay buffer
                case (fplane)
                    3'd0: addr <= slot_beats + 29'h20 + {8'd0, ofs};
                    3'd1: addr <= slot_beats + 29'h9800 + {8'd0, ofs};
                    3'd2: addr <= slot_beats + 29'hC000 + {8'd0, ofs};
                    3'd3: addr <= ov_beats + {8'd0, ofs};
                    3'd4: addr <= ov_beats + 29'h2600 + {8'd0, ofs};
                    default: addr <= ov_beats + 29'h3000 + {8'd0, ofs};
                endcase
                nbeats <= (fplane == 3'd0) ? ybeats : (fplane == 3'd3) ? 7'd40 : (fplane >= 3'd4) ? 7'd20 : cbeats;
                burst <= {1'b0, (fplane == 3'd0) ? ybeats : (fplane == 3'd3) ? 7'd40 : (fplane >= 3'd4) ? 7'd20 : cbeats};
                beat <= 0;
                want <= 1;
                st <= S_WAIT;
            end
            S_WAIT: begin
                // drive the bus once granted; the command is held until accepted, then the beats arrive
                if (grant && !rd && beat == 0 && !rd_sent) rd <= 1;
                if (rd && !busy) begin rd <= 0; end
                if (valid && grant) begin
                    beat <= beat + 7'd1;
                    if (beat + 7'd1 == nbeats) begin
                        case (fplane)
                            3'd1: begin fplane <= 3'd2; st <= S_MUL; end             // U done: V of the same row next
                            3'd4: begin fplane <= 3'd5; st <= S_MUL; end
                            default: begin
                                if (fplane == 3'd0) begin yv[frow[1:0]] <= 1; yid[frow[1:0]] <= frow; end
                                if (fplane == 3'd2) begin cv[frow[1:0]] <= 1; cid[frow[1:0]] <= frow; end
                                if (fplane == 3'd3) begin oyv[frow[0]] <= 1; oyid[frow[0]] <= frow[7:0]; end
                                if (fplane == 3'd5) begin ocv[frow[0]] <= 1; ocid[frow[0]] <= frow[6:0]; end
                                want <= 0;
                                fi <= fi + 3'd1;
                                st <= S_PICK;
                            end
                        endcase
                    end
                end
            end
            S_DONE: begin
                nxt_y0 <= r0[1:0]; nxt_y1 <= r1[1:0]; nxt_c0 <= c0[1:0]; nxt_c1 <= c1[1:0];
                nxt_ovy <= d9[1]; nxt_ovc <= d9[2];
                nxt_fy <= fy; nxt_fcy <= fcy; nxt_d <= d9; nxt_valid <= jv; nxt_act <= 1;
                st <= S_FIN;
            end
            S_FIN: begin
                nxt_ready <= 1;
                st <= S_IDLE;
            end
            default: st <= S_IDLE;
        endcase
        if (reset) begin st <= S_IDLE; want <= 0; rd <= 0; yv <= 0; cv <= 0; oyv <= 0; ocv <= 0;
                         nxt_ready <= 0; nxt_valid <= 0; nxt_act <= 0; end
    end
    always @(posedge clk) begin
        if (st == S_ADDR) rd_sent <= 0;
        else if (rd && !busy) rd_sent <= 1;
    end

    // Beats from DDR go into the row buffer of the plane/row being fetched.
    always @(posedge clk) begin
        we_y <= 0; we_u <= 0; we_v <= 0; we_oy <= 0; we_ou <= 0; we_ov <= 0;
        if (st == S_WAIT && valid && grant) begin
            wa_r <= beat;
            wd_raw <= dout;
            wd_e <= {dout[55:48], dout[39:32], dout[23:16], dout[7:0]};
            wd_o <= {dout[63:56], dout[47:40], dout[31:24], dout[15:8]};
            case (fplane)
                3'd0: we_y[frow[1:0]] <= 1;
                3'd1: we_u[frow[1:0]] <= 1;
                3'd2: we_v[frow[1:0]] <= 1;
                3'd3: we_oy[frow[0]] <= 1;
                3'd4: we_ou[frow[0]] <= 1;
                default: we_ov[frow[0]] <= 1;
            endcase
        end
    end

    // ------------------------------------------------------------------ pixel pipeline
    localparam LAT = 15;                             // clocks from ce_pix of a pixel to its rgb (checked by the testbench)
    reg ce_d1 = 0;  reg [9:0] x_q = 0;
    always @(posedge clk) begin ce_d1 <= ce_pix; if (ce_pix) x_q <= px; end

    // stage 0: position inside the destination rectangle (video) and inside the active line (overlay)
    reg        s0_v = 0, s0_act = 0; reg [9:0] s0_k = 0; reg [9:0] s0_x = 0;
    always @(posedge clk) begin
        s0_v <= ce_d1 && cur_valid && x_q >= dx0 && x_q < dx0 + dw;
        s0_act <= ce_d1 && cur_act && x_q < 10'd640;
        s0_k <= x_q - dx0;
        s0_x <= x_q;
    end
    // stage 1: source coordinate
    reg [31:0] s1_p = 0; reg s1_v = 0, s1_act = 0; reg [9:0] s1_x = 0;
    always @(posedge clk) begin s1_p <= s0_k * hstep; s1_v <= s0_v; s1_act <= s0_act; s1_x <= s0_x; end
    reg [31:0] s2_sx = 0; reg s2_v = 0, s2_act = 0; reg [9:0] s2_x = 0;
    always @(posedge clk) begin s2_sx <= sx0 + s1_p; s2_v <= s1_v; s2_act <= s1_act; s2_x <= s1_x; end
    // stage 3: indices and fractions (both luma and chroma)
    wire [31:0] s2_cs = {1'b0, s2_sx[31:1]};
    wire [9:0] lx0f = s2_sx[25:16];
    wire [9:0] cx0f = s2_cs[25:16];
    reg [9:0] s3_lx0 = 0, s3_cx0 = 0;  reg s3_lclamp = 0, s3_cclamp = 0;
    reg [7:0] s3_fx = 0, s3_fcx = 0;  reg s3_v = 0, s3_act = 0; reg [9:0] s3_x = 0;
    always @(posedge clk) begin
        s3_lx0 <= (s2_sx[31:16] >= {6'd0, src_w}) ? src_w - 10'd1 : lx0f;
        s3_lclamp <= ((s2_sx[31:16] >= {6'd0, src_w}) ? src_w - 10'd1 : lx0f) + 10'd1 >= src_w;
        s3_cx0 <= (s2_cs[31:16] >= {6'd0, cw}) ? cw - 10'd1 : cx0f;
        s3_cclamp <= ((s2_cs[31:16] >= {6'd0, cw}) ? cw - 10'd1 : cx0f) + 10'd1 >= cw;
        s3_fx <= s2_sx[15:8]; s3_fcx <= s2_cs[15:8];
        s3_v <= s2_v; s3_act <= s2_act; s3_x <= s2_x;
    end
    // stage 4: read addresses (registered into the RAMs at the end of this stage) and per-pixel side data
    function [6:0] wordE(input [9:0] p); wordE = p[0] ? (p + 10'd1) >> 3 : p >> 3; endfunction
    reg [1:0] s4_lane_ye, s4_lane_yo, s4_lane_ce, s4_lane_co;
    reg [2:0] s4_oly = 0, s4_olc = 0; reg s4_ovy = 0, s4_ovc = 0;
    reg s4_lodd = 0, s4_codd = 0, s4_lclamp = 0, s4_cclamp = 0, s4_v = 0, s4_act = 0;
    reg [7:0] s4_fx = 0, s4_fcx = 0, s4_fy = 0, s4_fcy = 0;
    reg [1:0] s4_by0 = 0, s4_by1 = 0, s4_bc0 = 0, s4_bc1 = 0;
    reg [9:0] s4_x = 0;
    reg [8:0] s4_d = 0;
    wire [9:0] lx1 = s3_lx0 + 10'd1;
    wire [9:0] cx1 = s3_cx0 + 10'd1;
    always @(posedge clk) begin
        ra_ye <= wordE(s3_lx0);
        ra_yo <= s3_lx0 >> 3;
        ra_ce <= wordE(s3_cx0);
        ra_co <= s3_cx0 >> 3;
        ra_oy <= s3_x[9:4];                         // overlay luma pixel x/2, eight per beat
        ra_oc <= {1'b0, s3_x[9:5]};                 // overlay chroma pixel x/4
        s4_oly <= s3_x[3:1]; s4_olc <= s3_x[4:2]; s4_ovy <= cur_ovy; s4_ovc <= cur_ovc;
        s4_lane_ye <= s3_lx0[0] ? lx1[2:1] : s3_lx0[2:1];
        s4_lane_yo <= s3_lx0[2:1];
        s4_lane_ce <= s3_cx0[0] ? cx1[2:1] : s3_cx0[2:1];
        s4_lane_co <= s3_cx0[2:1];
        s4_lodd <= s3_lx0[0]; s4_codd <= s3_cx0[0];
        s4_lclamp <= s3_lclamp; s4_cclamp <= s3_cclamp;
        s4_fx <= s3_fx; s4_fcx <= s3_fcx; s4_fy <= cur_fy; s4_fcy <= cur_fcy;
        s4_by0 <= cur_y0; s4_by1 <= cur_y1; s4_bc0 <= cur_c0; s4_bc1 <= cur_c1;
        s4_v <= s3_v; s4_act <= s3_act; s4_x <= s3_x; s4_d <= cur_d;
    end
    // stage 4b: the RAM output arrives one clock after its address, so the side data waits one clock
    reg [1:0] b_lane_ye, b_lane_yo, b_lane_ce, b_lane_co;
    reg [2:0] b_oly = 0, b_olc = 0; reg b_ovy = 0, b_ovc = 0;
    reg b_lodd = 0, b_codd = 0, b_lclamp = 0, b_cclamp = 0, b_v = 0, b_act = 0;
    reg [7:0] b_fx = 0, b_fcx = 0, b_fy = 0, b_fcy = 0;
    reg [1:0] b_by0 = 0, b_by1 = 0, b_bc0 = 0, b_bc1 = 0;
    reg [9:0] b_x = 0; reg [8:0] b_d = 0;
    always @(posedge clk) begin
        b_lane_ye <= s4_lane_ye; b_lane_yo <= s4_lane_yo; b_lane_ce <= s4_lane_ce; b_lane_co <= s4_lane_co;
        b_oly <= s4_oly; b_olc <= s4_olc; b_ovy <= s4_ovy; b_ovc <= s4_ovc;
        b_lodd <= s4_lodd; b_codd <= s4_codd; b_lclamp <= s4_lclamp; b_cclamp <= s4_cclamp; b_v <= s4_v; b_act <= s4_act;
        b_fx <= s4_fx; b_fcx <= s4_fcx; b_fy <= s4_fy; b_fcy <= s4_fcy;
        b_by0 <= s4_by0; b_by1 <= s4_by1; b_bc0 <= s4_bc0; b_bc1 <= s4_bc1; b_x <= s4_x; b_d <= s4_d;
    end
    // stage 5: RAM data is valid; choose rows, banks and lanes
    function [7:0] lane8(input [31:0] w, input [1:0] l); lane8 = w >> (8 * l); endfunction
    function [7:0] lane64(input [63:0] w, input [2:0] l); lane64 = w >> (8 * l); endfunction
    reg [7:0] s5_y0a = 0, s5_y0b = 0, s5_y1a = 0, s5_y1b = 0;
    reg [7:0] s5_u0a = 0, s5_u0b = 0, s5_u1a = 0, s5_u1b = 0;
    reg [7:0] s5_v0a = 0, s5_v0b = 0, s5_v1a = 0, s5_v1b = 0;
    reg [7:0] s5_oy = 0, s5_ou = 0, s5_ov = 0;
    reg [7:0] s5_fx = 0, s5_fcx = 0, s5_fy = 0, s5_fcy = 0;
    reg s5_v = 0, s5_act = 0; reg [9:0] s5_x = 0; reg [8:0] s5_d = 0;
    // pixel A = x0, pixel B = x1 (= A when clamped); x0 odd -> A comes from the odd bank
    function [7:0] pickA(input odd, input [31:0] e, input [31:0] o, input [1:0] le, input [1:0] lo);
        pickA = odd ? lane8(o, lo) : lane8(e, le);
    endfunction
    function [7:0] pickB(input odd, input clamp, input [31:0] e, input [31:0] o, input [1:0] le, input [1:0] lo);
        pickB = clamp ? pickA(odd, e, o, le, lo) : (odd ? lane8(e, le) : lane8(o, lo));
    endfunction
    always @(posedge clk) begin
        s5_y0a <= pickA(b_lodd, q_ye[b_by0], q_yo[b_by0], b_lane_ye, b_lane_yo);
        s5_y0b <= pickB(b_lodd, b_lclamp, q_ye[b_by0], q_yo[b_by0], b_lane_ye, b_lane_yo);
        s5_y1a <= pickA(b_lodd, q_ye[b_by1], q_yo[b_by1], b_lane_ye, b_lane_yo);
        s5_y1b <= pickB(b_lodd, b_lclamp, q_ye[b_by1], q_yo[b_by1], b_lane_ye, b_lane_yo);
        s5_u0a <= pickA(b_codd, q_ue[b_bc0], q_uo[b_bc0], b_lane_ce, b_lane_co);
        s5_u0b <= pickB(b_codd, b_cclamp, q_ue[b_bc0], q_uo[b_bc0], b_lane_ce, b_lane_co);
        s5_u1a <= pickA(b_codd, q_ue[b_bc1], q_uo[b_bc1], b_lane_ce, b_lane_co);
        s5_u1b <= pickB(b_codd, b_cclamp, q_ue[b_bc1], q_uo[b_bc1], b_lane_ce, b_lane_co);
        s5_v0a <= pickA(b_codd, q_ve[b_bc0], q_vo[b_bc0], b_lane_ce, b_lane_co);
        s5_v0b <= pickB(b_codd, b_cclamp, q_ve[b_bc0], q_vo[b_bc0], b_lane_ce, b_lane_co);
        s5_v1a <= pickA(b_codd, q_ve[b_bc1], q_vo[b_bc1], b_lane_ce, b_lane_co);
        s5_v1b <= pickB(b_codd, b_cclamp, q_ve[b_bc1], q_vo[b_bc1], b_lane_ce, b_lane_co);
        s5_oy <= lane64(q_oy[b_ovy], b_oly);
        s5_ou <= lane64(q_ou[b_ovc], b_olc);
        s5_ov <= lane64(q_ov[b_ovc], b_olc);
        s5_fx <= b_fx; s5_fcx <= b_fcx; s5_fy <= b_fy; s5_fcy <= b_fcy;
        s5_v <= b_v; s5_act <= b_act; s5_x <= b_x; s5_d <= b_d;
    end
    // stage 6: horizontal interpolation (6 lerps)
    function [7:0] lerp8(input [7:0] a, input [7:0] b, input [7:0] f);
        reg signed [9:0] dd; reg signed [18:0] m, sh;
        begin
            dd = $signed({2'b00, b}) - $signed({2'b00, a});
            m = dd * $signed({1'b0, f}) + 19'sd128;
            sh = m >>> 8;                              // arithmetic shift (floor), exactly as the Python model
            lerp8 = a + sh[7:0];                       // modulo 256; the true result is always 0..255
        end
    endfunction
    reg [7:0] s6_ya = 0, s6_yb = 0, s6_ua = 0, s6_ub = 0, s6_va = 0, s6_vb = 0, s6_fy = 0, s6_fcy = 0;
    reg [7:0] s6_oy = 0, s6_ou = 0, s6_ov = 0;
    reg s6_v = 0, s6_act = 0; reg [9:0] s6_x = 0; reg [8:0] s6_d = 0;
    always @(posedge clk) begin
        s6_ya <= lerp8(s5_y0a, s5_y0b, s5_fx);   s6_yb <= lerp8(s5_y1a, s5_y1b, s5_fx);
        s6_ua <= lerp8(s5_u0a, s5_u0b, s5_fcx);  s6_ub <= lerp8(s5_u1a, s5_u1b, s5_fcx);
        s6_va <= lerp8(s5_v0a, s5_v0b, s5_fcx);  s6_vb <= lerp8(s5_v1a, s5_v1b, s5_fcx);
        s6_oy <= s5_oy; s6_ou <= s5_ou; s6_ov <= s5_ov;
        s6_fy <= s5_fy; s6_fcy <= s5_fcy; s6_v <= s5_v; s6_act <= s5_act; s6_x <= s5_x; s6_d <= s5_d;
    end
    // stage 7: vertical interpolation
    reg [7:0] s7_y = 0, s7_u = 0, s7_v = 0, s7_oy = 0, s7_ou = 0, s7_ov = 0;
    reg s7_valid = 0, s7_act = 0; reg [9:0] s7_x = 0; reg [8:0] s7_d = 0;
    always @(posedge clk) begin
        s7_y <= lerp8(s6_ya, s6_yb, s6_fy);
        s7_u <= lerp8(s6_ua, s6_ub, s6_fcy);
        s7_v <= lerp8(s6_va, s6_vb, s6_fcy);
        s7_oy <= s6_oy; s7_ou <= s6_ou; s7_ov <= s6_ov;
        s7_valid <= s6_v; s7_act <= s6_act; s7_x <= s6_x; s7_d <= s6_d;
    end
    // stage 8: overlay over picture, then the colour matrix inputs
    wire bt601 = flags[0], full = flags[1], dither = flags[2];
    wire signed [9:0] k_gain = full ? 10'sd256 : 10'sd298;
    wire signed [9:0] k_rv = bt601 ? (full ? 10'sd359 : 10'sd409) : (full ? 10'sd403 : 10'sd459);
    wire signed [9:0] k_gu = bt601 ? (full ? 10'sd88 : 10'sd100) : (full ? 10'sd48 : 10'sd55);
    wire signed [9:0] k_gv = bt601 ? (full ? 10'sd183 : 10'sd208) : (full ? 10'sd120 : 10'sd136);
    wire signed [10:0] k_bu = bt601 ? (full ? 11'sd454 : 11'sd516) : (full ? 11'sd475 : 11'sd541);
    wire ov_hit = ov_en && s7_act && (s7_oy != 8'd0);
    reg signed [10:0] s8_c = 0, s8_d = 0, s8_e = 0;
    reg s8_valid = 0; reg [9:0] s8_x = 0; reg [8:0] s8_dd = 0;
    always @(posedge clk) begin
        s8_c <= $signed({3'b000, ov_hit ? s7_oy : s7_y}) - (full ? 11'sd0 : 11'sd16);
        s8_d <= $signed({3'b000, ov_hit ? s7_ou : s7_u}) - 11'sd128;
        s8_e <= $signed({3'b000, ov_hit ? s7_ov : s7_v}) - 11'sd128;
        s8_valid <= s7_valid | ov_hit; s8_x <= s7_x; s8_dd <= s7_d;
    end
    reg signed [21:0] s9_gc = 0, s9_rv = 0, s9_gu = 0, s9_gv = 0, s9_bu = 0;
    reg s9_valid = 0; reg [9:0] s9_x = 0; reg [8:0] s9_dd = 0;
    always @(posedge clk) begin
        s9_gc <= k_gain * s8_c;
        s9_rv <= k_rv * s8_e;
        s9_gu <= k_gu * s8_d;
        s9_gv <= k_gv * s8_e;
        s9_bu <= k_bu * s8_d;
        s9_valid <= s8_valid; s9_x <= s8_x; s9_dd <= s8_dd;
    end
    // stage 10: sums
    reg signed [21:0] s10_r = 0, s10_g = 0, s10_b = 0;
    reg s10_valid = 0; reg [9:0] s10_x = 0; reg [8:0] s10_dd = 0;
    always @(posedge clk) begin
        s10_r <= s9_gc + s9_rv + 22'sd128;
        s10_g <= s9_gc - s9_gu - s9_gv + 22'sd128;
        s10_b <= s9_gc + s9_bu + 22'sd128;
        s10_valid <= s9_valid; s10_x <= s9_x; s10_dd <= s9_dd;
    end
    // stage 11: shift, saturate
    function [7:0] sat8(input signed [21:0] v);
        reg signed [21:0] t;
        begin t = v >>> 8; sat8 = (t < 0) ? 8'd0 : (t > 255) ? 8'd255 : t[7:0]; end
    endfunction
    reg [7:0] s11_r = 0, s11_g = 0, s11_b = 0;
    reg s11_valid = 0; reg [9:0] s11_x = 0; reg [8:0] s11_dd = 0;
    always @(posedge clk) begin
        s11_r <= sat8(s10_r); s11_g <= sat8(s10_g); s11_b <= sat8(s10_b);
        s11_valid <= s10_valid; s11_x <= s10_x; s11_dd <= s10_dd;
    end
    // stage 12: optional ordered dither to 6 bits, black outside the rectangle
    function [1:0] bayer(input [1:0] yy, input [1:0] xx);
        case ({yy[0], xx[0]})
            2'b00: bayer = 2'd0;
            2'b01: bayer = 2'd2;
            2'b10: bayer = 2'd3;
            default: bayer = 2'd1;
        endcase
    endfunction
    wire [1:0] t = bayer(s11_dd[1:0], s11_x[1:0]);
    function [7:0] dith(input [7:0] c, input [1:0] tt);
        reg [8:0] s;
        begin s = {1'b0, c} + {7'd0, tt}; dith = (s > 9'd255 ? 8'd255 : s[7:0]) & 8'hFC; end
    endfunction
    always @(posedge clk) begin
        if (!s11_valid) rgb <= 24'd0;
        else if (dither) rgb <= {dith(s11_r, t), dith(s11_g, t), dith(s11_b, t)};
        else rgb <= {s11_r, s11_g, s11_b};
    end
endmodule
