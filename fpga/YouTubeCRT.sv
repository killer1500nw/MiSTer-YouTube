// SPDX-License-Identifier: GPL-2.0-or-later
// Native CRT YUV-streaming stage of the MiSTer YouTube project (v0.15): the ARM decodes, this core scales.
module emu (
    `include "sys/emu_ports.vh"
);
wire clk_sys;
wire movie_commit;
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
    "-,Native CRT 640x480 YUV URL stream;",
    "-;",
    "O[6],Output,240p,480i;",
    "O[7],480i field order,Normal,Swapped;",
    "O[8],Analog dither,Off,On;",
    "O[10:9],480i flicker filter,Off,Low,Medium,High;",
    "O[12:11],Composite blur,Off,Low,Medium,High;",
    "-;",
    "T[0],Reset;",
    "J1,A,B,X,Y,L,R,Select,Start;",
    "jn,A,B,X,Y,L,R,Select,Start;",
    "v,6;",
    "V,v0.16b-fb-",`BUILD_DATE
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
wire [7:0] movie_data;
wire movie_audio_wr, movie_audio_ready;
wire [13:0] movie_audio_addr;
wire [15:0] movie_left, movie_right;
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
assign ioctl_wait = 1'b0;
// Retain the template PLL instance name to match its timing constraints.
pll pll (.refclk(CLK_50M), .rst(1'b0), .outclk_0(clk_sys), .locked(pll_locked));
wire reset = RESET | status[0] | buttons[1] | ~pll_locked;
assign CLK_VIDEO = clk_sys;
wire raw_ce, raw_hs, raw_vs, raw_de, raw_il, raw_field;
wire [9:0] px;
wire [8:0] py, next_y;
wire next_field, next_il;
wire [7:0] test_r, test_g, test_b;
crt240 video (
    .clk(clk_sys), .reset(reset), .pattern(2'b00), .interlace(status[6]), .il_active(raw_il), .field(raw_field),
    .ce_pix(raw_ce), .hs(raw_hs), .vs(raw_vs), .de(raw_de),
    .r(test_r), .g(test_g), .b(test_b), .pixel_x(px), .pixel_y(py),
    .next_y(next_y), .next_field(next_field), .next_il(next_il)
);
wire swap_boundary = raw_ce && (px == 0) && (py == 240);
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

// ---- DDR: the stream controller (mailbox, audio) and the scaler (picture rows) share the port
wire [28:0] s_addr, c_addr;
wire [7:0] s_burst, s_be, c_burst;
wire s_rd, s_wr, c_rd, c_want, c_grant, s_busy, s_valid, c_busy, c_valid;
wire [63:0] s_din;
wire [63:0] h0, h1, h2, h3, h4, h5, h6, h7;
wire [2:0] h_slot;
wire rec_ready;
ddr_stream stream (
    .clk(clk_sys), .reset(reset), .enable(movie_mode), .il(raw_il),
    .busy(s_busy), .valid(s_valid), .dout(DDRAM_DOUT),
    .addr(s_addr), .burst(s_burst), .be(s_be),
    .rd(s_rd), .wr(s_wr), .din(s_din),
    .audio_wr(movie_audio_wr), .audio_addr(movie_audio_addr), .audio_data(movie_data),
    .audio_ready(movie_audio_ready), .commit(movie_commit),
    .clear_frame(stream_clear), .error(movie_error), .done(movie_done),
    .hdr0(h0), .hdr1(h1), .hdr2(h2), .hdr3(h3), .hdr4(h4), .hdr5(h5), .hdr6(h6), .hdr7(h7), .hdr_slot(h_slot), .record_ready(rec_ready),
    .in0({joy1,joy0}), .in1({key_seq,key_ev})
);
ddr_arb arb (
    .clk(clk_sys), .reset(reset),
    .s_addr(s_addr), .s_burst(s_burst), .s_be(s_be), .s_rd(s_rd), .s_wr(s_wr), .s_din(s_din),
    .s_busy(s_busy), .s_valid(s_valid),
    .c_want(c_want), .c_addr(c_addr), .c_burst(c_burst), .c_rd(c_rd),
    .c_grant(c_grant), .c_busy(c_busy), .c_valid(c_valid),
    .ddr_addr(DDRAM_ADDR), .ddr_burst(DDRAM_BURSTCNT), .ddr_be(DDRAM_BE),
    .ddr_rd(DDRAM_RD), .ddr_wr(DDRAM_WE), .ddr_din(DDRAM_DIN),
    .ddr_busy(DDRAM_BUSY), .ddr_valid(DDRAM_DOUT_READY)
);
reg movie_tick=0;
always @(posedge clk_sys) begin
    if(reset || movie_clear || !movie_mode) movie_tick<=0;
    else if(swap_boundary) movie_tick<=~movie_tick;
end
// Commit a complete picture and its PCM segment at the same blanking slot. In 480i a record is only committed after
// field 1, so both fields of a picture come from the same record. Late records hold the previous image and insert
// silence, not A/V drift.
wire commit_phase = raw_il ? raw_field : (movie_tick==0);
wire frame_slot = swap_boundary && commit_phase && movie_audio_ready && !movie_error;
assign movie_commit = movie_mode && rec_ready && frame_slot;
movie_audio sound (
    .clk(clk_sys), .reset(reset | movie_clear), .commit(movie_commit),
    .seg_clocks(raw_il ? 21'd1335600 : 21'd1333056),
    .wr(movie_audio_wr), .addr(movie_audio_addr), .data(movie_data),
    .left(movie_left), .right(movie_right)
);
wire have_frame;
wire [23:0] scaled_rgb;
yuv_scaler scaler (
    .clk(clk_sys), .reset(reset | movie_clear), .ce_pix(raw_ce), .px(px),
    .ny(next_y), .nfield(next_field), .nil(next_il), .flicker(status[10:9]), .blur(status[12:11]), .prog(~raw_il),
    .cfg_load(movie_commit), .cfg_slot(h5[4] ? h5[7:5] : h_slot), .cfg_flags(h0[34:32] | {status[8],2'b00}),
    .cfg_w1(h1), .cfg_w2(h2), .cfg_w3(h3), .cfg_w4(h4), .cfg_w5(h5), .cfg_w6(h6), .cfg_w7(h7),
    .have_frame(have_frame),
    .want(c_want), .grant(c_grant), .busy(c_busy), .valid(c_valid), .dout(DDRAM_DOUT),
    .addr(c_addr), .burst(c_burst), .rd(c_rd), .rgb(scaled_rgb)
);
// The scaler's pipeline has a fixed latency (checked in simulation: tb_yuv_scaler LAT): every video signal is delayed
// by the same number of clocks so pixels and syncs stay together.
localparam LAT = 15;
reg [LAT-1:0] ce_sr=0, hs_sr=0, vs_sr=0, de_sr=0, f1_sr=0, corner_sr=0;
reg [23:0] test_sr [0:LAT-1];
integer k;
// A magenta corner flags a rejected record; the old picture stays visible.
wire corner_now = movie_error && px < 8 && py < 8;
always @(posedge clk_sys) begin
    ce_sr <= {ce_sr[LAT-2:0], raw_ce};
    hs_sr <= {hs_sr[LAT-2:0], raw_hs};
    vs_sr <= {vs_sr[LAT-2:0], raw_vs};
    de_sr <= {de_sr[LAT-2:0], raw_de};
    f1_sr <= {f1_sr[LAT-2:0], raw_il & (raw_field ^ status[7])};
    corner_sr <= {corner_sr[LAT-2:0], corner_now};
    test_sr[0] <= {test_r,test_g,test_b};
    for(k=1;k<LAT;k=k+1) test_sr[k] <= test_sr[k-1];
end
assign CE_PIXEL = ce_sr[LAT-1];
assign VGA_HS = hs_sr[LAT-1];
assign VGA_VS = vs_sr[LAT-1];
assign VGA_DE = de_sr[LAT-1];
assign VGA_F1 = f1_sr[LAT-1];      // interlaced field flag for the HDMI scaler (0 in 240p)
assign {VGA_R,VGA_G,VGA_B} = !de_sr[LAT-1] ? 24'd0 : corner_sr[LAT-1] ? 24'hff00ff :
    have_frame ? scaled_rgb : test_sr[LAT-1];
assign AUDIO_L = reset ? 16'd0 : movie_mode ? movie_left : 16'd0;
assign AUDIO_R = reset ? 16'd0 : movie_mode ? movie_right : 16'd0;
reg [24:0] heartbeat = 0;
always @(posedge clk_sys) heartbeat <= heartbeat + 1'b1;
assign LED_USER = movie_mode ? (movie_error ? heartbeat[22] : movie_done) : heartbeat[24];
endmodule
