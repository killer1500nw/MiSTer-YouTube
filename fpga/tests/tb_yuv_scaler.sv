`timescale 1ns/1ps
// Drives yuv_scaler with the real video timing generator and a DDR model (random wait states, burst latency,
// gaps between beats), then dumps the captured picture. tools/run_yuv_sim.py compares the dump with the Python model.
//   +IL=1     480i (default)   +IL=0   240p
//   +SLOT=n   which slot the picture sits in (default 0)
module tb_yuv_scaler;
localparam LAT = 15;
localparam [28:0] BASE = 29'h06000000;
reg clk = 0, reset = 1;
always #12.5 clk = ~clk;                           // 40 MHz
reg interlace = 1;
integer slot = 0;
wire ce, hs, vs, de, il, fld, nfield, nil;
wire [9:0] px; wire [8:0] py, ny;
crt240 vid (.clk(clk), .reset(reset), .pattern(2'd0), .interlace(interlace), .il_active(il), .field(fld),
            .ce_pix(ce), .hs(hs), .vs(vs), .de(de), .r(), .g(), .b(), .pixel_x(px), .pixel_y(py),
            .next_y(ny), .next_field(nfield), .next_il(nil));

reg [63:0] mem [0:65535];
reg [63:0] ovmem [0:3*16384-1];                    // the three 128 KiB overlay buffers (beats)
reg cfg_load = 0;
wire have_frame, want, rd, busy, valid;
wire [28:0] addr; wire [7:0] burst; wire [63:0] dout;
wire [23:0] rgb;
wire grant = want;
yuv_scaler #(.BASE(BASE)) dut (
    .clk(clk), .reset(reset), .ce_pix(ce), .px(px), .ny(ny), .nfield(nfield), .nil(nil),
    .cfg_load(cfg_load), .cfg_slot(slot[2:0]), .cfg_flags(mem[0][34:32]),
    .cfg_w1(mem[1]), .cfg_w2(mem[2]), .cfg_w3(mem[3]), .cfg_w4(mem[4]), .cfg_w5(mem[5]),
    .have_frame(have_frame), .want(want), .grant(grant), .busy(busy), .valid(valid), .dout(dout),
    .addr(addr), .burst(burst), .rd(rd), .rgb(rgb));

// ---- DDR model
reg [31:0] lfsr = 32'hACE1ACE1;
reg active = 0, valid_r = 0;
reg [63:0] dout_r = 0;
reg [28:0] a_cur = 0; reg [7:0] left = 0; reg [3:0] lat = 0;
wire [28:0] slot_base = BASE + 29'd512 + {10'd0, slot[2:0], 16'd0};
localparam [28:0] OVB = BASE + 29'h80200;
assign busy = (lfsr[1:0] == 2'b00) || active;
assign valid = valid_r;
assign dout = dout_r;
integer bursts = 0, beats = 0;
always @(posedge clk) begin
    lfsr <= {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
    valid_r <= 0;
    if (rd && !busy && !reset) begin
        if (a_cur < 0) $fatal(1, "bad address");
        active <= 1; a_cur <= addr; left <= burst; lat <= 4 + lfsr[5:4];
        bursts = bursts + 1;
        if (!(addr >= slot_base && addr + burst <= slot_base + 29'd65536) &&
            !(addr >= OVB && addr + burst <= OVB + 29'd49152)) $fatal(1, "read outside the slot and overlays: %h", addr);
    end
    if (active) begin
        if (lat != 0) lat <= lat - 1;
        else if (lfsr[8:7] != 0) begin
            valid_r <= 1; dout_r <= (a_cur >= OVB) ? ovmem[a_cur - OVB] : mem[a_cur - slot_base];
            a_cur <= a_cur + 1; left <= left - 1; beats = beats + 1;
            if (left == 1) active <= 0;
        end
    end
end

// ---- capture: samples rgb when the delayed pixel enable says a pixel starts
reg [LAT-1:0] ce_sr = 0;
reg [9:0] px_sr [0:LAT-1];
reg [8:0] py_sr [0:LAT-1];
reg       f_sr  [0:LAT-1];
reg       il_sr [0:LAT-1];
integer k;
always @(posedge clk) begin
    ce_sr <= {ce_sr[LAT-2:0], ce};
    px_sr[0] <= px; py_sr[0] <= py; f_sr[0] <= fld; il_sr[0] <= il;
    for (k = 1; k < LAT; k = k + 1) begin px_sr[k] <= px_sr[k-1]; py_sr[k] <= py_sr[k-1]; f_sr[k] <= f_sr[k-1]; il_sr[k] <= il_sr[k-1]; end
end
reg [23:0] cap [0:480*640-1];
reg        got [0:479];
integer d, x, i, frames = 0, capturing = 0;
reg old_vs = 0;
always @(posedge clk) begin
    old_vs <= vs;
    if (vs && !old_vs) frames <= frames + 1;
    if (ce_sr[LAT-1] && px_sr[LAT-1] < 640 && py_sr[LAT-1] < 240 && capturing) begin
        d = il_sr[LAT-1] ? (py_sr[LAT-1] * 2 + f_sr[LAT-1]) : (py_sr[LAT-1] * 2);
        cap[d * 640 + px_sr[LAT-1]] <= rgb;
        got[d] <= 1;
    end
end
// the analogue output looks at rgb on every clock: it must hold for the whole 3-clock pixel period
reg [23:0] held = 0; reg [1:0] hold_n = 0; integer hold_bad = 0;
always @(posedge clk) begin
    if (ce_sr[LAT-1]) begin held <= rgb; hold_n <= 2; end
    else if (hold_n != 0) begin hold_n <= hold_n - 1; if (capturing && rgb !== held) hold_bad <= hold_bad + 1; end
end

integer f, il_arg, slot_arg;
initial begin
    for (i = 0; i < 480; i = i + 1) got[i] = 0;
    if ($value$plusargs("IL=%d", il_arg)) interlace = il_arg[0];
    if ($value$plusargs("SLOT=%d", slot_arg)) slot = slot_arg;
    $readmemh("slot.hex", mem);
    $readmemh("overlay.hex", ovmem);
    repeat (50) @(posedge clk);
    reset = 0;
    repeat (20) @(posedge clk);
    cfg_load = 1; @(posedge clk); cfg_load = 0;        // the picture is committed
    // let a few frames pass (the first lines need their rows fetched), then capture two whole frames
    while (frames < 4) @(posedge clk);
    capturing = 1;
    while (frames < 7) @(posedge clk);
    capturing = 0;
    repeat (64) @(posedge clk);
    f = $fopen("out.txt", "w");
    for (d = 0; d < 480; d = d + 1) if (got[d]) begin
        $fwrite(f, "%0d", d);
        for (x = 0; x < 640; x = x + 1) $fwrite(f, " %06x", cap[d * 640 + x]);
        $fwrite(f, "\n");
    end
    $fclose(f);
    $display("DONE bursts=%0d beats=%0d have_frame=%0d hold_bad=%0d", bursts, beats, have_frame, hold_bad);
    $finish;
end
initial begin #3000000000; $fatal(1, "watchdog"); end
endmodule
