// SPDX-License-Identifier: GPL-2.0-or-later
// One small simple-dual-port RAM (one write port, one registered read port): 64 x 64 bits (one DDR beat per word).
// No reset on the ports, so it maps to an M10K block.
module linebuf64 (
    input wire clk,
    input wire we,
    input wire [5:0] wa,
    input wire [63:0] wd,
    input wire [5:0] ra,
    output reg [63:0] rq
);
    (* ramstyle = "M10K" *) reg [63:0] mem [0:63];
    always @(posedge clk) begin
        if (we) mem[wa] <= wd;
        rq <= mem[ra];
    end
endmodule
