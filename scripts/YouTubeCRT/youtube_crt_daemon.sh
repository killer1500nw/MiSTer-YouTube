#!/bin/bash
# Started at boot from /media/fat/linux/user-startup.sh. Runs the player daemon in the background.
# Log: /media/fat/YouTubeNative/YouTube-crt-log.txt (appended; the previous one is kept as .old past 1 MB).
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
LOG=/media/fat/YouTubeNative/YouTube-crt-log.txt
mkdir -p /media/fat/YouTubeNative
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ]; then mv -f "$LOG" "$LOG.old"; fi
echo "" >> "$LOG"
echo "launcher: files on card are version $(cat "$BASE/VERSION.txt" 2>/dev/null)" >> "$LOG"
exec python3 "$BASE/youtube_crt.py" --daemon >> "$LOG" 2>&1
