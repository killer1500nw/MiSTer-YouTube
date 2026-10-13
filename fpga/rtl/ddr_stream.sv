// SPDX-License-Identifier: GPL-2.0-or-later
// v0.16 stream controller (v0.15 plus header word 6, see docs/FPGA-YUV-v0.15.md). Eight 512KiB slots at physical 0x30001000.
// Per record it reads the 64-byte header (picture geometry for the scaler) and copies only the 6400 bytes of PCM
// into the audio banks; the YUV picture stays in DDR and is fetched row by row by yuv_scaler while it is displayed.
// Single outstanding Avalon read, one 64-bit beat. Producer publication follows payload readback.
// The acknowledged "consumed" count lags the commit count by one record: the record on screen is only released
// when its successor is committed, because the scaler is still reading it from DDR.
module ddr_stream #(
 parameter integer POLL_CLOCKS=40000
)(
 input wire clk,reset,enable,busy,valid,
 input wire il,                                   // the core is showing 480i (published in the stats word, bit 33)
 input wire [63:0] dout,
 output wire [28:0] addr,
 output wire [7:0] burst,be,
 output wire rd,wr,
 output wire [63:0] din,
 output wire audio_wr,audio_ready,
 output wire [13:0] audio_addr,
 output wire [7:0] audio_data,
 input wire commit,
 output reg clear_frame=0,error=0,done=0,
 // header of the record that is waiting to be committed (stable until the commit)
 output reg [63:0] hdr0=0,hdr1=0,hdr2=0,hdr3=0,hdr4=0,hdr5=0,hdr6=0,hdr7=0,
 output wire [2:0] hdr_slot,
 output wire record_ready,
 // Controller/keyboard state, published every poll at BASE+5 / BASE+6 (see PROTOCOL.md).
 input wire [63:0] in0,in1
);
 localparam [28:0] BASE=29'h06000000;
 localparam [28:0] AUDIO_BEATS=29'h0000E800;      // 0x74000 bytes / 8
 localparam [31:0] MAGIC=32'h35565559;            // "YUV5"
 localparam [31:0] IDENT=32'h37445459;            // v0.16b identity: header words 6 and 7 (v0.16: 36445459 had word 6 only, v0.15: 35445459, v0.14: 34445459)
 localparam OFF=0,IDLE=1,HEART=2,ACK=3,STATS=4,RHEAD=5,WHEAD=6,
  CHEAD=7,RPROD=8,WPROD=9,CPROD=10,BEGIN_FRAME=11,
  RDATA=12,WDATA=13,EMIT=14,END_FRAME=15,WAIT_COMMIT=16,WIN0=17,WIN1=18,
  HRD=19,HWAIT=20,HCHK=21;
 reg [4:0] state=OFF;
 reg [31:0] heart=0,session=0,consumed=0,played=0;
 reg [63:0] header=0,producer=0,word_data=0,stats_data=0,in0_r=0,in1_r=0;
 reg [17:0] byte_count=0;
 reg [19:0] delay_count=0;
 reg [26:0] watchdog=0;
 reg [2:0] hi=0;
 reg started=0,stopping=0;
 wire [31:0] available=producer[31:0]-consumed;
 wire [31:0] acked=(consumed==0) ? 32'd0 : consumed-32'd1;      // the displayed record is not released yet
 wire [28:0] slot_beats=BASE+29'd512+{10'd0,consumed[2:0],16'd0};
 assign burst=1;
 assign be=8'hff;
 assign hdr_slot=consumed[2:0];
 assign record_ready=(state==WAIT_COMMIT);
 assign rd=(state==RHEAD || state==RPROD || state==RDATA || state==HRD);
 assign wr=(state==HEART || state==ACK || state==STATS || state==WIN0 || state==WIN1);
 // All outputs remain stable for the entire waitrequest interval.
 assign addr=state==HEART ? BASE+29'd4 : state==ACK ? BASE+29'd2 :
             state==STATS ? BASE+29'd3 : state==WIN0 ? BASE+29'd5 :
             state==WIN1 ? BASE+29'd6 : state==RPROD ? BASE+29'd1 :
             state==HRD ? slot_beats+{26'd0,hi} :
             state==RDATA ? slot_beats+AUDIO_BEATS+{14'd0,byte_count[17:3]} : BASE;
 assign din=state==HEART ? {heart,IDENT} :
            state==ACK ? {session,acked} : state==WIN0 ? in0_r :
            state==WIN1 ? in1_r : stats_data;
 assign audio_wr=enable && !stopping && state==EMIT;
 assign audio_addr=byte_count[13:0];
 assign audio_data=word_data[7:0];
 assign audio_ready=enable && !stopping && state==WAIT_COMMIT && !error;
 always @(posedge clk) begin
  clear_frame<=0;
  if(!enable) stopping<=1;
  // Bus watchdog only: awaiting network data or a frame boundary is normal.
  if(rd || wr || state==WHEAD || state==WPROD || state==HWAIT || state==WDATA) begin
   watchdog<=watchdog+1'b1;
   if(&watchdog) error<=1;
  end else watchdog<=0;
  case(state)
   OFF: begin
    delay_count<=0;session<=0;consumed<=0;played<=0;started<=0;
    done<=0;error<=0;watchdog<=0;
    if(enable) begin stopping<=0;clear_frame<=1;state<=IDLE;end
   end
   IDLE: begin
    if(!enable || stopping) state<=OFF;
    else if(delay_count==POLL_CLOCKS-1) begin
     delay_count<=0;heart<=heart+1'b1;in0_r<=in0;in1_r<=in1;state<=HEART;
    end else delay_count<=delay_count+1'b1;
   end
   HEART: if(!busy) state<=ACK;
   ACK: if(!busy) begin stats_data<={30'd0,il,error,played};state<=STATS;end
   STATS: if(!busy) state<=WIN0;
   WIN0: if(!busy) state<=WIN1;
   WIN1: if(!busy) state<=RHEAD;
   RHEAD: if(!busy) begin
    if(valid) begin header<=dout;state<=CHEAD;end else state<=WHEAD;
   end
   WHEAD: if(valid) begin header<=dout;state<=CHEAD;end
   CHEAD: begin
    if(!enable || stopping) state<=OFF;
    else if(header[31:0]!=32'h33515459 || header[63:32]==0) begin
     // A stop/idle command is also the recovery path: it clears a latched
     // error, so the player can restart without toggling RAM streaming.
     if(session!=0) clear_frame<=1;
     session<=0;consumed<=0;played<=0;started<=0;done<=0;error<=0;state<=IDLE;
    end else if(error) state<=IDLE;
    else if(header[63:32]!=session) begin
     session<=header[63:32];consumed<=0;played<=0;started<=0;
     done<=0;clear_frame<=1;state<=IDLE;
    end else state<=RPROD;
   end
   RPROD: if(!busy) begin
    if(valid) begin producer<=dout;state<=CPROD;end else state<=WPROD;
   end
   WPROD: if(valid) begin producer<=dout;state<=CPROD;end
   CPROD: begin
    if(!enable || stopping) state<=OFF;
    else if(available>8 || (producer[63:32]!=0 && producer[63:32]<producer[31:0])) begin
     error<=1;state<=IDLE;
    end else if(available!=0 && (started || available>=4 ||
                                (producer[63:32]!=0 && producer[63:32]==producer[31:0]))) begin
     byte_count<=0;hi<=0;started<=1;done<=0;state<=HRD;
    end else begin
     done<=started && producer[63:32]!=0 && consumed==producer[63:32];
     state<=IDLE;
    end
   end
   // Header words 0..7 of the slot: geometry, flags, the picture's base address and plane offsets for the scaler.
   HRD: if(!busy) begin
    if(valid) begin
     case(hi)
      0: hdr0<=dout;
      1: hdr1<=dout;
      2: hdr2<=dout;
      3: hdr3<=dout;
      4: hdr4<=dout;
      5: hdr5<=dout;
      6: hdr6<=dout;
      default: hdr7<=dout;
     endcase
     if(hi==3'd7) state<=HCHK;
     else begin hi<=hi+1'b1; state<=HRD; end
    end else state<=HWAIT;
   end
   HWAIT: if(valid) begin
    case(hi)
     0: hdr0<=dout;
     1: hdr1<=dout;
     2: hdr2<=dout;
     3: hdr3<=dout;
     4: hdr4<=dout;
     5: hdr5<=dout;
     6: hdr6<=dout;
     default: hdr7<=dout;
    endcase
    if(hi==3'd7) state<=HCHK;
    else begin hi<=hi+1'b1; state<=HRD; end
   end
   HCHK: begin
    if(!enable || stopping) state<=OFF;
    else if(hdr0[31:0]!=MAGIC) begin error<=1;state<=IDLE;end
    else state<=BEGIN_FRAME;
   end
   BEGIN_FRAME: if(!enable || stopping) state<=OFF;else state<=RDATA;
   RDATA: if(!busy) begin
    if(valid) begin word_data<=dout;state<=EMIT;end else state<=WDATA;
   end
   WDATA: if(valid) begin word_data<=dout;state<=EMIT;end
   EMIT: begin
    if(!enable || stopping) state<=OFF;
    else begin
     word_data<={8'd0,word_data[63:8]};byte_count<=byte_count+1'b1;
     if(byte_count==6399) state<=END_FRAME;
     else if(byte_count[2:0]==7) state<=RDATA;
    end
   end
   END_FRAME: if(!enable || stopping) state<=OFF;else state<=WAIT_COMMIT;
   WAIT_COMMIT: begin
    if(!enable || stopping) state<=OFF;
    else if(commit) begin consumed<=consumed+1'b1;played<=played+1'b1;state<=IDLE;end
   end
   default: state<=OFF;
  endcase
  if(reset) begin
   state<=OFF;session<=0;consumed<=0;played<=0;started<=0;
   stopping<=0;clear_frame<=0;error<=0;done<=0;delay_count<=0;watchdog<=0;
  end
 end
endmodule
