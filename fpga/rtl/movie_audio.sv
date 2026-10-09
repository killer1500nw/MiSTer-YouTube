// SPDX-License-Identifier: GPL-2.0-or-later
// Double-buffered stereo PCM. One 1600-sample segment per two native refreshes.
// 424 pixels * 262 lines * 3 clocks/pixel * 2 refreshes = 666,528 clocks.
// Effective source sample rate ~48009.986 Hz; framework handles physical output.
module movie_audio #(
 parameter integer SEGMENT_CLOCKS=666528
)(
 input wire clk, reset, commit,
 input wire wr,
 input wire [13:0] addr,
 input wire [7:0] data,
 output reg [15:0] left=0, right=0
);
 (* ramstyle="M10K" *) reg [31:0] bank0 [0:1599];
 (* ramstyle="M10K" *) reg [31:0] bank1 [0:1599];
 reg [31:0] q0, q1;
 reg [23:0] lo=0;
 reg front=0, active=0;
 reg [11:0] sample_index=0;
 reg [20:0] phase=0;
 reg [1:0] fetch=0;
 wire [21:0] next_phase={1'b0,phase}+22'd1600;
 wire sample_tick=next_phase>=SEGMENT_CLOCKS;
 wire [31:0] sample_word=front ? q1 : q0;
 // No reset on the RAM ports: keep this as embedded memory, not registers.
 always @(posedge clk) begin
  q0<=bank0[sample_index];q1<=bank1[sample_index];
  if(wr && !reset && addr<6400 && addr[1:0]==3) begin
   if(front) bank0[addr[13:2]]<={data,lo};
   else bank1[addr[13:2]]<={data,lo};
  end
 end
 always @(posedge clk) begin
  if(wr) case(addr[1:0])
   0: lo[7:0]<=data;
   1: lo[15:8]<=data;
   2: lo[23:16]<=data;
   default: ;
  endcase
  fetch<={fetch[0],1'b0};
  if(fetch[1] && active) begin
   left<=sample_word[15:0];right<=sample_word[31:16];
  end
  if(active) begin
   if(sample_tick) begin
    phase<=next_phase-SEGMENT_CLOCKS;
    if(sample_index==1599) begin
     active<=0;fetch<=0;
     if(!commit) begin left<=0;right<=0;end
    end else begin sample_index<=sample_index+1'b1;fetch<=2'b01;end
   end else phase<=next_phase[20:0];
  end
  // Commit has priority over segment end for gap-free consecutive records.
  if(commit) begin
   front<=~front;sample_index<=0;phase<=0;active<=1;fetch<=2'b01;
  end
  if(reset) begin
   front<=0;active<=0;sample_index<=0;phase<=0;fetch<=0;left<=0;right<=0;lo<=0;
  end
 end
endmodule
