// SPDX-License-Identifier: GPL-2.0-or-later
// One small simple-dual-port RAM (one write port, one registered read port): 128 x 32 bits.
// No reset on the ports, so it maps to an M10K block.
module linebuf (
    input wire clk,
    input wire we,
    input wire [6:0] wa,
    input wire [31:0] wd,
    input wire [6:0] ra,
    output reg [31:0] rq
);
    (* ramstyle = "M10K" *) reg [31:0] mem [0:127];
    always @(posedge clk) begin
        if (we) mem[wa] <= wd;
        rq <= mem[ra];
    end
endmodule
