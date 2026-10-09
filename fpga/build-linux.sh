#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
compiler="${QUARTUS_SH:-quartus_sh}"
if ! command -v "$compiler" >/dev/null 2>&1; then
  echo 'Quartus is missing. Set QUARTUS_SH to the full path of Quartus 17.0.x quartus_sh.' >&2
  exit 1
fi
"$compiler" --version > build.log 2>&1
if ! grep -q 'Version 17.0' build.log; then
  echo 'This project targets Quartus 17.0.x. See build.log.' >&2
  exit 1
fi
"$compiler" --flow compile YouTubeCRT 2>&1 | tee -a build.log
test -s output_files/YouTubeCRT.rbf
"$compiler" -t check-timing.tcl 2>&1 | tee timing-check.txt
echo 'Compiled and reported-slack check passed: output_files/YouTubeCRT.rbf'
echo 'Review build.log and output_files/YouTubeCRT.sta.summary before hardware testing.'
