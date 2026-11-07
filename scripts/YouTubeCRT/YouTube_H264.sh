#!/bin/bash
# Load the H264 core (the FPGA decodes the video). The YouTube CRT player is already running in the background (it starts at boot) and
# attaches to the core by itself: the YouTube menu appears over the core's picture.
# The same thing as the "YouTube H264" entry in the Other menu, for when that entry is not there.
set -u
CORE="$(ls /media/fat/_Other/"YouTube H264_"*.rbf /media/fat/_Other/H264_*.rbf 2>/dev/null | head -n 1)"
if [ -z "$CORE" ]; then
  echo "The H264 core is not in /media/fat/_Other/. Run Scripts > Install_YouTube_CRT to install it (owenb321's core, github.com/owenb321/MiSTer_h264_decoder)."
  [ -t 0 ] && read -r -p "Press Enter to return to MiSTer." _
  exit 1
fi
echo "load_core $CORE" > /dev/MiSTer_cmd
exit 0
