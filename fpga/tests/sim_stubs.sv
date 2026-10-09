// Simulation only. Stubs the HPS transport and PLL, not frame storage/video.
module pll(input refclk,rst,output outclk_0,locked);
assign outclk_0=refclk;assign locked=1'b1;
endmodule
module hps_io #(parameter CONF_STR=0, VDNUM=2)(
 input clk_sys, inout [45:0] HPS_BUS, inout [35:0] EXT_BUS,
 inout [21:0] gamma_bus, output [1:0] buttons,
 output [127:0] status, input [15:0] status_menumask,
 output reg ioctl_download=0,ioctl_wr=0,
 output reg [26:0] ioctl_addr=0, output reg [7:0] ioctl_dout=0,
 output [15:0] ioctl_index, input ioctl_wait,
 output reg [1:0] img_mounted=0, output reg [63:0] img_size=0,
 input [31:0] sd_lba [2], input [5:0] sd_blk_cnt [2],
 input [1:0] sd_rd,sd_wr, output reg [1:0] sd_ack=0, output reg sd_buff_wr=0,
 output reg [13:0] sd_buff_addr=0, output reg [7:0] sd_buff_dout=0,
 input [7:0] sd_buff_din [2]
);
assign buttons=0;assign status=0;assign ioctl_index=1;
endmodule
