`timescale 1ns/1ps
module tb_stream;
`ifdef REAL_EMU
localparam PERIOD=666528;
`else
localparam PERIOD=360000;
`endif
localparam BASE=29'h06000000;
reg clk=0,reset=1,enable=0;
always #25 clk=~clk;
wire [28:0] addr;
wire [7:0] burst,be;
wire rd,wr,dl,vwr,awr,ready,clr,error,done;
wire [63:0] din;
wire [26:0] va;
wire [13:0] aa;
wire [7:0] data;
wire fw,fvalid,ferror;
wire [15:0] pixel,al,ar;
reg slot=0;
`ifdef REAL_EMU
wire commit=core.movie_commit;
`else
wire commit=slot && fw && ready && !error;
`endif
// Model payload contents procedurally: avoids a huge simulator-only RAM mux.
reg [63:0] mem[0:4];
integer slot_record[0:7];
function [63:0] read_memory(input [28:0] address);
 integer offset;
 begin
  offset=address-BASE;
  if(offset<5) read_memory=mem[offset];
  else read_memory=payload(slot_record[(offset-512)/32768],(offset-512)%32768);
 end
endfunction
integer cycle=0,delay=0;
reg pending=0;
reg [63:0] reply=0;
reg force_busy=0;
wire busy=force_busy || (cycle%11==0);
`ifdef ZERO_LATENCY
wire valid=rd && !busy;
wire [63:0] dout=read_memory(addr);
`else
wire valid=pending && delay==0;
wire [63:0] dout=reply;
`endif
`ifdef REAL_EMU
emu core(.CLK_50M(clk),.RESET(reset),.DDRAM_BUSY(busy),.DDRAM_DOUT_READY(valid),.DDRAM_DOUT(dout),
 .DDRAM_ADDR(addr),.DDRAM_BURSTCNT(burst),.DDRAM_BE(be),.DDRAM_RD(rd),.DDRAM_WE(wr),.DDRAM_DIN(din),
 .AUDIO_L(al),.AUDIO_R(ar));
wire [127:0] sim_status={122'd0,enable,5'd0};
initial force core.status=sim_status;
assign dl=core.movie_dl;assign vwr=core.movie_wr;assign awr=core.movie_audio_wr;
assign ready=core.movie_audio_ready;assign clr=core.movie_clear;
assign error=core.movie_error;assign done=core.movie_done;
assign va=core.movie_addr;assign aa=core.movie_audio_addr;assign data=core.movie_data;
assign fw=core.store_wait;assign fvalid=core.frame_valid;assign ferror=core.frame_error;assign pixel=core.frame_pixel;
wire [4:0] current_state=core.stream.state;
`else
ddr_stream #(.POLL_CLOCKS(8)) dut(clk,reset,enable,busy,valid,dout,addr,burst,be,rd,wr,din,
 dl,vwr,awr,ready,va,aa,data,commit,clr,error,done);
frame_store frames(clk,reset|clr,dl,16'd1,vwr,va,data,fw,slot&&ready&&!error,17'd76799,pixel,fvalid,ferror);
movie_audio #(.SEGMENT_CLOCKS(PERIOD)) sound(clk,reset|clr,commit,awr,aa,data,al,ar);
wire [4:0] current_state=dut.state;
`endif
reg held=0;
reg [28:0] held_addr;
reg [63:0] held_data;
reg held_rd,held_wr;
integer read_count=0;
always @(posedge clk) begin
 if(held && !reset && (addr!==held_addr || din!==held_data || rd!==held_rd || wr!==held_wr))
  $fatal(1,"Avalon command changed while busy");
 held=(rd||wr)&&busy&&!reset;held_addr=addr;held_data=din;held_rd=rd;held_wr=wr;
 if(pending) begin
  if(delay>0) delay<=delay-1;
  else pending<=0;
 end
 if((rd||wr) && !busy && !reset) begin
  if(burst!=1 || be!=255 || (rd&&wr)) $fatal(1,"DDR command format");
  if(wr) begin
   if(addr<BASE+2 || addr>BASE+4) $fatal(1,"FPGA wrote CPU-owned or payload RAM");
   mem[addr-BASE]<=din;
  end
  if(rd) begin
   if(!(addr==BASE || addr==BASE+1 || (addr>=BASE+512 && addr<BASE+262656)))
    $fatal(1,"DDR read outside bounds");
   if(addr>=BASE+512) begin
    if((addr-BASE-512)%32768>=20000) $fatal(1,"Read slot padding");
    read_count=read_count+1;
   end
`ifndef ZERO_LATENCY
   if(pending) $fatal(1,"Multiple outstanding reads");
   pending<=1;delay<=cycle%3;reply<=read_memory(addr);
`endif
  end
 end
end
function [63:0] payload(input integer rec,input integer wordoff);
 integer k,off,val;
 begin
  payload=0;
  for(k=0;k<8;k=k+1) begin
   off=wordoff*8+k;
   if(off<153600) val=16'h1000+rec;
   else begin
    val=rec*4000+(off-153600)/4;
    if(off%4>=2) val=-val;
   end
   payload[k*8+:8]=off%2 ? (val>>8)&255 : val&255;
  end
 end
endfunction
integer sent=0,allowed=4,n,i,acknowledged;
reg produce=1;
// Producer respects the eight-slot window and publishes after all payload.
always @(negedge clk) begin
 slot=(cycle%PERIOD==PERIOD-1);
 if(produce && !reset && mem[2][63:32]==32'h12345678) begin
  acknowledged=mem[2][31:0];
  if(sent<allowed && sent-acknowledged<8) begin
   slot_record[sent%8]=sent+1;
   sent=sent+1;mem[1][31:0]=sent;
   if(sent==12) mem[1][63:32]=12;
  end
 end
end
integer seen=0,start=-1,age,idx,checks=0,silent=0;
reg checking=1;
reg [15:0] el,er;
always @(posedge clk) begin
 cycle=cycle+1;
 if(commit) begin seen=seen+1;start=cycle;end
 #1;
 if(checking && start>=0) begin
  age=cycle-start;
  if(age==3 && pixel!==16'h1000+seen) $fatal(1,"Picture/PCM commit mismatch");
  if(age>=2 && age<PERIOD) begin
   idx=((age-2)*64'd1600)/PERIOD;
   el=seen*4000+idx;er=-(seen*4000+idx);
   if(al!==el || ar!==er) $fatal(1,"PCM mismatch frame %d sample %d L %h/%h R %h/%h",seen,idx,al,el,ar,er);
   checks=checks+1;
  end
  if(age>=PERIOD) begin
   if(al!==0 || ar!==0) $fatal(1,"Late/EOF audio not silent");
   silent=silent+1;
  end
 end
end
initial begin
 for(i=0;i<5;i=i+1) mem[i]=0;
 for(i=0;i<8;i=i+1) slot_record[i]=0;
 repeat(6) @(negedge clk);reset=0;
 repeat(20) @(negedge clk);
 if(rd||wr||dl) $fatal(1,"Default-off DDR activity");
 enable=1;mem[0]={32'h12345678,32'h33515459};
 wait(seen==4);
 // Empty the producer for >one segment: retain last image, mute, then resume.
 repeat(PERIOD*2+20) @(negedge clk);allowed=12;
 wait(done);repeat(PERIOD+20) @(negedge clk);
 if(error||ferror||seen!=12||read_count!=12*20000||checks<12*(PERIOD-4)||silent<PERIOD)
  $fatal(1,"Stream counts/error seen %d reads %d checks %d silent %d",seen,read_count,checks,silent);
 checking=0;produce=0;
 // Explicit stop command acknowledges idle and clears previous display/audio.
 mem[0]=0;wait(mem[2]==0);repeat(10) @(negedge clk);
 if(fvalid||al||ar) $fatal(1,"Session stop did not clear A/V");
 // New session resets counters and catches an invalid producer overrun.
 mem[1]=9;mem[0]={32'h23456789,32'h33515459};
 wait(error);wait(mem[3][32]);
 // Disable during a held bus command: finish the bus handshake, then stop.
 wait(rd||wr);@(negedge clk);force_busy=1;
 repeat(4) @(negedge clk);enable=0;
 repeat(4) @(negedge clk);force_busy=0;
 wait(current_state==0);repeat(20) @(negedge clk);
 if(rd||wr||dl||awr||vwr) $fatal(1,"DDR activity after disable");
 $display("PASS: 12 complete A/V records, 8-slot wrap, all PCM samples, atomic commits, late-data silence/restart, EOF, session stop, overrun rejection, disable under backpressure.");$finish;
end
initial begin #(PERIOD*30*50);$fatal(1,"stream watchdog state %d seen %d",current_state,seen);end
endmodule
