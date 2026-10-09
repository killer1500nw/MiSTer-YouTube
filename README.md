# MiSTer YouTube CRT Player

Experimental on-device YouTube streaming for MiSTer FPGA, with native CRT
video output and audio. A supplied YouTube URL or video ID is resolved on the
MiSTer; Linux decodes the stream and an FPGA core displays video and plays audio.
No casting server or saved movie file is required. Small bounded RAM buffers
hold media during playback.

## Current checkpoint

- FPGA core: **v0.13-smooth** (Quartus 17.0 project).
- Player: **v0.13.7**, with a URL/ID prompt. Enter selects the built-in test clip.
- 320×240 RGB565 active picture, approximately 30fps content delivery with
  approximately 60Hz 240p CRT scan timing; 48kHz stereo source audio.
- Source selection is H.264, at most 426×240 and 30fps; fast bilinear scaling
  retains aspect ratio and adds black bars as needed.
- Analog/component CRT and HDMI playback tested on the development setup.
  This checkpoint does not implement selectable 480i output.

**Validation:** v0.13.6 streamed a 14m55s talking clip to completion with user
reported good lip sync. All 179 sampled FPGA queue readings were 7–8 frames;
26,843 records completed in 894.63 seconds. v0.13.7 preserves the playback
functions and adds URL selection; broad compatibility with other videos has
not yet been established. A later user-entered ID was rejected by YouTube as
unavailable, before decoding began.

## Install on the existing development setup

This is a developer preview, not a complete fresh-SD-card installer. See
[Runtime requirements](docs/RUNTIME.md) for the external tools it expects.

1. Copy the release core `YouTubeCRT_v0.13-smooth.rbf` to `/media/fat/_Other/`.
2. Copy the whole `scripts/YouTubeURL` folder to `/media/fat/Scripts/YouTubeURL/`.
   Keep all files together. Stop any previous stream before starting another.
3. From the **main MiSTer menu**, open Scripts → YouTubeURL → Start_YouTube_URL.
4. Enter a YouTube URL or 11-character ID. Blank input uses the known test clip;
   Q cancels. Press Enter again to return to the main menu.
5. Load v0.13-smooth within two minutes. Set **Display = Loaded frame**.
6. Set **RAM streaming On for 5 seconds, Off for 2 seconds, then On**.
7. Playback stops at the end. To stop early, return to the main menu first,
   then run Stop_YouTube_URL if necessary. Scripts are not accessible inside
   the core on the development setup. Exiting early may log a heartbeat error.

Keep your working CRT configuration, including `vga_scaler=0`; this package
contains no MiSTer.ini replacement. Other analog configurations are unverified.

Reports use a unique name under `/media/fat/YouTubeNative/`:
`YouTube-url-13-7-XXXXXXXX.txt`. Check the latest report if playback does not start.
Reports can contain video titles and IDs; review before sharing.

## Input examples

```text
https://www.youtube.com/watch?v=M7kB0lis3xg
https://youtu.be/M7kB0lis3xg
M7kB0lis3xg
```

Sharing and timestamp parameters are discarded; playback starts at the beginning.
Channels, playlist-only URLs and live broadcasts are unsupported. Videos need an
available compatible low-resolution H.264 stream. There is no title search,
account login, pause, seek or in-core browsing UI in this checkpoint. YouTube
access failures (including unavailable videos and HTTP 403) are possible.

## Source and development

- `fpga/`: complete v0.13-smooth source, framework, build scripts and tests.
- `scripts/YouTubeURL/`: current v0.13.7 player, native copy helper source/binary,
  memory checks and certificate bundle.
- `tests/`: host pipeline tests for the current player.
- [Build notes](docs/BUILD.md), [project checkpoint](docs/CHECKPOINT.md),
  [release notes](docs/RELEASE.md), [credits](THIRD_PARTY_NOTICES.md).

The older scripts inside `fpga/MiSTer-Scripts` are retained for the original
FPGA test suite and provenance; install the current `scripts/YouTubeURL` folder.

## Licence

The existing GPL version 2 licence is retained in LICENSE; original upstream
headers and notices remain in place. Core additions were identified as
GPL-2.0-or-later in the original source package. See THIRD_PARTY_NOTICES.md for
framework, certificate and external-runtime attribution. This is an independent
experimental project, not an official YouTube or MiSTer release.
