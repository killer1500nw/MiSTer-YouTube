// SPDX-License-Identifier: GPL-2.0-or-later
// Shares the single DDR port between the stream controller (single-beat mailbox/audio reads and writes) and the
// scaler (burst reads of picture rows).
//
// The scaler holds `c_want` for a whole row fetch and is served as soon as the stream controller has no read in
// flight. Bus ownership never changes while a read is outstanding, so read data can only ever reach the master that
// asked for it. The master that does not own the bus sees `busy` high, which simply makes it wait.
module ddr_arb (
    input  wire        clk, reset,
    // stream controller
    input  wire [28:0] s_addr,
    input  wire [7:0]  s_burst, s_be,
    input  wire        s_rd, s_wr,
    input  wire [63:0] s_din,
    output wire        s_busy, s_valid,
    // scaler
    input  wire        c_want,
    input  wire [28:0] c_addr,
    input  wire [7:0]  c_burst,
    input  wire        c_rd,
    output wire        c_grant, c_busy, c_valid,
    // the DDR port
    output wire [28:0] ddr_addr,
    output wire [7:0]  ddr_burst, ddr_be,
    output wire        ddr_rd, ddr_wr,
    output wire [63:0] ddr_din,
    input  wire        ddr_busy, ddr_valid
);
    reg owner = 0;                    // 0 = stream controller, 1 = scaler
    reg s_out = 0;                    // a stream controller read is in flight (they are single-beat)
    wire s_accept_rd = !owner && s_rd && !ddr_busy;
    // An Avalon command must stay unchanged until it is accepted, so ownership is never handed over while the
    // stream controller has a command asserted that the bus has not taken yet.
    wire s_pending = !owner && (s_rd || s_wr) && ddr_busy;

    always @(posedge clk) begin
        if (s_accept_rd) s_out <= 1;
        else if (!owner && ddr_valid && s_out) s_out <= 0;
        if (!owner) begin
            if (c_want && !s_out && !s_accept_rd && !s_pending) owner <= 1;
        end else begin
            if (!c_want) owner <= 0;
        end
        if (reset) begin owner <= 0; s_out <= 0; end
    end

    assign ddr_addr  = owner ? c_addr  : s_addr;
    assign ddr_burst = owner ? c_burst : s_burst;
    assign ddr_be    = owner ? 8'hff   : s_be;
    assign ddr_rd    = owner ? c_rd    : s_rd;
    assign ddr_wr    = owner ? 1'b0    : s_wr;
    assign ddr_din   = s_din;
    assign s_busy    = owner ? 1'b1 : ddr_busy;
    assign c_busy    = owner ? ddr_busy : 1'b1;
    assign s_valid   = !owner && ddr_valid;
    assign c_valid   = owner && ddr_valid;
    assign c_grant   = owner;
endmodule
