`timescale 1ns/1ps
module tb_crt240;
reg clk=0, reset=1;
reg [1:0] pattern=0;
wire ce,hs,vs,de;
wire [7:0] r,g,b;
crt240 dut(clk,reset,pattern,ce,hs,vs,de,r,g,b,,);
always #25 clk=~clk; // 20 MHz
integer frame,px,py,active,hsp,vsp,cycle,spacing=0;
initial begin
    // Check ten whole frames, including reset in the middle of a line.
    for (frame=0;frame<10;frame=frame+1) begin
        active=0;hsp=0;vsp=0;
        for (py=0;py<262;py=py+1) begin
            for(px=0;px<424;px=px+1) begin
                for(cycle=0;cycle<3;cycle=cycle+1) begin
                    @(negedge clk);
                    if(ce !== (cycle==1)) $fatal(1,"Incorrect pixel enable cadence");
                    if(cycle==1) begin
                        if(de !== (px<320 && py<240)) $fatal(1,"DE geometry error");
                        if(hs !== (px>=336 && px<368)) $fatal(1,"HS timing error");
                        if(vs !== (py>=244 && py<247)) $fatal(1,"VS timing error");
                        if(!de && {r,g,b}!==24'd0) $fatal(1,"Non-black blanking");
                        if(^{r,g,b}===1'bx) $fatal(1,"Unknown pixel");
                        active=active+de;hsp=hsp+hs;vsp=vsp+vs;
                    end
                    reset=(frame==0 || (frame==4 && py==111 && px>=55 && px<61));
                end
            end
        end
        if(active!=76800 || hsp!=8384 || vsp!=1272) $fatal(1,"Frame totals error");
        pattern=frame%4;
    end
    $display("PASS: 10 frames; 320x240 active; 424x262 total; /3 CE; HS32 VS3; black blanking; continuous sync during reset.");
    $finish;
end
initial begin #200000000; $fatal(1,"Simulation watchdog"); end
endmodule
