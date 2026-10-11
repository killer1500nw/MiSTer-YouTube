#!/bin/bash
# YouTube CRT: one-step install / update for MiSTer.
#
#   1. Put this single file in /media/fat/Scripts/ (or run it over SSH).
#   2. Run it from the MiSTer menu: Scripts > Install_YouTube_CRT.
#   3. Load the YouTubeCRT_v0.14-auto core from _Other. The menu appears.
#
# It downloads the latest release from GitHub (about 1 MB), installs the core and the player,
# downloads the video tools (yt-dlp, QuickJS, ffmpeg) and makes the player start at boot.
# Run it again at any time to update; your lists and settings are kept.
set -u
FAT="${YTCRT_FAT:-/media/fat}"
URL="${YTCRT_URL:-https://github.com/killer1500nw/MiSTer-YouTube/releases/latest/download/YouTubeCRT-installer.zip}"
TMP="${TMPDIR:-/tmp}/ytcrt-install.$$"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

say() { echo "$@"; }
die() {
  echo
  echo "ERROR: $*"
  if [ -t 0 ]; then read -r -p "Press Enter to return to MiSTer." _ || true; fi
  exit 1
}

say "YouTube CRT installer"
command -v python3 >/dev/null 2>&1 || die "python3 was not found. Update your MiSTer Linux (run update_all) and try again."

say "Downloading $URL"
ZIP="$TMP/installer.zip"
if command -v curl >/dev/null 2>&1; then
  curl -fL --retry 3 --connect-timeout 20 -o "$ZIP" "$URL" || rm -f "$ZIP"
elif command -v wget >/dev/null 2>&1; then
  wget -q -T 30 -t 3 -O "$ZIP" "$URL" || rm -f "$ZIP"
fi
if [ ! -s "$ZIP" ]; then
  # Fallback: Python's own downloader.
  python3 - "$URL" "$ZIP" <<'PY' || rm -f "$ZIP"
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2], 'wb') as f:
    f.write(r.read())
PY
fi
[ -s "$ZIP" ] || die "Could not download the installer. Check the MiSTer's internet connection and clock (a wrong date breaks HTTPS), then run this again."

say "Unpacking..."
python3 - "$ZIP" "$FAT" <<'PY' || die "The download was not a valid installer package."
import sys, zipfile
zf, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zf) as z:
    names = z.namelist()
    if not any(n == 'Scripts/YouTubeCRT/Install_YouTube_CRT.sh' for n in names):
        raise SystemExit(1)
    for n in names:                                   # only ever write inside Scripts/YouTubeCRT
        if not n.startswith('Scripts/YouTubeCRT/') or '..' in n.split('/'):
            raise SystemExit('unexpected path in package: ' + n)
    z.extractall(dest)
PY
find "$FAT/Scripts/YouTubeCRT" -name '*.sh' -exec sed -i 's/\r$//' {} + 2>/dev/null
chmod +x "$FAT"/Scripts/YouTubeCRT/*.sh 2>/dev/null

if [ -n "${YTCRT_NO_RUN:-}" ]; then
  say "Unpacked to $FAT/Scripts/YouTubeCRT (not running the installer)."
  exit 0
fi
exec bash "$FAT/Scripts/YouTubeCRT/Install_YouTube_CRT.sh"
