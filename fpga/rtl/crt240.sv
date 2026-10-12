// SPDX-License-Identifier: GPL-2.0-or-later
// 40 MHz clock / 3 = 13.333333 MHz pixel enable.
// 848 pixels/line (640 active), 262 lines/frame: 15.72327 kHz / 60.01248 Hz.
// With `interlace` set the same line rate gives 480i: 525 lines per frame
// (field 0 = 262 lines, field 1 = 263), 59.94 Hz fields. Field 1's vertical
// sync starts half a line (424 pixels) into line 244, which is what makes the
// two fields interleave. Each field shows 240 active lines (the scaler picks which
// lines of the 480-line frame).
// next_* describe the line that follows the current one, for the scaler's prefetch.
// The mode is latched at the end of a frame, so it never changes mid-picture.
// Sync runs continuously, including during user reset.
module crt240 (
    input wire clk,
    input wire reset,
    input wire [1:0] pattern,
    input wire interlace,
    output wire il_active,   // 1 while the running frame is 480i
    output wire field,       // 480i field number (always 0 when progressive)
    output wire ce_pix,
    output wire hs, vs, de,
    output reg [7:0] r, g, b,
    output wire [9:0] pixel_x,
    output wire [8:0] pixel_y,
    output wire [8:0] next_y,
    output wire next_field, next_il
);
    reg [1:0] divider = 0;
    reg [9:0] x = 0;
    reg [8:0] y = 0;
    reg [9:0] marker = 0;
    reg frame_phase = 0;
    reg il = 0, fld = 0;
    wire [8:0] last_line = (il && fld) ? 9'd262 : 9'd261;
    assign il_active = il;
    assign field = fld;
    reg [1:0] mode = 0;

    assign pixel_x = x;
    assign pixel_y = y;
    wire at_last = (y == last_line);
    assign next_y = at_last ? 9'd0 : y + 1'b1;
    assign next_field = at_last ? ((interlace && il) ? ~fld : 1'b0) : fld;
    assign next_il = at_last ? interlace : il;
    assign ce_pix = (divider == 2);
    assign hs = (x >= 672 && x < 736);
    assign vs = (!il || !fld) ? (y >= 244 && y < 247) :
                ((y == 244 && x >= 424) || y == 245 || y == 246 || (y == 247 && x < 424));
    assign de = (x < 640 && y < 240);

    always @(posedge clk) begin
        divider <= ce_pix ? 2'd0 : divider + 1'b1;
        if (ce_pix) begin
            if (x == 847) begin
                x <= 0;
                if (y == last_line) begin
                    y <= 0;
                    il <= interlace;
                    fld <= (interlace && il) ? ~fld : 1'b0;
                    mode <= pattern; // change pattern only at a frame boundary
                    frame_phase <= ~frame_phase;
                    if (frame_phase)
                        marker <= (marker >= 606) ? 10'd0 : marker + 10'd2;
                end else y <= y + 1'b1;
            end else x <= x + 1'b1;
        end
        if (reset) begin
            marker <= 0;
            frame_phase <= 0;
        end
    end

    reg [23:0] colour;
    wire [9:0] gx = x - 10'd64;
    wire [7:0] grey = (x < 64) ? 8'd0 : (x >= 576) ? 8'd255 : gx[8:1];
    always @* begin
        colour = 24'h000000;
        if (de) begin
            case (mode)
                2'd0: begin // eight bars, 80 pixels each
                    if      (x < 80)  colour = 24'hbfbfbf;
                    else if (x < 160) colour = 24'hbfbf00;
                    else if (x < 240) colour = 24'h00bfbf;
                    else if (x < 320) colour = 24'h00bf00;
                    else if (x < 400) colour = 24'hbf00bf;
                    else if (x < 480) colour = 24'hbf0000;
                    else if (x < 560) colour = 24'h0000bf;
                    else              colour = 24'h000000;
                    // Moving white square on black strip.
                    if (y >= 208 && y < 232) begin
                        colour = 0;
                        if (x >= marker && x < marker + 10'd32)
                            colour = 24'hffffff;
                    end
                end
                2'd1: begin // 16-pixel grid and central cross
                    colour = 24'h101010;
                    if (x[4:0] == 0 || y[3:0] == 0) colour = 24'h808080;
                    if (x == 320 || y == 120) colour = 24'hffffff;
                end
                2'd2: colour = {grey, grey, grey};
                2'd3: colour = (x[4] ^ y[3]) ? 24'hffffff : 24'h000000;
            endcase
            // White outer border and inset green rectangle show overscan.
            if (x == 0 || x == 639 || y == 0 || y == 239)
                colour = 24'hffffff;
            else if (((x == 16 || x == 623) && y >= 8 && y <= 231) ||
                     ((y == 8 || y == 231) && x >= 16 && x <= 623))
                colour = 24'h00ff00;
        end
        {r,g,b} = colour;
    end
endmodule
