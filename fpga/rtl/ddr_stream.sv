// SPDX-License-Identifier: GPL-2.0-or-later
// Eight 256KiB slots at physical 0x30001000. Each record is 160000 bytes:
// 320x240 RGB565LE (153600) followed by 1600 stereo S16LE samples (6400).
// Single outstanding Avalon read, one 64-bit beat, serialized into existing
// tested frame/audio stores. Producer publication follows payload readback.
module ddr_stream #(
 parameter integer POLL_CLOCKS=20000
)(
 input wire clk,reset,enable,busy,valid,
 input wire [63:0] dout,
 output wire [28:0] addr,
 output wire [7:0] burst,be,
 output wire rd,wr,
 output wire [63:0] din,
 output wire frame_download,frame_wr,audio_wr,audio_ready,
 output wire [26:0] frame_addr,
 output wire [13:0] audio_addr,
 output wire [7:0] frame_data,
 input wire commit,
 output reg clear_frame=0,error=0,done=0
);
 localparam [28:0] BASE=29'h06000000;
 localparam OFF=0,IDLE=1,HEART=2,ACK=3,STATS=4,RHEAD=5,WHEAD=6,
  CHEAD=7,RPROD=8,WPROD=9,CPROD=10,BEGIN_FRAME=11,
  RDATA=12,WDATA=13,EMIT=14,END_FRAME=15,WAIT_COMMIT=16;
 reg [4:0] state=OFF;
 reg [31:0] heart=0,session=0,consumed=0,played=0;
 reg [63:0] header=0,producer=0,word_data=0,stats_data=0;
 reg [17:0] byte_count=0;
 reg [19:0] delay_count=0;
 reg [26:0] watchdog=0;
 reg started=0,stopping=0;
 wire [31:0] available=producer[31:0]-consumed;
 assign burst=1;
 assign be=8'hff;
 assign rd=(state==RHEAD || state==RPROD || state==RDATA);
 assign wr=(state==HEART || state==ACK || state==STATS);
 // All outputs remain stable for the entire waitrequest interval.
 assign addr=state==HEART ? BASE+29'd4 : state==ACK ? BASE+29'd2 :
             state==STATS ? BASE+29'd3 : state==RPROD ? BASE+29'd1 :
             state==RDATA ? BASE+29'd512+{11'd0,consumed[2:0],15'd0}+{14'd0,byte_count[17:3]} : BASE;
 assign din=state==HEART ? {heart,32'h33445459} :
            state==ACK ? {session,consumed} : stats_data;
 assign frame_download=enable && !stopping &&
                       (state==BEGIN_FRAME || state==RDATA || state==WDATA || state==EMIT);
 assign frame_wr=enable && !stopping && state==EMIT && byte_count<153600;
 assign audio_wr=enable && !stopping && state==EMIT && byte_count>=153600;
 assign frame_addr={9'd0,byte_count};
 wire [17:0] audio_offset=byte_count-18'd153600;
 assign audio_addr=audio_offset[13:0];
 assign frame_data=word_data[7:0];
 assign audio_ready=enable && !stopping && state==WAIT_COMMIT && !error;
 always @(posedge clk) begin
  clear_frame<=0;
  if(!enable) stopping<=1;
  // Bus watchdog only: awaiting network data or a frame boundary is normal.
  if(rd || wr || state==WHEAD || state==WPROD || state==WDATA) begin
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
     delay_count<=0;heart<=heart+1'b1;state<=HEART;
    end else delay_count<=delay_count+1'b1;
   end
   HEART: if(!busy) state<=ACK;
   ACK: if(!busy) begin stats_data<={31'd0,error,played};state<=STATS;end
   STATS: if(!busy) state<=RHEAD;
   RHEAD: if(!busy) begin
    if(valid) begin header<=dout;state<=CHEAD;end else state<=WHEAD;
   end
   WHEAD: if(valid) begin header<=dout;state<=CHEAD;end
   CHEAD: begin
    if(!enable || stopping) state<=OFF;
    else if(header[31:0]!=32'h33515459 || header[63:32]==0) begin
     if(session!=0) clear_frame<=1;
     session<=0;consumed<=0;played<=0;started<=0;done<=0;state<=IDLE;
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
     byte_count<=0;started<=1;done<=0;state<=BEGIN_FRAME;
    end else begin
     done<=started && producer[63:32]!=0 && consumed==producer[63:32];
     state<=IDLE;
    end
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
     if(byte_count==159999) state<=END_FRAME;
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
