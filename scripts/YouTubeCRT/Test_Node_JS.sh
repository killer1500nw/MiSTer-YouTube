#!/bin/bash
# YouTube CRT: optional experiment. Can Node.js solve YouTube's "security check" faster than QuickJS?
#
# Some videos (children's TV, age-restricted) need a JavaScript challenge. QuickJS, the small interpreter
# the player uses, takes 3-5 minutes for it on a MiSTer. Node.js compiles the JavaScript and should do
# it in seconds, if it runs on the MiSTer's Linux at all. This script finds out.
#
# What it does (nothing outside /media/fat/YouTubeNative is touched, and the player is not changed):
#   1. downloads Node.js v22 for 32-bit ARM from nodejs.org (about 49 MB, checksum verified; yt-dlp needs 22+)
#   2. unpacks just the "node" program into /media/fat/YouTubeNative/node-test
#   3. checks that it starts, then times one yt-dlp lookup of a security-check video using it
# The result is shown and saved to /media/fat/YouTubeNative/YouTube-node-test.txt: send that file back.
#
# Run it from the MAIN MiSTer menu (no core loaded), so the whole CPU is free: Scripts > YouTubeCRT >
# Test_Node_JS. It needs internet and takes a few minutes. To remove everything afterwards:
#   rm -rf /media/fat/YouTubeNative/node-test
set -u
RT=/media/fat/YouTubeNative
DIR=$RT/node-test
REPORT=$RT/YouTube-node-test.txt
BASE="$(cd -- "$(dirname -- "$0")" && pwd)"
VID="${1:-eqVnhWeYq94}"            # a Power Rangers episode that needs the security check
NODE_VER=v22.23.3
NODE_SHA=8b99058ec6f4e372b9887f8ecb577a292adb132d59e5104356428a758fdb126c
NODE_URL=https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-armv7l.tar.gz

mkdir -p "$DIR"
: > "$REPORT"
say() { echo "$@"; echo "$@" >> "$REPORT"; }
finish() {
  say ""
  say "Report saved to $REPORT (send this file back)."
  say "To remove the test files: rm -rf $DIR"
  if [ -t 0 ]; then read -r -p "Press Enter to return to MiSTer." _ || true; fi
  exit "${1:-0}"
}

say "YouTube CRT Node.js test, $(date '+%Y-%m-%d %H:%M:%S')"
say "--- this MiSTer ---"
say "kernel: $(uname -a)"
say "libc:   $( (ldd --version 2>&1 || true) | head -1)"
say "memory: $(free -m 2>/dev/null | sed -n 2p)"
say "disk:   $(df -h /media/fat 2>/dev/null | tail -1)"

command -v python3 >/dev/null 2>&1 || { say "ERROR: python3 not found."; finish 1; }
YTDLP="$(ls -d "$RT"/resolver-*-qjs-*/bundle/yt-dlp_linux_armv7l 2>/dev/null | tail -1)"
[ -x "$YTDLP" ] || { say "ERROR: yt-dlp not found under $RT. Run Install_YouTube_CRT first."; finish 1; }
say "yt-dlp: $YTDLP"

# ---------------------------------------------------------------- 1. download
TARBALL="$DIR/node.tar.gz"
if [ ! -x "$DIR/node" ]; then
  say ""
  say "--- 1. downloading Node.js $NODE_VER (about 49 MB) ---"
  # The same way the installer downloads yt-dlp: Python with the certificate bundle shipped next to this script
  # (the MiSTer's own curl may not trust nodejs.org). Errors are shown, not hidden.
  python3 - "$NODE_URL" "$TARBALL" "$BASE/cacert.pem" <<'PY' 2>&1 | while read -r l; do say "$l"; done
import ssl, sys, urllib.request
url, out, ca = sys.argv[1:4]
try:
    ctx = ssl.create_default_context(cafile=ca)
    done, step = 0, 0
    with urllib.request.urlopen(url, timeout=60, context=ctx) as r, open(out, 'wb') as f:
        total = int(r.headers.get('Content-Length') or 0)
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if done // (8 << 20) > step:
                step = done // (8 << 20)
                print('  downloaded %d of %d MB' % (done >> 20, total >> 20), flush=True)
except Exception as exc:
    print('download error: %s: %s' % (type(exc).__name__, exc))
PY
  [ -s "$TARBALL" ] || { say "ERROR: could not download Node.js. Check the internet connection and the MiSTer's clock, then run again."; finish 1; }
  GOT="$(python3 - "$TARBALL" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for chunk in iter(lambda: f.read(1 << 20), b''):
        h.update(chunk)
print(h.hexdigest())
PY
)"
  if [ "$GOT" != "$NODE_SHA" ]; then
    say "ERROR: the download's checksum is wrong (got $GOT). Deleting it, nothing was run."
    rm -f "$TARBALL"
    finish 1
  fi
  say "download OK, checksum verified"

  # ------------------------------------------------------------ 2. unpack just bin/node
  say "--- 2. unpacking the node program ---"
  python3 - "$TARBALL" "$DIR/node" <<'PY' || { say "ERROR: could not unpack."; rm -f "$TARBALL"; finish 1; }
import sys, tarfile
src, out = sys.argv[1], sys.argv[2]
with tarfile.open(src, 'r:gz') as t:
    for m in t:
        if m.name.endswith('/bin/node') and m.isfile():
            with t.extractfile(m) as f, open(out, 'wb') as o:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    o.write(chunk)
            break
    else:
        raise SystemExit('bin/node not found in the archive')
PY
  chmod +x "$DIR/node"
  rm -f "$TARBALL"
fi

# ---------------------------------------------------------------- 3. does it start?
say ""
say "--- 3. does Node start on this MiSTer? ---"
NODEOUT="$("$DIR/node" --version 2>&1)"
if [ $? -ne 0 ] || ! echo "$NODEOUT" | grep -q '^v'; then
  say "Node did NOT start: $NODEOUT"
  say "That means this MiSTer's Linux is too old for Node.js. Nothing was changed; QuickJS stays in use."
  finish 2
fi
say "node $NODEOUT starts fine"
START=$(date +%s)
"$DIR/node" -e 'let s=0;for(let i=0;i<3e7;i++)s+=i%7;console.log("3e7 loop ok",s)' 2>&1 | while read -r l; do say "$l"; done
say "(small speed check took $(( $(date +%s) - START )) s)"

# ---------------------------------------------------------------- 4. the real test
say ""
say "--- 4. one yt-dlp lookup of $VID using Node (QuickJS needed 3-5 minutes for videos like this) ---"
LOG="$DIR/yt-dlp-node.log"
export SSL_CERT_FILE="$BASE/cacert.pem" REQUESTS_CA_BUNDLE="$BASE/cacert.pem" CURL_CA_BUNDLE="$BASE/cacert.pem"
T0=$(date +%s)
( "$YTDLP" --ignore-config --no-playlist --no-cache-dir --skip-download --verbose \
    --print '%(id)s | %(title).50s | %(duration)ss' \
    --extractor-args 'youtube:skip=translated_subs,hls' --force-ipv4 \
    --js-runtimes "node:$DIR/node" --compat-options no-certifi \
    -- "https://www.youtube.com/watch?v=$VID" > "$DIR/yt-dlp-node.out" 2> "$LOG" ) &
PID=$!
LIMIT=420
while kill -0 $PID 2>/dev/null; do
  sleep 2
  NOW=$(( $(date +%s) - T0 ))
  if [ "$NOW" -gt "$LIMIT" ]; then
    kill $PID 2>/dev/null; sleep 1; kill -9 $PID 2>/dev/null
    say "gave up after ${LIMIT} s"
    break
  fi
done
wait $PID 2>/dev/null
CODE=$?
SECS=$(( $(date +%s) - T0 ))
say "yt-dlp finished in $SECS s (exit code $CODE)"
say "result: $(head -1 "$DIR/yt-dlp-node.out" 2>/dev/null)"
say "--- the lines that show which JavaScript engine did the work ---"
grep -i "jsc\|node\|challenge\|ERROR" "$LOG" | cut -c1-200 | tail -12 | while read -r l; do say "$l"; done

say ""
if [ "$CODE" -eq 0 ] && [ -s "$DIR/yt-dlp-node.out" ]; then
  if [ "$SECS" -lt 90 ]; then
    say "RESULT: WORKS AND IS FAST ($SECS s). Node.js can replace QuickJS for these videos."
  else
    say "RESULT: works but took $SECS s. Better than nothing; tell me the number."
  fi
else
  say "RESULT: the lookup did not complete with Node. The yt-dlp log is in $LOG (send it too)."
fi
finish 0
