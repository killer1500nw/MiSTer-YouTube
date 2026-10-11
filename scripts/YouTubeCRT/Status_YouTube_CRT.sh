#!/bin/bash
# Prints everything needed to see why the GUI or playback is not working. Photograph the output.
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
LOG=/media/fat/YouTubeNative/YouTube-crt-log.txt
RT=/media/fat/YouTubeNative
chk() { if [ -e "$1" ]; then echo "  OK      $2"; else echo "  MISSING $2"; fi; }
echo "=== YouTube CRT status (files on card: version $(cat "$BASE/VERSION.txt" 2>/dev/null)) ==="
echo "player version : $(grep -o "__version__ = '[^']*'" "$BASE/ytcrt/__init__.py" 2>/dev/null)"
echo -n "core file      : "; ls /media/fat/_Other/ 2>/dev/null | grep -i youtubecrt | tr '\n' ' '; echo
echo -n "python3        : "; (python3 --version 2>&1 || echo MISSING)
echo -n "player process : "; if ps | grep -v grep | grep -q "youtube_crt.py --daemon"; then echo RUNNING; else echo "NOT running"; fi
echo "tools under $RT:"
chk "$RT" "YouTubeNative folder"
YT="$(ls -d $RT/resolver-*-qjs-*/bundle/yt-dlp_linux_armv7l 2>/dev/null | tail -1)"
QJ="$(ls -d $RT/resolver-*-qjs-*/qjs 2>/dev/null | tail -1)"
FF="$(ls -d $RT/ffmpeg-*-armhf-static/ffmpeg 2>/dev/null | tail -1)"
chk "${YT:-/nonexistent}" "yt-dlp ${YT}"
chk "${QJ:-/nonexistent}" "qjs    ${QJ}"
chk "${FF:-/nonexistent}" "ffmpeg ${FF}"
echo "YouTubeNative contains: $(ls $RT 2>/dev/null | tr '\n' ' ')"
if [ -n "$FF" ]; then echo -n "ffmpeg runs    : "; "$FF" -version 2>&1 | head -1; fi
if [ -n "$QJ" ]; then echo -n "qjs runs       : "; echo 'console.log("ok")' > /tmp/q.js; "$QJ" /tmp/q.js 2>&1 | head -1; fi
echo "--- last log lines ---"
tail -n 22 "$LOG" 2>/dev/null || echo "(no log file: the player never started)"
echo "----------------------"
if [ -t 0 ]; then read -r -p "Press Enter to return to MiSTer." _ || true; fi
