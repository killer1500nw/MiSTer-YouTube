// SPDX-License-Identifier: GPL-2.0-or-later
// 20 MHz clock / 3 = 6.666667 MHz pixel enable.
// 424 pixels/line, 262 lines/frame: 15.72327 kHz / 60.01248 Hz.
// Sync runs continuously, including during user reset.
module crt240 (
    input wire clk,
    input wire reset,
    input wire [1:0] pattern,
    output wire ce_pix,
    output wire hs, vs, de,
    output reg [7:0] r, g, b,
    output wire [8:0] pixel_x, pixel_y
);
    reg [1:0] divider = 0;
    reg [8:0] x = 0;
    reg [8:0] y = 0;
    reg [8:0] marker = 0;
    reg frame_phase = 0;
    reg [1:0] mode = 0;

    assign pixel_x = x;
    assign pixel_y = y;
    assign ce_pix = (divider == 2);
    assign hs = (x >= 336 && x < 368);
    assign vs = (y >= 244 && y < 247);
    assign de = (x < 320 && y < 240);

    always @(posedge clk) begin
        divider <= ce_pix ? 2'd0 : divider + 1'b1;
        if (ce_pix) begin
            if (x == 423) begin
                x <= 0;
                if (y == 261) begin
                    y <= 0;
                    mode <= pattern; // change pattern only at a frame boundary
                    frame_phase <= ~frame_phase;
                    if (frame_phase)
                        marker <= (marker == 303) ? 9'd0 : marker + 1'b1;
                end else y <= y + 1'b1;
            end else x <= x + 1'b1;
        end
        if (reset) begin
            marker <= 0;
            frame_phase <= 0;
        end
    end

    reg [23:0] colour;
    wire [7:0] grey = (x < 32) ? 8'd0 : (x >= 288) ? 8'd255 : (x - 9'd32);
    always @* begin
        colour = 24'h000000;
        if (de) begin
            case (mode)
                2'd0: begin // eight bars, 40 pixels each
                    if      (x < 40)  colour = 24'hbfbfbf;
                    else if (x < 80)  colour = 24'hbfbf00;
                    else if (x < 120) colour = 24'h00bfbf;
                    else if (x < 160) colour = 24'h00bf00;
                    else if (x < 200) colour = 24'hbf00bf;
                    else if (x < 240) colour = 24'hbf0000;
                    else if (x < 280) colour = 24'h0000bf;
                    else              colour = 24'h000000;
                    // Moving white square on black strip.
                    if (y >= 208 && y < 232) begin
                        colour = 0;
                        if (x >= marker && x < marker + 9'd16)
                            colour = 24'hffffff;
                    end
                end
                2'd1: begin // 16-pixel grid and central cross
                    colour = 24'h101010;
                    if (x[3:0] == 0 || y[3:0] == 0) colour = 24'h808080;
                    if (x == 160 || y == 120) colour = 24'hffffff;
                end
                2'd2: colour = {grey, grey, grey};
                2'd3: colour = (x[3] ^ y[3]) ? 24'hffffff : 24'h000000;
            endcase
            // White outer border and inset green rectangle show overscan.
            if (x == 0 || x == 319 || y == 0 || y == 239)
                colour = 24'hffffff;
            else if (((x == 8 || x == 311) && y >= 8 && y <= 231) ||
                     ((y == 8 || y == 231) && x >= 8 && x <= 311))
                colour = 24'h00ff00;
        end
        {r,g,b} = colour;
    end
endmodule
