# Runtime requirements

The player currently expects these existing files on MiSTer:

```text
/media/fat/YouTubeNative/resolver-2026.08.19-qjs-0.17.0/bundle/yt-dlp_linux_armv7l
/media/fat/YouTubeNative/resolver-2026.08.19-qjs-0.17.0/qjs
/media/fat/YouTubeNative/ffmpeg-7.0.2-armhf-static/ffmpeg
```

Development hardware: DE10-Nano ARM Cortex-A9, ARMv7 hard-float Linux,
Python 3.9.6 and glibc 2.31. Root permissions are needed for /dev/mem.
The player checks the Linux/FPGA memory layout before mapping the reserved area.
Do not remove those checks or substitute a different core/protocol.

External tools are not bundled in this repository or its ready-to-copy player
ZIP. On the existing development machine leave the installed runtime in place.
For a new installation, obtain suitable ARMv7 builds from their upstream projects
and install them at the exact paths above. These are the tested build identities,
not a claim that they are current releases. A verified fresh-install workflow is
still a follow-up task; do not treat this package as a one-step installer.

Upstream projects:
- https://github.com/yt-dlp/yt-dlp
- https://bellard.org/quickjs/
- https://ffmpeg.org/

The CA bundle and libyt_sender.so are supplied beside url_player.py. The helper
is specific to ABI 130 and this FPGA mailbox layout. No pip packages are needed
for normal playback. URL availability depends on the resolver and YouTube.
