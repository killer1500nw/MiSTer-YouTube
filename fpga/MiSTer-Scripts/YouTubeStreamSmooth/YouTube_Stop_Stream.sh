#!/bin/bash
set -Eeuo pipefail
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
python3 "$BASE/player.py" --stop
if [ -t 0 ]; then read -r -p 'Press Enter to return to MiSTer.' _ || true; fi
