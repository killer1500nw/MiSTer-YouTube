#!/bin/bash
# Shows where YouTube CRT files are and what is running. Photograph the output, or run over SSH.
echo "=== YouTube CRT: where is everything? (files on card: version $(cat "$(dirname "$0")/VERSION.txt" 2>/dev/null)) ==="
echo "Scripts folder:"; ls -la /media/fat/Scripts/YouTubeCRT 2>&1 | head -20
echo "--- YouTubeNative folder ---"; ls -la /media/fat/YouTubeNative 2>&1 | head -20
echo "--- boot entry ---"; grep -n -A1 "YouTube CRT" /media/fat/linux/user-startup.sh 2>&1
echo "--- running ---"; ps | grep -v grep | grep -i "youtube"
echo "--- log files anywhere on the card ---"; find /media/fat -maxdepth 3 -iname "*crt*log*" -o -maxdepth 3 -iname "*crt-daemon*" 2>/dev/null
sync
if [ -t 0 ]; then read -r -p "Press Enter to return to MiSTer." _ || true; fi
