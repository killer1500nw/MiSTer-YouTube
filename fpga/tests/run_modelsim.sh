#!/usr/bin/env bash
# RTL tests for the v0.15 core, for the ModelSim-Altera Starter edition that ships with Quartus Lite. Run from Git Bash
# on Windows. (The v0.14 testbenches live in git history: tag core-v0.14.)
#   1. video timing (240p and 480i)
#   2. yuv_scaler against the Python model: 12 pictures (sizes, crops, colour matrices, dither, 240p, overlays)
#   3. the whole core against a player-like DDR model: mailbox, header parsing, slot reuse, audio, 480i and 240p
set -uo pipefail
cd -- "$(dirname -- "$0")/.."
MS="${MODELSIM_BIN:-/c/intelFPGA_lite/17.0/modelsim_ase/win32aloem}"
export PATH="$MS:$PATH"
F="$(pwd -W 2>/dev/null || pwd)"; W="$(mktemp -d)"; cd "$W"
run() {  # name top files...
  local name=$1 top=$2; shift 2
  vlib "w_$name" >/dev/null
  vlog -sv -quiet -work "w_$name" +incdir+"$F" "$@" >"$name.log" 2>&1
  vsim -c -work "w_$name" -do "run -all; quit -f" "$top" >>"$name.log" 2>&1
  if grep -q "PASS" "$name.log" && ! grep -qiE "fatal|\*\* Error" "$name.log"; then grep PASS "$name.log" | head -1
  else echo "FAIL $name"; tail -5 "$name.log"; fi
}
run timing    tb_crt240    $F/rtl/crt240.sv $F/tests/tb_crt240.sv &
run interlace tb_interlace $F/rtl/crt240.sv $F/tests/tb_interlace.sv &
wait
ROOT="$F/.."
python "$ROOT/tools/run_yuv_sim.py"
python "$ROOT/tools/run_core_sim.py"
