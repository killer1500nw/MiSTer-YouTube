// SPDX-License-Identifier: GPL-2.0-or-later
// Native CRT frame-transfer stage of the MiSTer YouTube project.
module emu (
    `include "sys/emu_ports.vh"
);
assign ADC_BUS = 'Z;
assign USER_OUT = '1;
assign {UART_RTS, UART_TXD, UART_DTR} = 0;
assign {SD_SCK, SD_MOSI, SD_CS} = 'Z;
assign {SDRAM_DQ, SDRAM_A, SDRAM_BA, SDRAM_CLK, SDRAM_CKE,
        SDRAM_DQML, SDRAM_DQMH, SDRAM_nWE, SDRAM_nCAS, SDRAM_nRAS, SDRAM_nCS} = 'Z;
assign DDRAM_CLK=clk_sys;
assign VGA_SL = 0;
assign VGA_F1 = 0;         // Progressive, not 480i.
assign VGA_SCALER = 0;     // Never force the analogue scaler.
assign VGA_DISABLE = 0;
assign HDMI_FREEZE = 0;
assign HDMI_BLACKOUT = 0;
assign HDMI_BOB_DEINT = 0;
assign VIDEO_ARX = 13'd4;
assign VIDEO_ARY = 13'd3;
assign LED_DISK = 0;
assign LED_POWER = 0;
assign BUTTONS = 0;
assign AUDIO_MIX = 0;
assign AUDIO_S = 1;

`include "build_id.v"
localparam CONF_STR = {
    "YouTubeCRT;;",
    "-;",
    "-,Native CRT 320x240 URL stream;",
    "D1F1,RAW,Load frame;",
    "O[4],Display,Loaded frame,Test pattern;",
    "O[2:1],Pattern,Colour bars,Grid,Greyscale,Checkerboard;",
    "O[3],Test tone,Off,On;",
    "O[5],RAM streaming,Off,On;",
    "T[0],Reset;",
    "v,6;",
    "V,v0.13-smooth-",`BUILD_DATE
};
wire [127:0] status;
wire [1:0] buttons;
wire ioctl_download, ioctl_wr, ioctl_wait;
wire [26:0] ioctl_addr;
wire [7:0] ioctl_dout;
wire [15:0] ioctl_index;
wire [31:0] sd_lba [2];
wire [5:0] sd_blocks [2];
wire [7:0] sd_input [2];
assign sd_input[0]=8'd0;
assign sd_input[1]=8'd0;
assign sd_lba[1]=32'd0;
assign sd_blocks[1]=6'd0;
wire sd_rd, sd_buff_wr;
wire [1:0] sd_ack, img_mounted;
wire [13:0] sd_buff_addr;
wire [7:0] sd_buff_dout;
wire [63:0] img_size;
wire movie_mode, movie_error, movie_done, movie_clear;
wire movie_dl, movie_wr;
wire [26:0] movie_addr;
wire [7:0] movie_data;
wire store_wait;
wire movie_audio_wr, movie_audio_ready;
wire [13:0] movie_audio_addr;
wire [15:0] movie_left, movie_right;
wire clk_sys;
wire pll_locked;
hps_io #(.CONF_STR(CONF_STR), .VDNUM(2)) hps_io (
    .clk_sys(clk_sys), .HPS_BUS(HPS_BUS),
    .EXT_BUS(), .gamma_bus(), .buttons(buttons),
    .status(status), .status_menumask({14'd0,movie_mode,1'b0}),
    .ioctl_download(ioctl_download), .ioctl_wr(ioctl_wr),
    .ioctl_addr(ioctl_addr), .ioctl_dout(ioctl_dout),
    .ioctl_index(ioctl_index), .ioctl_wait(ioctl_wait),
    .img_mounted(img_mounted), .img_size(img_size),
    .sd_lba(sd_lba), .sd_blk_cnt(sd_blocks), .sd_rd({1'b0,sd_rd}), .sd_wr(2'b00),
    .sd_ack(sd_ack), .sd_buff_addr(sd_buff_addr), .sd_buff_dout(sd_buff_dout),
    .sd_buff_din(sd_input), .sd_buff_wr(sd_buff_wr)
);
// Retain the template PLL instance name to match its timing constraints.
pll pll (.refclk(CLK_50M), .rst(1'b0), .outclk_0(clk_sys), .locked(pll_locked));
wire reset = RESET | status[0] | buttons[1] | ~pll_locked;
assign CLK_VIDEO = clk_sys;
wire raw_ce, raw_hs, raw_vs, raw_de;
wire [8:0] px, py;
wire [7:0] test_r, test_g, test_b;
crt240 video (
    .clk(clk_sys), .reset(reset), .pattern(status[2:1]),
    .ce_pix(raw_ce), .hs(raw_hs), .vs(raw_vs), .de(raw_de),
    .r(test_r), .g(test_g), .b(test_b), .pixel_x(px), .pixel_y(py)
);
wire [16:0] full_frame_address = raw_de ?
    ({8'd0,py} << 8) + ({8'd0,py} << 6) + {8'd0,px} : 17'd0;
// Full 320x240 source pixels for still frames and streamed video.
wire [16:0] frame_address=full_frame_address;
wire [15:0] frame_pixel;
wire frame_valid, frame_error;
wire swap_boundary = raw_ce && (px == 0) && (py == 240);
// RAM streaming owns the frame input while enabled. Native scanout is unchanged.
assign sd_lba[0]=0;
assign sd_blocks[0]=0;
assign sd_rd=0;
assign movie_mode=status[5];
wire stream_clear;
reg old_stream_mode=0;
always @(posedge clk_sys) old_stream_mode<=movie_mode;
assign movie_clear=stream_clear || (old_stream_mode != movie_mode);
ddr_stream stream (
    .clk(clk_sys), .reset(reset), .enable(movie_mode),
    .busy(DDRAM_BUSY), .valid(DDRAM_DOUT_READY), .dout(DDRAM_DOUT),
    .addr(DDRAM_ADDR), .burst(DDRAM_BURSTCNT), .be(DDRAM_BE),
    .rd(DDRAM_RD), .wr(DDRAM_WE), .din(DDRAM_DIN),
    .frame_download(movie_dl), .frame_wr(movie_wr),
    .frame_addr(movie_addr), .frame_data(movie_data),
    .audio_wr(movie_audio_wr), .audio_addr(movie_audio_addr),
    .audio_ready(movie_audio_ready), .commit(movie_commit),
    .clear_frame(stream_clear), .error(movie_error), .done(movie_done)
);
reg movie_tick=0;
always @(posedge clk_sys) begin
    if(reset || movie_clear || !movie_mode) movie_tick<=0;
    else if(swap_boundary) movie_tick<=~movie_tick;
end
// Commit a complete picture and its PCM segment at the same blanking slot.
// Late records hold the previous image and insert silence, not A/V drift.
wire frame_slot=swap_boundary && (!movie_mode ||
    (movie_tick==0 && movie_audio_ready && !movie_error));
wire movie_commit=movie_mode && store_wait && frame_slot;
movie_audio sound (
    .clk(clk_sys), .reset(reset | movie_clear), .commit(movie_commit),
    .wr(movie_audio_wr), .addr(movie_audio_addr), .data(movie_data),
    .left(movie_left), .right(movie_right)
);
assign ioctl_wait=store_wait && !movie_mode;
frame_store frames (
    .clk(clk_sys), .reset(reset | movie_clear),
    .download(movie_mode ? movie_dl : ioctl_download),
    .file_index(movie_mode ? 16'd1 : ioctl_index),
    .wr(movie_mode ? movie_wr : ioctl_wr),
    .addr(movie_mode ? movie_addr : ioctl_addr), .data(movie_mode ? movie_data : ioctl_dout),
    .wait_req(store_wait), .swap_boundary(frame_slot),
    .read_addr(frame_address), .pixel(frame_pixel),
    .valid(frame_valid), .load_error(frame_error)
);
// Frame RAM has one clock read latency. Delay every video signal equally.
reg ce_d=0, hs_d=0, vs_d=0, de_d=0;
reg [23:0] test_d=0;
reg select_frame=0;
always @(posedge clk_sys) begin
    ce_d <= raw_ce; hs_d <= raw_hs; vs_d <= raw_vs; de_d <= raw_de;
    test_d <= {test_r,test_g,test_b};
    if(swap_boundary) select_frame <= !status[4];
end
assign CE_PIXEL = ce_d;
assign VGA_HS = hs_d;
assign VGA_VS = vs_d;
assign VGA_DE = de_d;
wire [23:0] rgb888 = {frame_pixel[15:11],frame_pixel[15:13],
                       frame_pixel[10:5],frame_pixel[10:9],
                       frame_pixel[4:0],frame_pixel[4:2]};
// A magenta corner flags a rejected frame; old good frame stays visible.
reg error_corner=0;
always @(posedge clk_sys) error_corner <= (frame_error || movie_error) && px < 8 && py < 8;
assign {VGA_R,VGA_G,VGA_B} = !de_d ? 24'd0 : error_corner ? 24'hff00ff :
    (frame_valid && select_frame) ? rgb888 : test_d;
// Optional quiet 1 kHz signed square-wave, disabled by default.
reg [13:0] tone_count = 0;
reg tone = 0;
always @(posedge clk_sys) begin
    if (reset || !status[3]) begin
        tone_count <= 0;
        tone <= 0;
    end else if (tone_count == 9999) begin
        tone_count <= 0;
        tone <= ~tone;
    end else tone_count <= tone_count + 1'b1;
end
assign AUDIO_L = reset ? 16'd0 : movie_mode ? movie_left :
                 status[3] ? (tone ? 16'h0800 : 16'hf800) : 16'd0;
assign AUDIO_R = reset ? 16'd0 : movie_mode ? movie_right :
                 status[3] ? (tone ? 16'h0800 : 16'hf800) : 16'd0;
reg [24:0] heartbeat = 0;
always @(posedge clk_sys) heartbeat <= heartbeat + 1'b1;
assign LED_USER = movie_mode ? (movie_error ? heartbeat[22] : movie_done) : heartbeat[24];
endmodule
