`timescale 1ns/1ps
// 480i timing: field lengths, half-line vsync offset, mode switching at frame
// boundaries, and unchanged line/pixel structure.
module tb_interlace;
reg clk=0, reset=0, interlace=0;
always #12.5 clk=~clk;
wire il, fld, ce, hs, vs, de;
crt240 dut(.clk(clk),.reset(reset),.pattern(2'd0),.interlace(interlace),.il_active(il),.field(fld),
    .ce_pix(ce),.hs(hs),.vs(vs),.de(de),.r(),.g(),.b(),.pixel_x(),.pixel_y(),.next_y(),.next_field(),.next_il());
integer cyc=0, last_rise=-1, rise_at=0, n=0, vslen=0, bad=0, active=0;
integer spacing[0:63];
integer fields_seen[0:63];
integer f_active[0:63];
reg old_vs=0;
always @(posedge clk) begin
    cyc<=cyc+1;
    old_vs<=vs;
    if(ce && de) active<=active+1;
    if(vs && !old_vs) begin
        if(last_rise>=0 && n<64) begin
            spacing[n]=cyc-last_rise; fields_seen[n]=fld; f_active[n]=active; n=n+1;
        end
        active<=0;
        last_rise<=cyc; rise_at<=cyc;
    end
    if(!vs && old_vs && (cyc-rise_at)!=3*2544) bad<=bad+1;   // vsync is always exactly 3 lines
end
integer i, k, half=0, full=0;
initial begin
    // 3 progressive frames, then switch on 480i mid-frame, then off again.
    repeat(3*262*2544) @(posedge clk);
    interlace=1;
    repeat(10*525*2544/2) @(posedge clk);
    interlace=0;
    repeat(4*262*2544) @(posedge clk);
    for(i=0;i<n;i=i+1) begin
        if(spacing[i]==262*2544) full=full+1;
        else if(spacing[i]==667800) half=half+1;
        else $fatal(1,"Unexpected vsync spacing %0d at %0d", spacing[i], i);
        if(f_active[i]!=153600 && i>1) $fatal(1,"Active pixels per field %0d at %0d", f_active[i], i);
    end
    // Steady 480i: every pair of fields is 525 lines, fields alternate 0/1.
    k=0;
    for(i=1;i<n-1;i=i+1)
        if(spacing[i]==667800 && spacing[i+1]==667800) begin
            if(fields_seen[i]==fields_seen[i+1]) $fatal(1,"Field flag does not alternate");
            k=k+1;
        end
    if(k<8) $fatal(1,"Too few steady 480i fields (%0d)",k);
    if(bad!=0) $fatal(1,"VSync width wrong %0d times",bad);
    if(full<3) $fatal(1,"Progressive frames missing");
    $display("PASS: 480i = 262/263-line fields, 262.5-line vsync spacing (667800 clocks), 525-line frame, field flag alternates, VS 3 lines, switching at frame boundaries, 240p unchanged (%0d half, %0d full).",half,full);
    $finish;
end
initial begin #2000000000; $fatal(1,"watchdog"); end
endmodule
