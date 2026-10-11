#!/bin/bash
# Remove the boot entry and stop the player.
echo "YouTube CRT uninstall (version $(cat "$(dirname "$0")/VERSION.txt" 2>/dev/null))"
STARTUP=/media/fat/linux/user-startup.sh
if [ -f "$STARTUP" ]; then
  sed -i '/# YouTube CRT player/,+1d' "$STARTUP"
  echo "Removed from $STARTUP"
fi
PIDS="$(ps | grep -v grep | grep "youtube_crt.py --daemon" | awk '{print $1}')"
if [ -n "$PIDS" ]; then kill $PIDS && echo "Player stopped."; else echo "Player was not running."; fi
if [ -t 0 ]; then read -r -p "Press Enter to return to MiSTer." _ || true; fi
