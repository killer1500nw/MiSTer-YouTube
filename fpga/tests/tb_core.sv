`timescale 1ns/1ps
// Whole v0.15 core (emu) against a DDR model that behaves like the player: a mailbox, YUV records in slots, producer
// publication that respects the one-record acknowledgement lag. It records what comes out of the VGA and audio pins;
// tools/run_core_sim.py compares that with the Python model.
//   +IL=1 (480i, default) / +IL=0   +NREC=n records in mem.hex   +LATE=n records in late.hex that reuse slots
module tb_core;
localparam [28:0] BASE = 29'h06000000;
reg clk = 0, reset = 1;
always #12.5 clk = ~clk;                           // 40 MHz
integer il_arg = 1, nrec = 6, nlate = 0;

// ---- DDR model: mailbox + 8 slots of 512 KiB, burst reads, random wait states and gaps
reg [63:0] mem [0:573951];                         // 512 beats of mailbox, 8 slots x 65536 beats, 3 overlay buffers x 16384
reg [63:0] late [0:1048575];                       // records that reuse slots, written just before they are published
wire [28:0] addr; wire [7:0] burst, be;
wire rd, wr, busy, valid;
wire [63:0] din, dout;
reg [31:0] lfsr = 32'h13579BDF;
reg active = 0, valid_r = 0;
reg [63:0] dout_r = 0;
reg [28:0] a_cur = 0; reg [7:0] left = 0; reg [3:0] lat = 0;
assign busy = (lfsr[2:0] == 3'b000) || active;
assign valid = valid_r;
assign dout = dout_r;
integer reads = 0, writes = 0, bursts = 0;
reg [28:0] held_addr; reg [63:0] held_din; reg held_rd = 0, held_wr = 0, held = 0;
always @(posedge clk) begin
    lfsr <= {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
    valid_r <= 0;
    // an Avalon command must not change while it is waiting to be accepted
    if (held && !reset && (addr !== held_addr || din !== held_din || rd !== held_rd || wr !== held_wr))
        $fatal(1, "Avalon command changed while busy");
    held <= (rd || wr) && busy && !reset; held_addr <= addr; held_din <= din; held_rd <= rd; held_wr <= wr;
    if (wr && !busy && !reset) begin
        mem[addr - BASE] <= din; writes = writes + 1;
    end
    if (rd && !busy && !reset) begin
        if (active) $fatal(1, "read while a burst is in flight");
        active <= 1; a_cur <= addr; left <= burst; lat <= 3 + lfsr[5:4];
        reads = reads + 1; if (burst > 1) bursts = bursts + 1;
        if (addr < BASE || addr - BASE > 573951) $fatal(1, "read outside the region: %h", addr);
    end
    if (active) begin
        if (lat != 0) lat <= lat - 1;
        else if (lfsr[8:7] != 0) begin
            valid_r <= 1; dout_r <= mem[a_cur - BASE];
            a_cur <= a_cur + 1; left <= left - 1;
            if (left == 1) active <= 0;
        end
    end
end

wire ce, hs, vs, de, f1;
wire [7:0] r, g, b;
wire [15:0] al, ar;
emu core (.CLK_50M(clk), .RESET(reset),
          .DDRAM_BUSY(busy), .DDRAM_DOUT_READY(valid), .DDRAM_DOUT(dout),
          .DDRAM_ADDR(addr), .DDRAM_BURSTCNT(burst), .DDRAM_BE(be), .DDRAM_RD(rd), .DDRAM_WE(wr), .DDRAM_DIN(din),
          .CE_PIXEL(ce), .VGA_HS(hs), .VGA_VS(vs), .VGA_DE(de), .VGA_F1(f1), .VGA_R(r), .VGA_G(g), .VGA_B(b),
          .AUDIO_L(al), .AUDIO_R(ar));

// ---- the "player": publish a record when the acknowledged count leaves room (ring of 8, never the displayed slot)
wire [31:0] ack = mem[2][31:0];
integer produced = 0, w;
always @(posedge clk) if (!reset && produced < nrec + nlate && produced < ack + 8 && mem[0][63:32] != 0) begin
    if (produced >= nrec)                           // a late record takes over a slot that is no longer on screen
        for (w = 0; w < 65536; w = w + 1) mem[512 + (produced % 8) * 65536 + w] = late[(produced - nrec) * 65536 + w];
    produced <= produced + 1;
    mem[1] <= {(produced + 1 >= nrec + nlate) ? 32'd1 : 32'd0, 32'd0} | (produced + 1);
    if (produced + 1 >= nrec + nlate) mem[1][63:32] <= produced + 1;
end

// ---- video capture from the VGA pins only: every active line, tagged with record, field and line index
integer commits = 0, rec = -1;
reg pend = 0, old_vs = 0, old_de = 0, fld = 0, fld_known = 0;
integer line = 0, x = 0, fcap, i;
reg [23:0] lbuf [0:639];
always @(posedge clk) begin
    if (core.movie_commit) begin commits <= commits + 1; pend <= 1; end
    if (ce) begin
        old_vs <= vs; old_de <= de;
        if (vs && !old_vs) begin
            line <= 0; fld_known <= 0;
            if (pend) begin rec <= commits - 1; pend <= 0; end
        end
        if (de) begin
            if (!old_de) begin x <= 1; lbuf[0] <= {r, g, b}; if (!fld_known) begin fld <= f1; fld_known <= 1; end end
            else begin lbuf[x] <= {r, g, b}; x <= x + 1; end
        end
        if (!de && old_de && rec >= 0) begin       // a line has ended: write it out
            $fwrite(fcap, "%0d %0d %0d", rec, fld, line);
            for (i = 0; i < 640; i = i + 1) $fwrite(fcap, " %06x", lbuf[i]);
            $fwrite(fcap, "\n");
        end
        if (!de && old_de) line <= line + 1;
    end
end

// ---- audio: every sample change, tagged with the record committed before it
reg [31:0] last_s = 0;
integer faud;
always @(posedge clk) if ({al, ar} !== last_s) begin
    last_s <= {al, ar};
    if (commits > 0) $fwrite(faud, "%0d %0d %0d\n", commits - 1, al, ar);
end

integer fo, n;
initial begin
    if ($value$plusargs("IL=%d", il_arg)) ;
    if ($value$plusargs("NREC=%d", nrec)) ;
    if ($value$plusargs("LATE=%d", nlate)) ;
    $readmemh("mem.hex", mem);
    if (nlate > 0) $readmemh("late.hex", late);
    fcap = $fopen("video.txt", "w"); faud = $fopen("audio.txt", "w");
    if (il_arg != 0) force core.status[6] = 1'b1;
    mem[1] = 64'd0; mem[2] = 64'd0;
    repeat (50) @(posedge clk);
    reset = 0;
    mem[0] = {32'd77, 32'h33515459};               // the run command (low half) and the session (high half)
    while (commits < nrec + nlate) @(posedge clk);
    repeat (4 * 525 * 2544) @(posedge clk);        // keep going: the last record is shown for a while
    $fwrite(faud, "end\n"); $fclose(faud); $fclose(fcap);
    fo = $fopen("core_out.txt", "w");
    $fwrite(fo, "commits %0d ack %0d session %0d played %0d ident %h reads %0d writes %0d bursts %0d error %0d produced %0d\n",
            commits, mem[2][31:0], mem[2][63:32], mem[3][31:0], mem[4][31:0], reads, writes, bursts, core.movie_error, produced);
    $fclose(fo);
    $display("DONE commits=%0d", commits);
    $finish;
end
initial begin #2000000000; $fatal(1, "watchdog"); end
endmodule
