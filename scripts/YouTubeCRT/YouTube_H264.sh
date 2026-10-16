#!/bin/bash
# Load the H264 core (the FPGA decodes the video). The YouTube CRT player is already running in the background (it starts at boot) and
# attaches to the core by itself: the YouTube menu appears over the core's picture.
# The same thing as the "YouTube (H264)" entry in the Other menu, for when that entry is not there.
set -u
if ! ls /media/fat/_Other/H264_*.rbf >/dev/null 2>&1; then
  echo "The H264 core is not in /media/fat/_Other/ (H264_*.rbf). Install it from github.com/owenb321/MiSTer_h264_decoder first."
  [ -t 0 ] && read -r -p "Press Enter to return to MiSTer." _
  exit 1
fi
printf '<mistergamedescription>\n  <rbf>_Other/H264</rbf>\n</mistergamedescription>\n' > /tmp/youtube_h264.mgl
echo "load_core /tmp/youtube_h264.mgl" > /dev/MiSTer_cmd
exit 0
