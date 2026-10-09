// SPDX-License-Identifier: GPL-2.0-or-later
// Two RGB565 frames in embedded RAM. Byte-wide HPS file index 1, little endian.
// Index 2 accepts 160x120 RGB565LE movie frames (38400 bytes).
// Exact length and sequential addresses are required. Only swap in blanking.
module frame_store (
    input wire clk, reset,
    input wire download,
    input wire [15:0] file_index,
    input wire wr,
    input wire [26:0] addr,
    input wire [7:0] data,
    output wire wait_req,
    input wire swap_boundary,
    input wire [16:0] read_addr,
    output wire [15:0] pixel,
    output reg valid=0,
    output reg load_error=0
);
    (* ramstyle = "M10K" *) reg [15:0] bank0 [0:76799];
    (* ramstyle = "M10K" *) reg [15:0] bank1 [0:76799];
    reg [15:0] q0, q1;
    reg front=0, write_bank=1, pending=0;
    reg old_download=0, active=0, bad=0;
    reg [17:0] expected=0;
    reg [7:0] lo=0;
    wire selected = (file_index == 16'd1 || file_index == 16'd2);
    reg [17:0] frame_bytes=153600;
    wire byte_ok = download && active && selected && wr && !bad &&
                   !pending && !reset && addr == {9'd0,expected} && expected < frame_bytes;
    assign wait_req = pending;
    assign pixel = front ? q1 : q0;

    // Read and write ports kept free of reset logic to infer FPGA block RAM.
    always @(posedge clk) begin
        q0 <= bank0[read_addr];
        q1 <= bank1[read_addr];
        if(byte_ok && addr[0]) begin
            if(write_bank) bank1[addr[17:1]] <= {data,lo};
            else           bank0[addr[17:1]] <= {data,lo};
        end
    end

    always @(posedge clk) begin
        old_download <= download;
        if(pending && swap_boundary) begin
            front <= write_bank;
            valid <= 1;
            pending <= 0;
        end
        if(download && !old_download) begin
            active <= selected;
            frame_bytes <= file_index==2 ? 18'd38400 : 18'd153600;
            expected <= 0;
            bad <= 0;
            if(selected) load_error <= 0;
        end
        if(download && active && wr) begin
            if(!byte_ok) bad <= 1;
            else begin
                expected <= expected + 1'b1;
                if(!addr[0]) lo <= data;
                // Capture the writable bank after any previous pending swap.
                if(addr == 0) write_bank <= ~front;
            end
        end
        if(!download && old_download && active) begin
            active <= 0;
            if(!bad && expected == frame_bytes) pending <= 1;
            else load_error <= 1;
        end
        if(reset) begin
            valid <= 0; pending <= 0; active <= 0;
            expected <= 0; bad <= 0; load_error <= 0;
            front <= 0; write_bank <= 1;
        end
    end
endmodule
