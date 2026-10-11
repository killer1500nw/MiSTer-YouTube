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
    "-;",
    "O[6],Output,240p,480i;",
    "O[7],480i field order,Normal,Swapped;",
    "-;",
    "T[0],Reset;",
    "J1,A,B,X,Y,L,R,Select,Start;",
    "jn,A,B,X,Y,L,R,Select,Start;",
    "v,6;",
    "V,v0.14-auto-",`BUILD_DATE
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
// Controller and keyboard arrive from the MiSTer main program through hps_io, not
// through Linux. They are published into the DDR mailbox for the player to read.
wire [31:0] joy0, joy1;
wire [10:0] ps2_key;
reg [10:0] key_old=0;
reg [47:0] key_ev=0;      // last three keyboard events, newest in [15:0]
reg [15:0] key_seq=0;     // increments per event so the player never misses one
always @(posedge clk_sys) if(ps2_key[10]!=key_old[10]) begin
    key_old<=ps2_key; key_ev<={key_ev[31:0],5'd0,ps2_key}; key_seq<=key_seq+1'b1;
end
hps_io #(.CONF_STR(CONF_STR), .VDNUM(2)) hps_io (
    .clk_sys(clk_sys), .HPS_BUS(HPS_BUS),
    .EXT_BUS(), .gamma_bus(), .buttons(buttons),
    .joystick_0(joy0), .joystick_1(joy1), .ps2_key(ps2_key),
    .status(status), .status_menumask(16'd0),
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
wire raw_ce, raw_hs, raw_vs, raw_de, raw_il, raw_field;
wire [8:0] px, py;
wire [7:0] test_r, test_g, test_b;
crt240 video (
    .clk(clk_sys), .reset(reset), .pattern(2'b00), .interlace(status[6]), .il_active(raw_il), .field(raw_field),
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
// Streaming is always on: the player starts its own session. (A wire, not a menu option, so a saved
// setting from an older core can never switch it off; simulation forces it to test start/stop.)
wire stream_on = 1'b1;
assign movie_mode = stream_on;
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
    .clear_frame(stream_clear), .error(movie_error), .done(movie_done),
    .in0({joy1,joy0}), .in1({key_seq,key_ev})
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
    .seg_clocks(raw_il ? 21'd667800 : 21'd666528),
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
reg ce_d=0, hs_d=0, vs_d=0, de_d=0, f1_d=0;
reg [23:0] test_d=0;
reg select_frame=0;
always @(posedge clk_sys) begin
    ce_d <= raw_ce; hs_d <= raw_hs; vs_d <= raw_vs; de_d <= raw_de;
    f1_d <= raw_il & (raw_field ^ status[7]);
    test_d <= {test_r,test_g,test_b};
    if(swap_boundary) select_frame <= 1'b1;     // the player's picture is always the one shown
end
assign CE_PIXEL = ce_d;
assign VGA_HS = hs_d;
assign VGA_VS = vs_d;
assign VGA_DE = de_d;
assign VGA_F1 = f1_d;      // interlaced field flag for the HDMI scaler (0 in 240p)
wire [23:0] rgb888 = {frame_pixel[15:11],frame_pixel[15:13],
                       frame_pixel[10:5],frame_pixel[10:9],
                       frame_pixel[4:0],frame_pixel[4:2]};
// A magenta corner flags a rejected frame; old good frame stays visible.
reg error_corner=0;
always @(posedge clk_sys) error_corner <= (frame_error || movie_error) && px < 8 && py < 8;
assign {VGA_R,VGA_G,VGA_B} = !de_d ? 24'd0 : error_corner ? 24'hff00ff :
    (frame_valid && select_frame) ? rgb888 : test_d;
assign AUDIO_L = reset ? 16'd0 : movie_mode ? movie_left : 16'd0;
assign AUDIO_R = reset ? 16'd0 : movie_mode ? movie_right : 16'd0;
reg [24:0] heartbeat = 0;
always @(posedge clk_sys) heartbeat <= heartbeat + 1'b1;
assign LED_USER = movie_mode ? (movie_error ? heartbeat[22] : movie_done) : heartbeat[24];
endmodule
