#!/bin/bash
# Install the optional MiSTer H264 core (the FPGA decodes the video: up to 720p, the ARM almost idle).
#
# The core is the work of owenb321 (https://github.com/owenb321/MiSTer_h264_decoder, GPL-3.0-or-later). It is not part of the YouTube CRT
# player and is not in its release: this script downloads that one file (about 4.4 MB) from the author's release page and checks it
# against the checksum published for it, then adds "Other > YouTube (H264)". Run it once; run it again to repair.
set -u
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
echo "Install the H264 core (by owenb321, GPL-3.0)"
echo "  from: https://github.com/owenb321/MiSTer_h264_decoder/releases/tag/v1.1.0"
echo "  to:   /media/fat/_Other/  (one file, about 4.4 MB; checked against its published SHA-256)"
if [ "${1:-}" != "-y" ] && [ -t 0 ]; then
  read -r -p "Continue? [y/N] " ANSWER || ANSWER=n
  case "$ANSWER" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0;; esac
fi
if python3 "$BASE/ytcrt_setup.py" --h264-core; then
  echo
  echo "Done. Open the Other menu and load YouTube (H264)."
else
  echo
  echo "The H264 core could not be installed. Check the MiSTer's internet connection and run this script again."
fi
[ -t 0 ] && read -r -p "Press Enter to return to MiSTer." _ || true
exit 0
