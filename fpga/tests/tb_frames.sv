`timescale 1ns/1ps
module tb_frames;
reg clk=0,reset=1,download=0,wr=0,boundary=0;
reg [15:0] index=1;
reg [26:0] addr=0;
reg [7:0] data=0;
reg [16:0] rd=0;
wire wait_req,valid,err;
wire [15:0] pixel;
frame_store dut(clk,reset,download,index,wr,addr,data,wait_req,boundary,rd,pixel,valid,err);
always #25 clk=~clk;
integer i;
function [15:0] pattern(input integer a,input integer n);
    pattern=(a ^ (n*16'h5a5a));
endfunction
task send_byte(input integer a,input [7:0] d);
begin
    @(negedge clk);addr=a;data=d;wr=1;
    @(negedge clk);wr=0;
end endtask
task start;
begin @(negedge clk);download=1;repeat(2) @(negedge clk);end endtask
task stop;
begin @(negedge clk);download=0;repeat(2) @(negedge clk);end endtask
task send_frame(input integer n);
reg [15:0] p;
begin
    start;
    for(integer a=0;a<76800;a=a+1) begin
        p=pattern(a,n);send_byte(2*a,p[7:0]);send_byte(2*a+1,p[15:8]);
    end
    stop;
end endtask
task swap;
begin @(negedge clk);boundary=1;@(negedge clk);boundary=0;end endtask
task check_frame(input integer n);
begin
    for(integer a=0;a<76800;a=a+1) begin
        @(negedge clk);rd=a;@(negedge clk);
        if(pixel !== pattern(a,n)) $fatal(1,"RAM pixel mismatch address %d",a);
    end
end endtask
initial begin
    repeat(3) @(negedge clk);reset=0;
    send_frame(1);
    if(valid || !wait_req || err) $fatal(1,"Frame published before blanking");
    swap;
    if(!valid || wait_req) $fatal(1,"Missing first frame");
    check_frame(1);
    send_frame(2);
    if(!wait_req || err) $fatal(1,"Missing pending frame");
    check_frame(1); // old front remains intact throughout second download
    swap;check_frame(2);
    start;send_byte(0,8'hff);stop;
    if(!err || wait_req) $fatal(1,"Short frame accepted");
    swap;check_frame(2);
    start;send_byte(0,8'h00);send_byte(3,8'hff);stop;
    if(!err || wait_req) $fatal(1,"Non-sequential frame accepted");
    swap;check_frame(2);
    send_frame(3);swap;check_frame(3);
    // Exact frame followed by one extra byte must not replace the front.
    start;
    for(i=0;i<153601;i=i+1) send_byte(i,8'h55);
    stop;
    if(!err || wait_req) $fatal(1,"Oversized frame accepted");
    swap;check_frame(3);
    @(negedge clk);reset=1;@(negedge clk);
    if(valid || wait_req) $fatal(1,"Reset did not invalidate image");
    $display("PASS: all 76800 pixels; little endian; double buffering; blanking-only swaps; short/out-of-order/oversized rejection; reset.");
    $finish;
end
initial begin #300000000; $fatal(1,"watchdog");end
endmodule
