`timescale 1ns/1ps
module tb_crt240;
reg clk=0, reset=1;
reg [1:0] pattern=0;
wire ce,hs,vs,de;
wire [7:0] r,g,b;
crt240 dut(.clk(clk),.reset(reset),.pattern(pattern),.interlace(1'b0),.il_active(),.field(),
    .ce_pix(ce),.hs(hs),.vs(vs),.de(de),.r(r),.g(g),.b(b),.pixel_x(),.pixel_y(),.next_y(),.next_field(),.next_il());
always #12.5 clk=~clk; // 40 MHz
integer frame,px,py,active,hsp,vsp,cycle,spacing=0;
initial begin
    // Check ten whole frames, including reset in the middle of a line.
    for (frame=0;frame<10;frame=frame+1) begin
        active=0;hsp=0;vsp=0;
        for (py=0;py<262;py=py+1) begin
            for(px=0;px<848;px=px+1) begin
                for(cycle=0;cycle<3;cycle=cycle+1) begin
                    @(negedge clk);
                    if(ce !== (cycle==1)) $fatal(1,"Incorrect pixel enable cadence");
                    if(cycle==1) begin
                        if(de !== (px<640 && py<240)) $fatal(1,"DE geometry error");
                        if(hs !== (px>=672 && px<736)) $fatal(1,"HS timing error");
                        if(vs !== (py>=244 && py<247)) $fatal(1,"VS timing error");
                        if(!de && {r,g,b}!==24'd0) $fatal(1,"Non-black blanking");
                        if(^{r,g,b}===1'bx) $fatal(1,"Unknown pixel");
                        active=active+de;hsp=hsp+hs;vsp=vsp+vs;
                    end
                    reset=(frame==0 || (frame==4 && py==111 && px>=55 && px<61));
                end
            end
        end
        if(active!=153600 || hsp!=16768 || vsp!=2544) $fatal(1,"Frame totals error");
        pattern=frame%4;
    end
    $display("PASS: 10 frames; 640x240 active; 848x262 total; /3 CE; HS64 VS3; black blanking; continuous sync during reset.");
    $finish;
end
initial begin #200000000; $fatal(1,"Simulation watchdog"); end
endmodule
