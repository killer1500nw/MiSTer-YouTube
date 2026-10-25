#!/bin/bash
# One-time setup: install the H264 core, make the YouTube CRT player start at boot, start it now,
# and report what it sees. After this you never run a script again: load Other > YouTube (H264)
# from the MiSTer menu and the GUI appears.
set -Eeuo pipefail
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
STARTUP=/media/fat/linux/user-startup.sh
MARK="# YouTube CRT player"
LOG=/media/fat/YouTubeNative/YouTube-crt-log.txt
RT=/media/fat/YouTubeNative
VER="$(grep -o "__version__ = '[^']*'" "$BASE/ytcrt/__init__.py" 2>/dev/null | cut -d"'" -f2)"
echo "YouTube CRT installer - files are version ${VER:-unknown}"
# The player now runs on the H264 core only (installed below). The old YouTubeCRT cores (v0.14 / v0.15 / v0.16) are removed.
for OLD in YouTubeCRT_v0.14-auto.rbf YouTubeCRT_v0.15-yuv.rbf YouTubeCRT_v0.16-fb.rbf; do
  for DIR in /media/fat/_Other "$BASE"; do
    if [ -f "$DIR/$OLD" ]; then rm -f "$DIR/$OLD" && echo "Removed the old core: $DIR/$OLD"; fi
  done
done
# A menu entry for the H264 core (the FPGA decodes the video): MiSTer lists a .mgl file like a core, in the Other menu. Only when that
# core has been installed (this player does not ship it).
if ls /media/fat/_Other/H264_*.rbf >/dev/null 2>&1; then
  cat > "/media/fat/_Other/YouTube (H264).mgl" <<'MGL'
<mistergamedescription>
  <rbf>_Other/H264</rbf>
</mistergamedescription>
MGL
  echo "Menu entry written: Other > YouTube (H264)"
else
  echo "Optional: for up to 720p video, run Scripts > YouTubeCRT > Install_H264_Core (installs owenb321's H264 core)."
fi
command -v python3 >/dev/null || echo "WARNING: python3 not found; the player cannot run."
echo "Checking the video tools (downloads them if missing; needs internet)..."
if python3 "$BASE/ytcrt_setup.py" --root "$RT"; then
  :
else
  echo "WARNING: could not install the video tools. Menus will work but videos will not."
  echo "         Check the MiSTer's internet connection and run this script again."
fi

# The optional H264 core (the FPGA decodes the video, up to 720p) by owenb321, GPL-3.0: downloaded from the author's release page and checked
# against its published SHA-256 (not bundled). If it fails, the player still works with the ARM decoding; run Install_H264_Core later.
echo "Installing the H264 core by owenb321 (github.com/owenb321/MiSTer_h264_decoder, GPL-3.0)..."
if python3 "$BASE/ytcrt_setup.py" --h264-core; then
  :
else
  echo "NOTE: the H264 core was not installed. The player works without it; run Scripts > YouTubeCRT > Install_H264_Core later."
fi

mkdir -p "$(dirname "$STARTUP")"
touch "$STARTUP"
# Replace any earlier entry (older versions used commands MiSTer does not have).
sed -i "/$MARK/,+1d" "$STARTUP"
{
  echo ""
  echo "$MARK"
  echo "case \"\${1:-start}\" in start) ( bash $BASE/youtube_crt_daemon.sh </dev/null >/dev/null 2>&1 & ) ;; esac"
} >> "$STARTUP"
echo "Boot entry written to $STARTUP"

# Always restart the player so a re-run picks up new files.
OLD="$(ps | grep -v grep | grep "youtube_crt.py --daemon" | awk '{print $1}')" || true
if [ -n "$OLD" ]; then
  kill $OLD 2>/dev/null || true
  sleep 1
  echo "Stopped the old player."
fi
rm -f /tmp/YouTubeCRT.stop
( bash "$BASE/youtube_crt_daemon.sh" </dev/null >/dev/null 2>&1 & )
echo "Player started (version $(grep -o "__version__ = '[^']*'" "$BASE/ytcrt/__init__.py" 2>/dev/null | cut -d"'" -f2))."
sleep 3
sync
echo "--- player log ($LOG) ---"
tail -n 8 "$LOG" 2>/dev/null || echo "(no log yet)"
echo "-------------------------"
echo "Expected: 'Waiting for the YouTubeCRT core or the H264 core to be loaded...'. Now load Other > YouTube (H264)."
if [ -t 0 ]; then read -r -p "Press Enter to return to MiSTer." _ || true; fi
