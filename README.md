# MiSTer YouTube CRT Player

Browse and watch YouTube on a MiSTer FPGA, on a CRT in 240p or 480i, at up to **480p** video quality, with a gamepad or keyboard. Load the
**YouTubeCRT** core and a menu appears on screen: your channels, playlists and videos, with thumbnails, resume
and a playback bar. There is no account or login, and nothing is saved to the card except small lists and a cache. The video streams to the MiSTer's RAM.
Video is found on the MiSTer itself (yt-dlp and ffmpeg). It now uses owenb321's MiSTer_h264_decoder to decode video on the FPGA instead of the ARM.

## How this code was written

The RTL in this repository was written by an LLM working under human direction. The human contribution was research, compiling, running each build on real hardware, finding what broke, and narrowing it down to a root cause.

This is stated up front because it is a fair thing for anyone evaluating the code to know. It is not an endorsement of the approach — draw your own conclusions.

## What it does

- **Search** YouTube videos and public **playlists** from the menu (on-screen or real keyboard), with paging through the results.
- **Subscriptions**: the newest videos of all your channels together; **Channels**: one channel at a time.
- **Playlists**, **My Videos** (single videos you list), **Watch Later**, **History** with **resume**.
- **Play URL / ID**, **Add Channel via @handle** and **Add to Lists**: type a link or a handle on an on-screen keyboard or a real keyboard.
- **Load more videos…** at the end of every channel and playlist, so you can scroll well past the first 15.
- **Up to 480p video.** The ARM only decodes; the FPGA core scales the picture to the CRT (bilinear, with BT.601/709 colour),
  so a 480p stream plays smoothly on a stock MiSTer. Auto picks a 480p, 360p or 240p stream per video from an estimate of how much
  the ARM can decode in time and steps down by itself if the picture starts to stutter. A clip that is too heavy for the
  **Fast** decoder setting is moved to **Fastest** (and remembered) before the picture is made smaller. Optional **14:9**
  and **Fill** crops for widescreen video on a 4:3 screen.
- **Made for a CRT.** 240p or interlaced 480i output (core menu), with optional analog dither, a 480i flicker filter and a
  composite blur for the soft look of 80s and 90s TV. Nothing is added unless you switch it on.
- A pause/seek bar, a speed test, live quality change (More panel), and a diagnostics screen.

Not supported: live streams and past livestreams (YouTube serves those in segments), 60 fps, and sign-in. Age restricted
videos play but take longer to start.

## Quick start

1. Download [`Install_YouTube_CRT.sh`](https://github.com/killer1500nw/MiSTer-YouTube/releases/latest/download/Install_YouTube_CRT.sh)
   and put that one file in `/media/fat/Scripts/` on the MiSTer's SD card.
2. On the MiSTer, run **Scripts → Install_YouTube_CRT**. The player and the core are inside that one file: it downloads the video tools
   (yt-dlp, QuickJS, ffmpeg and Node.js) and the H264 core by owenb321 (checksum-verified) into `_Other`, and makes the player start at boot. It needs internet
   access and a roughly correct clock, and it takes a couple of minutes.
3. Load **Other → YouTube (H264)**. The menu appears. Nothing is run per session.

Run the same script again at any time to update; your lists and settings are kept. YouTube needs the internet anyway,
so there is no separate offline package.

The player and helper files live in `/media/fat/Scripts/.config/YouTubeCRT/`.
Only small menu launchers remain in `Scripts/YouTubeCRT/`; settings, lists, tools and logs stay in `YouTubeNative/`.
When upgrading an older visible runtime, the installer preserves that folder under
`Scripts/.config/YouTubeCRT-legacy-*/YouTubeCRT/` before creating the menu launchers.

## What you can play: links, IDs, playlists and channels

| You have | You can use | Where it appears |
| --- | --- | --- |
| **A single video** | `https://www.youtube.com/watch?v=jNQXAC9IVRw`, `https://youtu.be/jNQXAC9IVRw`, a `/shorts/` link, or the 11-character ID `jNQXAC9IVRw` | **My Videos** (if you list it) or straight away via **Play URL / ID** |
| **A playlist** | `https://www.youtube.com/playlist?list=UU4QobU6STFB0P71PMvOGN5A`, or just the playlist ID (`PL…`, `UU…`, `OL…`) | **Playlists** |
| **A channel** | the channel ID `UC4QobU6STFB0P71PMvOGN5A`, the handle `@jawed`, or a `youtube.com/@jawed` / `/channel/UC…` link | **Subscriptions** and **Channels** |

(`jNQXAC9IVRw` is "Me at the zoo", the first video ever uploaded to YouTube, by its co-founder Jawed Karim in 2005.
`@jawed` is his channel, and a channel's uploads playlist is its channel ID with `UC` swapped for `UU`.)

Sharing and timestamp parts of a link (`&t=90s`, `?si=…`) are ignored; playback starts at the beginning or at your
saved resume point.

### Pointing the player at them with the text file

Everything you want listed lives in **one text file** on the SD card:

```text
/media/fat/YouTubeNative/ytcrt/subscriptions.txt
```

It is created the first time the player runs. Put **one entry per line, in any mix**; the player works out what each
line is. A name before a `|` is optional, `#` starts a comment, and duplicates are merged:

```text
# channels: an ID, an @handle or a channel link
@jawed
Jawed Karim | UC4QobU6STFB0P71PMvOGN5A

# playlists: a playlist link or ID
https://www.youtube.com/playlist?list=UU4QobU6STFB0P71PMvOGN5A
Uploads from jawed | UU4QobU6STFB0P71PMvOGN5A

# single videos: a link or an 11-character ID
jNQXAC9IVRw
https://youtu.be/jNQXAC9IVRw
Me at the zoo | https://www.youtube.com/watch?v=jNQXAC9IVRw
```

How to use it:

- **Edit the file** on a PC (SD card path `YouTubeNative/ytcrt/subscriptions.txt`) or over SSH, then in the GUI press
  **X** (Tab) on Channels, Playlists or My Videos to refresh.
- **Or skip the file:** Home → **Add Channel via @handle** (just type the handle, for example `@jawed`; quickest on a gamepad), or Home → **Add to Lists**, then type or paste a channel, playlist or video link/ID. The player writes
  the line for you and puts it in the right place. (A `watch?v=…&list=PL…` link is filed as the playlist; for just that video, use its plain `watch?v=` link.)
- **Import your real subscriptions** from a Google Takeout `subscriptions.csv` with `python tools/import_subscriptions.py`.
- A channel or playlist that cannot load is shown with a `!`; open it to see why (for example *not found (check the
  spelling)*). The same reason is in the log, `/media/fat/YouTubeNative/YouTube-crt-log.txt`.

### Scrolling past the first page

A channel starts with its newest ~15 videos (YouTube's own feed stops there) and a playlist with its first 50. Scroll to
the **Load more videos…** row at the end and press A: the next 50 are fetched and added to the list, and you can keep
going until the end. Videos from later pages and from playlists show no upload age. YouTube's feeds are sometimes
unreliable (random errors even for good channels), so the player retries them and falls back to yt-dlp automatically.

## Documentation

- [docs/YOUTUBECRT.md](docs/YOUTUBECRT.md): install, controls, settings, picture quality, crop, troubleshooting.
- [docs/PROTOCOL-v0.14-draft.md](docs/PROTOCOL-v0.14-draft.md): what changed in the FPGA core (auto-start, 480i, input) and why
  60 fps is not possible; [fpga/PROTOCOL.md](fpga/PROTOCOL.md): the DDR mailbox protocol.
- [docs/RUNTIME.md](docs/RUNTIME.md): the external tools the player uses and where they live.
- `release/`: the ready-built core and the packaged installer (see [docs/YOUTUBECRT.md](docs/YOUTUBECRT.md)).

## Source and development

- `fpga/`: the core: Quartus 17.0 project, RTL, framework and simulation tests (`fpga/tests/run_modelsim.sh` or `run.sh`).
- `scripts/YouTubeCRT/`: the player that runs on the MiSTer (Python 3, standard library only) and its installer scripts.
- `tests/`, `tools/`: player tests, a PC simulator that renders every screen to PNG (`tools/ytcrt_sim.py`), and helpers.
- [Build notes](docs/BUILD.md), [project checkpoint](docs/CHECKPOINT.md), [release notes](docs/RELEASE.md),
  [credits](THIRD_PARTY_NOTICES.md).

## The older v0.13 player (YouTubeURL)

The original script-based player and the v0.13-smooth core are still here, unchanged and separate from the above.
`scripts/YouTubeURL` plays **one video at a time** from a URL or ID you type into a script prompt: no menus, no
playlists or channels, and a manual start-up sequence. It needs the v0.13 core (`YouTubeCRT_v0.13-smooth.rbf`), not the
v0.14-auto core.

**Validation of v0.13.6:** it streamed a 14m55s talking clip to completion with user-reported good lip sync. All 179
sampled FPGA queue readings were 7–8 frames; 26,843 records completed in 894.63 seconds. v0.13.7 adds URL selection.

1. Copy `YouTubeCRT_v0.13-smooth.rbf` to `/media/fat/_Other/` and the whole `scripts/YouTubeURL` folder to
   `/media/fat/Scripts/YouTubeURL/`.
2. From the **main MiSTer menu**: Scripts → YouTubeURL → Start_YouTube_URL. Enter a URL or 11-character ID (blank uses
   the known test clip, Q cancels).
3. Load v0.13-smooth within two minutes, set **Display = Loaded frame**, then **RAM streaming On for 5 seconds, Off for
   2 seconds, then On**. Playback stops at the end; `Stop_YouTube_URL` stops it early from the main menu.

Keep your working CRT configuration, including `vga_scaler=0`; nothing here replaces `MiSTer.ini`. Reports go to
`/media/fat/YouTubeNative/YouTube-url-13-7-XXXXXXXX.txt`; they can contain video titles and IDs, so review before sharing.
The older scripts inside `fpga/MiSTer-Scripts` are kept for the original FPGA test suite and provenance.

## Licence

The existing GPL version 2 licence is retained in LICENSE; original upstream
headers and notices remain in place. Core additions were identified as
GPL-2.0-or-later in the original source package. See THIRD_PARTY_NOTICES.md for
framework, certificate and external-runtime attribution. This is an independent
experimental project, not an official YouTube or MiSTer release.
