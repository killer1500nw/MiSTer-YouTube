# YouTube CRT — on-screen browser and player

A gamepad/keyboard GUI that runs **inside the same streaming session** as the
video: menus are 320×240 frames pushed through the v0.13-smooth DDR mailbox, so
they appear on the CRT (and HDMI) exactly like video does. Playback uses the
validated pipeline from `YouTubeURL` (yt-dlp → ffmpeg → bounded A/V records).

> **Status: not yet run on hardware.** The core compiles and its RTL tests pass in simulation (see below). The menus, store, feed parsing, resume
> logic and the playback loop (OSD, pause, seek, end-of-video) were exercised on
> a PC with `tools/ytcrt_sim.py` and `tests/test_ytcrt.py`. Real RSS feeds and
> `@handle` resolution were checked against YouTube. Not yet verified on a
> DE10-Nano: evdev input while the core is loaded, thumbnails via the armhf
> ffmpeg, seeking with `-ss`, and the speed-test numbers. Treat the first run
> as a test and read the report file if anything misbehaves.

## How this code was written

The RTL in this repository was written by an LLM working under human direction. The human contribution was research, compiling, running each build on real hardware, finding what broke, and narrowing it down to a root cause.

This is stated up front because it is a fair thing for anyone evaluating the code to know. It is not an endorsement of the approach — draw your own conclusions.

## Features

| Feature | Notes |
| --- | --- |
| Subscriptions | Newest videos from all your channels, reverse-chronological (YouTube RSS, no login) |
| Channels | Browse one channel's latest videos (RSS returns about 15 per channel) |
| Watch Later | Local list; **Y** toggles on any video |
| History | Local watch history (last 100) |
| Resume | Position saved every 10 s and on exit. Setting: **Ask** (prompt) or **Always** |
| Thumbnails | 80×45 preview in the detail panel (fetched on demand, cached on the SD card) |
| Playback OSD | Title, progress bar, time; pause/seek; hides after 4 s |
| Speed test | Settings → Speed test: decodes a test stream and calibrates how much picture quality your ARM can sustain |

Not included (yet): search, playlists, live streams, account login, on-screen keyboard.

## Install (once)

**One script.** Download `Install_YouTube_CRT.sh` from the
[latest release](https://github.com/killer1500nw/MiSTer-YouTube/releases/latest), put that single file in
`/media/fat/Scripts/`, and run **Scripts → Install_YouTube_CRT** on the MiSTer. It downloads the installer package
(about 1 MB), installs the core into `_Other`, downloads the ARM builds of yt-dlp, QuickJS, ffmpeg and Node.js
(checksum-verified), adds the boot entry, starts the player and shows its log
(*Waiting for the YouTubeCRT core to be loaded...*). It needs internet access and a roughly correct clock. Run it
again to update. Then load **YouTubeCRT_v0.14-auto** from `_Other`.

**Offline instead.** `MiSTer-YouTube-SD.zip` (release asset; rebuild with `python tools/make_sd_package.py`) holds
the core, the player and the tools (see its `TOOLS.txt`): unzip it to the **root of the SD card** (it merges with your
`_Other`, `Scripts` and `YouTubeNative` folders), then run **Scripts → YouTubeCRT → Install_YouTube_CRT** once.

That is the last script you run. From then on: **load the YouTubeCRT core from the MiSTer menu
and the GUI appears.** The player starts the streaming session itself. There is no RAM-streaming
Off/On step, no two-minute window and nothing to launch. Load another core and the player goes
back to waiting; load YouTubeCRT again and the menu comes back. Leave the core from the normal MiSTer
menu (F12). `Uninstall_YouTube_CRT` removes the boot entry.

By hand instead of the installer, append this to `/media/fat/linux/user-startup.sh`:

```sh
# YouTube CRT player
case "${1:-start}" in start) ( bash /media/fat/Scripts/YouTubeCRT/youtube_crt_daemon.sh </dev/null >/dev/null 2>&1 & ) ;; esac
```

The core's menu options are on new setting bits, so a saved v0.13 config cannot leave streaming off or the test pattern on.

The daemon only *reads* one memory word twice a second while another core is loaded, and stops
writing within half a second if the core's heartbeat stops. Its log is
`/media/fat/YouTubeNative/YouTube-crt-daemon.txt` (overwritten each boot).

## Adding channels, playlists and single videos

Everything lives in **one file**, `/media/fat/YouTubeNative/ytcrt/subscriptions.txt` (created on first run). One entry
per line, **in any mix**; the player works out what each line is:

```text
# channels: an ID, an @handle or a channel link
UC4QobU6STFB0P71PMvOGN5A
Jawed Karim | UC4QobU6STFB0P71PMvOGN5A
@jawed
https://www.youtube.com/@jawed
# playlists: a playlist link or ID
https://www.youtube.com/playlist?list=UU4QobU6STFB0P71PMvOGN5A
Uploads from jawed | UU4QobU6STFB0P71PMvOGN5A
# single videos: a link or an 11-character ID
jNQXAC9IVRw
https://youtu.be/jNQXAC9IVRw
Me at the zoo | https://www.youtube.com/watch?v=jNQXAC9IVRw
```

These examples all point at the same place on purpose: `jNQXAC9IVRw` is "Me at the zoo", the first video ever
uploaded to YouTube (by co-founder Jawed Karim, 2005). `@jawed` is his channel (`UC4QobU6STFB0P71PMvOGN5A`), and a
channel's uploads playlist is its channel ID with `UC` swapped for `UU`. A name before the `|` is optional. Lines starting with `#` are ignored. What each kind gives you:

| Entry | Where it appears |
| --- | --- |
| Channel | **Subscriptions** (newest videos of all channels together) and **Channels** (one at a time) |
| Playlist | **Playlists**: open one to see its videos in playlist order (up to 200) |
| Single video | **My Videos**: a plain list you can play from; titles are looked up automatically unless you give a name |

**In the GUI:** Home → **Add Channel via @handle** (the `@` is already typed; add the handle, for example `@jawed`; the quickest way on a gamepad), or Home → **Add to Lists**, then type or paste a channel, playlist or video link/ID. The player files it in
the right place for you. (A `watch?v=...&list=PL...` link is filed as the playlist. To add just that video, use its plain `watch?v=` link.) Press **X** (Tab) on Channels, Playlists or My Videos to refresh after editing the file.

**Removing an entry:** highlight it on Channels, Playlists or My Videos and press **Y** (Remove), then confirm. The line is deleted from `subscriptions.txt`; your other lines and comments stay. (Handy for a mistyped handle.)

**Slow videos:** some videos need YouTube's JS security check. With Node.js (installed by `Install_YouTube_CRT`, about 49 MB) it takes seconds to a minute; with only QuickJS it takes 3 to 5 minutes on a MiSTer and can time out. The Loading screen says so as soon as it sees it, and remembers it: next time the warning shows at once, and the video's details panel says *slow start*. Once a video has loaded, its details are saved for 3 hours, so replays are quick.

A playlist or channel that does not load is listed with a `!`; open it to see why (for example *not found (check the
spelling)* or *The playlist does not exist*). The same reason is in the log. YouTube's RSS feeds for channels are
unreliable at times (random 404/500 even for good channels), so the player retries them and then asks yt-dlp for the
channel's newest uploads instead. Videos loaded that way, and playlist videos, show no age.

To import your real subscriptions, export them with Google Takeout (YouTube → subscriptions), then on a PC:
`python tools/import_subscriptions.py subscriptions.csv > subscriptions.txt`.

## Search

Home → **Search** (the first entry). Type what to look for with the on-screen keyboard (the space key works here) or a real keyboard, then Start / OK. The results are an ordinary video list: A plays, Y saves to Watch Later, and **Load more videos...** at the end fetches the next 20 (up to 200). A search is one yt-dlp run, so expect roughly 15 to 35 seconds on the MiSTer; B cancels. Starting a video from the results works like any other video, including the slow security check for some of them. Searches are not saved.

## More than the first page

A channel opens with its newest ~15 videos (all YouTube's RSS feed offers) and a playlist with its first 50. At the end
of the list is a **Load more videos...** row: press A and the next 50 are fetched with yt-dlp and added, as often as you
like until the list ends or reaches 500 videos. Videos from later pages show no upload age and are left out of the merged **Subscriptions**
feed (which stays the dated newest ones). Refreshing (X) goes back to the first page.

## Playing a link or ID

Home → **Play URL / ID**. Type a YouTube link (`watch?v=`, `youtu.be/`, `shorts/`) or an 11-character video ID.
Use the on-screen keyboard (d-pad moves, **A** types, **X** deletes, **Y** Shift, **Start** plays, **B** cancels) or
just type on a real keyboard (**Enter** plays, **Backspace** deletes, **Esc** cancels). Channel and playlist links are
refused with a message. A typed video gets its title from YouTube once it starts, and shows up in History.

## Controls

| | Gamepad | Keyboard |
| --- | --- | --- |
| Move | D-pad / left stick | Arrow keys |
| Select / play-pause | A | Enter, Space |
| Back / stop | B | Esc, Backspace |
| Refresh | X | Tab |
| Watch Later on/off | Y | W |
| Page up / down | L / R | PgUp / PgDn |
| Home | Start | Q |
| Playback: seek ±10 s (quick taps add up, then one seek) | Left / Right | Left / Right |
| Playback: seek ±60 s | L / R | PgUp / PgDn |
| Playback: More panel (Crop, Watch Later) | X | Tab |

## Video crop

Most YouTube video is 16:9 but a CRT is 4:3. **Settings → Video crop** sets how each video starts; during
playback press **X** for the **More** panel and change **Crop** for that video only (it restarts the picture at
the current position, so allow a few seconds).

| Crop | What you see |
| --- | --- |
| **Off** | The whole picture with black bars top and bottom (the original behaviour) |
| **14:9** | Trims the sides of widescreen video to 14:9 and leaves thin black bars |
| **Fill** | Trims widescreen to 4:3 so it fills the screen. This also removes the baked-in side bars from 4:3 shows stored in widescreen files |

Crops are centred and never touch a picture that is already narrower than the target (4:3 video, vertical shorts).

Input comes **through the core**, not Linux: MiSTer's main program owns your controller and keyboard and
sends them to the loaded core, which publishes them to the player. That is why a script alone could never read
them. The core declares buttons `A, B, X, Y, L, R, Select, Start`. The first time MiSTer sees a controller it may ask you
to map its buttons (F12 → *Define joystick buttons*); map them as A, B, X, Y, L, R, Select, Start. Keyboard
arrows, Enter, Space, Esc, Backspace, Tab, PgUp/PgDn, W and Q work too.

## Output modes and performance

The core menu (F12) is deliberately short: **Output: 240p / 480i** (default 240p), **480i field order** and **Reset**.
Everything else (streaming, test pattern, test tone, loading a frame) used to be there for development and is gone.
In 480i the same 320×240 picture is sent as 525-line interlaced video (262/263-line fields at
59.94 Hz, the same 15.7 kHz line rate as 240p). It adds compatibility, not detail. The player needs no setting
for it: only the core's Output option changes the video signal. 60 fps is not offered: no core supports 60 fps
records, and the ARM cannot decode 60 fps H.264 at useful sizes anyway (see
[PROTOCOL-v0.14-draft.md](PROTOCOL-v0.14-draft.md)).

Settings → **Decoder speed** (`Normal` / `Fast` / `Fastest`) passes
`-skip_loop_filter noref|all -flags2 +fast` to ffmpeg's H.264 decoder: less
CPU per frame for a slightly softer picture. Run **Speed test** after changing
it; **≥ 1.15×** means smooth, below **1.0×** means that setting cannot hold
real time.

## Picture quality: Stream quality

The screen is 320×240, but YouTube's own **240p stream is only about 210-240 kbps**, so it arrives already blocky.
Scaling, dithering and deblocking filters were compared on real frames and change very little on a CRT. A bigger
source **downscaled** to 320×240 is what actually looks cleaner, but it costs the ARM much more to decode.

What the ARM can decode in real time depends on the stream: how many **pixels**, how many **kbps** and how many
**frames per second**. Auto estimates that load for every stream a video offers and picks the largest picture
predicted to need at most 87% of the ARM:

* A calm, low-bitrate upload (a 90s sitcom, an old movie) is cheap, so it gets **360p** (or 480×360 for 4:3 shows).
* Busy footage (game capture, sports) is expensive and stays at **240p**.
* A **24 or 25 fps** source has 20% fewer frames than 30 fps, so it is estimated 20% cheaper.
* 480p is chosen only when a stream is light enough, which needs a faster ARM than a stock MiSTer.

Settings → **Stream quality**:

| Setting | Behaviour |
| --- | --- |
| **Auto** (default) | Picks per video by estimated load. If the picture still starves (an empty queue for more than ~2.5 s in 12) it steps down to the next lighter stream from the same spot, without asking YouTube again, and makes the estimate 25% more pessimistic. After 90 s of clean playback of a demanding stream it trusts the estimate 3% more |
| **240p / 360p / 480p** | Always the largest stream up to that size, whatever it costs, with no fall-back |

**Speed test** decodes a reference stream with both cheap decoder settings (Fast and Fastest), shows each result in
"x real time", and calibrates the estimate to *your* MiSTer. It also records which decoder setting larger pictures use.
The playback bar shows the size in use (`240p`, `360p`...) next to the time, and `YouTube-crt-log.txt` records every
choice with its estimated load and any step-down.

**Why there is no frame-rate or audio option.** Both were measured on real clips. Lowering the frame rate saves almost
no decoding, because the decoder has to process every frame to keep the picture correct (the scaler is a tiny part of
the work); only the decoder's own skip modes help, by 10-15%. Audio decodes at over 1,000x real time, so mono or a lower
sample rate changes nothing. The settings that do help are **Decoder speed** and the **Stream quality** choice above.

YouTube sometimes refuses a stream (HTTP 403) at random. The player retries up to three times, then asks YouTube
for fresh stream URLs once, before showing an error.

## Settings

Every entry in Settings does something:

| Setting | What it does |
| --- | --- |
| Stream quality | Auto / 240p / 360p / 480p: how big a YouTube stream to decode (see *Picture quality* above) |
| Video crop | Off / 14:9 / Fill: how widescreen video is trimmed for the 4:3 screen |
| Resume playback | Ask or Always resume where you stopped |
| Show Shorts | Include YouTube Shorts in channel lists |
| Thumbnails | The preview picture in the detail panel |
| Decoder speed | Normal / Fast / Fastest for small streams; larger streams use what the Speed test found |
| Speed test | Measures your MiSTer and calibrates Auto |
| Diagnostics | Player version, tool check, the latest log lines |
| Clear Watch Later / Clear History | Empty those lists |

## Files

All state lives in `/media/fat/YouTubeNative/ytcrt/`:
`settings.json`, `history.json`, `watch_later.json`, `subscriptions.txt`, and
`cache/` (feed cache, resolved handles, thumbnails — safe to delete).
The daemon log is `/media/fat/YouTubeNative/YouTube-crt-daemon.txt`.

## Developing without a MiSTer

```text
python tools/ytcrt_sim.py OUTDIR      # renders every screen and the playback OSD to PNG
python -m unittest tests.test_ytcrt   # POSIX-only pipe tests run on the MiSTer/Linux
```
