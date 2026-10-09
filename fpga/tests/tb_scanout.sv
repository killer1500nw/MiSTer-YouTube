`timescale 1ns/1ps
module tb_scanout;
reg clk=0,reset=1;
wire ce,hs,vs,de;
wire [7:0] r,g,b;
emu dut(.CLK_50M(clk),.RESET(reset),.CE_PIXEL(ce),.VGA_HS(hs),.VGA_VS(vs),.VGA_DE(de),.VGA_R(r),.VGA_G(g),.VGA_B(b));
always #25 clk=~clk;
reg checking=0;
integer xx,yy,checked=0;
reg [15:0] expected;
reg [23:0] rgb;
always @(posedge clk) begin
 xx=dut.px;yy=dut.py;
 #1;
 if(checking && ce) begin
  if(de !== (xx<320 && yy<240)) $fatal(1,"Scanout DE misaligned");
  if(hs !== (xx>=336 && xx<368) || vs !== (yy>=244 && yy<247)) $fatal(1,"Scanout sync misaligned");
  if(de) begin
   `ifdef STREAM_FRAME
   expected=(yy*320+xx)^16'h5a5a;
`else
   expected=(yy*320+xx)^16'h5a5a;
`endif
   rgb={expected[15:11],expected[15:13],expected[10:5],expected[10:9],expected[4:0],expected[4:2]};
   if({r,g,b} !== rgb) $fatal(1,"Scanout RGB misaligned x=%d y=%d",xx,yy);
   checked=checked+1;
  end else if({r,g,b}!==24'd0) $fatal(1,"Blanking RGB nonzero");
 end
end
integer a;
reg [15:0] wordvalue;
initial begin
 repeat(4) @(negedge clk);reset=0;
 `ifdef STREAM_FRAME
 force dut.movie_mode=1'b1;
 force dut.movie_clear=1'b0;
 force dut.movie_error=1'b0;
 force dut.movie_dl=dut.hps_io.ioctl_download;
 force dut.movie_wr=dut.hps_io.ioctl_wr;
 force dut.movie_addr=dut.hps_io.ioctl_addr;
 force dut.movie_data=dut.hps_io.ioctl_dout;
 force dut.movie_audio_ready=1'b1;
`endif
 dut.hps_io.ioctl_download=1;
 repeat(2) @(negedge clk);
 `ifdef STREAM_FRAME
 for(a=0;a<153600;a=a+1) begin
`else
 for(a=0;a<153600;a=a+1) begin
`endif
  wordvalue=(a/2)^16'h5a5a;
  dut.hps_io.ioctl_addr=a;
  dut.hps_io.ioctl_dout=a%2 ? wordvalue[15:8] : wordvalue[7:0];
  dut.hps_io.ioctl_wr=1;
  @(negedge clk);dut.hps_io.ioctl_wr=0;@(negedge clk);
 end
 dut.hps_io.ioctl_download=0;
 wait(dut.frame_valid);@(negedge clk);checking=1;
 wait(checked==76800);@(negedge clk);
 `ifdef STREAM_FRAME
 $display("PASS: actual emu native 320x240 stream, every displayed pixel, RGB/CE/DE/HS/VS alignment.");
`else
 $display("PASS: actual emu scanout, full 320x240 RGB565 image, RGB/CE/DE/HS/VS alignment, black blanking.");
`endif
 $finish;
end
initial begin #100000000; $fatal(1,"Watchdog");end
endmodule
