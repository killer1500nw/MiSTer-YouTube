#!/bin/bash
# One-time setup: copy the core, make the YouTube CRT player start at boot, start it now,
# and report what it sees. After this you never run a script again: load the
# YouTubeCRT core from the MiSTer menu (_Other) and the GUI appears.
set -Eeuo pipefail
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
STARTUP=/media/fat/linux/user-startup.sh
MARK="# YouTube CRT player"
LOG=/media/fat/YouTubeNative/YouTube-crt-log.txt
RT=/media/fat/YouTubeNative
VER="$(grep -o "__version__ = '[^']*'" "$BASE/ytcrt/__init__.py" 2>/dev/null | cut -d"'" -f2)"
echo "YouTube CRT installer - files are version ${VER:-unknown}"
# Only the current core is installed (older cores already on the card are left alone; the player still drives them).
for CORE in YouTubeCRT_v0.16-fb.rbf; do
  if [ -f "$BASE/$CORE" ]; then
    mkdir -p /media/fat/_Other
    cp -f "$BASE/$CORE" /media/fat/_Other/"$CORE"
    echo "Core installed: /media/fat/_Other/$CORE"
  elif [ -f /media/fat/_Other/"$CORE" ]; then
    echo "Core already in /media/fat/_Other/: $CORE"
  else
    echo "NOTE: $CORE is not next to this script or in /media/fat/_Other/."
  fi
done
command -v python3 >/dev/null || echo "WARNING: python3 not found; the player cannot run."
echo "Checking the video tools (downloads them if missing; needs internet)..."
if python3 "$BASE/ytcrt_setup.py" --root "$RT"; then
  :
else
  echo "WARNING: could not install the video tools. Menus will work but videos will not."
  echo "         Check the MiSTer's internet connection and run this script again."
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
OLD="$(ps | grep -v grep | grep "youtube_crt.py --daemon" | awk '{print $1}')"
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
echo "Expected: 'Waiting for the YouTubeCRT core to be loaded...'. Now load YouTubeCRT_v0.16-fb from _Other."
if [ -t 0 ]; then read -r -p "Press Enter to return to MiSTer." _ || true; fi
