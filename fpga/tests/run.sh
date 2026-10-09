#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python3 tests/test_player.py
iverilog -g2012 -s tb_crt240 -o "$work/timing.vvp" rtl/crt240.sv tests/tb_crt240.sv
vvp "$work/timing.vvp"
iverilog -g2012 -s tb_frames -o "$work/frames.vvp" rtl/frame_store.sv tests/tb_frames.sv
vvp "$work/frames.vvp"
for latency in DELAYED ZERO_LATENCY; do
  iverilog -g2012 -D"$latency" -s tb_stream -o "$work/stream.vvp" rtl/ddr_stream.sv rtl/frame_store.sv rtl/movie_audio.sv tests/tb_stream.sv
  vvp "$work/stream.vvp"
done
iverilog -g2012 -DREAL_EMU -s tb_stream -o "$work/real.vvp" YouTubeCRT.sv rtl/ddr_stream.sv rtl/crt240.sv rtl/frame_store.sv rtl/movie_audio.sv tests/sim_stubs.sv tests/tb_stream.sv
vvp "$work/real.vvp"
for size in FULL_FRAME STREAM_FRAME; do
  iverilog -g2012 -D"$size" -s tb_scanout -o "$work/scanout.vvp" YouTubeCRT.sv rtl/ddr_stream.sv rtl/crt240.sv rtl/frame_store.sv rtl/movie_audio.sv tests/sim_stubs.sv tests/tb_scanout.sv
  vvp "$work/scanout.vvp"
done
