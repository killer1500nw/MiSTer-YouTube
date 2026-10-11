#!/usr/bin/env bash
# Same RTL tests as run.sh, for the ModelSim-Altera Starter edition that ships
# with Quartus Lite (no Icarus needed). Run from Git Bash on Windows.
set -uo pipefail
cd -- "$(dirname -- "$0")/.."
MS="${MODELSIM_BIN:-/c/intelFPGA_lite/17.0/modelsim_ase/win32aloem}"
export PATH="$MS:$PATH"
F="$(pwd -W 2>/dev/null || pwd)"; W="$(mktemp -d)"; cd "$W"
run() {  # name top defines files...
  local name=$1 top=$2 defs=$3; shift 3
  vlib "w_$name" >/dev/null
  vlog -sv -quiet -work "w_$name" $defs +incdir+"$F" "$@" >"$name.log" 2>&1
  vsim -c -work "w_$name" -do "run -all; quit -f" "$top" >>"$name.log" 2>&1
  if grep -q "PASS" "$name.log" && ! grep -qiE "fatal|\*\* Error" "$name.log"; then grep PASS "$name.log" | head -1
  else echo "FAIL $name"; tail -5 "$name.log"; fi
}
# ModelSim (unlike Icarus/Quartus) rejects use-before-declare in the top level.
# Test against a copy with just those two declarations moved up.
perl "$F/tests/ms_prep.pl" < "$F/YouTubeCRT.sv" > "$W/emu_ms.sv"
TOP="$W/emu_ms.sv"
S="$F/rtl/ddr_stream.sv $F/rtl/frame_store.sv $F/rtl/movie_audio.sv"
run timing    tb_crt240    ""                 $F/rtl/crt240.sv $F/tests/tb_crt240.sv &
run interlace tb_interlace ""                 $F/rtl/crt240.sv $F/tests/tb_interlace.sv &
run frames    tb_frames    ""                 $F/rtl/frame_store.sv $F/tests/tb_frames.sv &
run stream    tb_stream    ""                 $S $F/tests/tb_stream.sv &
run real      tb_stream    +define+REAL_EMU   $TOP $S $F/rtl/crt240.sv $F/tests/sim_stubs.sv $F/tests/tb_stream.sv &
run scan_full tb_scanout   +define+FULL_FRAME   $TOP $S $F/rtl/crt240.sv $F/tests/sim_stubs.sv $F/tests/tb_scanout.sv &
run scan_strm tb_scanout   +define+STREAM_FRAME $TOP $S $F/rtl/crt240.sv $F/tests/sim_stubs.sv $F/tests/tb_scanout.sv &
wait
