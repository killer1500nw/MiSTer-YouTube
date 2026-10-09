# YouTubeCRT v0.13-smooth: 320×240 at 30fps

This experimental build doubles the video rate from 15fps to 30fps while
keeping the sharp 320×240 picture. Native 240p CRT timing and your component,
analogue and HDMI output settings remain unchanged. No MiSTer.ini changes.

## First: compile

1. Extract this ZIP into a **new folder** on your Windows PC.
2. Run **build-windows.bat** with your existing Quartus 17.0 installation.
3. Upload the new **build-result.zip** for review. If compilation fails,
   upload **build.log** instead.

No compiled RBF is included. Keep your working v0.12-sharp core and
YouTubeStreamSharp folder as the backup. FPGA simulations do not establish
Quartus fit/timing or actual on-device decoding speed.

## After the build has passed review

1. Copy the checked RBF to `_Other`, named **YouTubeCRT_v0.13-smooth.rbf**.
2. Copy `MiSTer-Scripts/YouTubeStreamSmooth` into `/media/fat/Scripts/`.
3. Run **YouTubeStreamSmooth → YouTube_Play_URL**.
4. Press Enter for the short default clip, or enter a YouTube video URL/ID.
5. Return to the menu and load **v0.13-smooth** within two minutes.
6. Select **Display → Loaded frame**. Set **RAM streaming → On for 5 seconds →
   Off for 2 seconds → On**. Leave it On while resolving and buffering.
7. Check motion, continuous sound and lip sync on CRT and HDMI.
8. Upload `/media/fat/YouTubeNative/YouTube-url-stream-smooth.txt`.

Start with the short clip. If that works, repeat the same 15-minute talking
video to check sustained playback and lip sync. A clip recorded at a low
frame rate cannot gain real motion detail simply by being played at 30fps.

Run **YouTube_Stop_Stream** in this folder to stop. Use the new launcher with
the new core; protocol identifiers reject mismatched versions. Launchers share
a lock so the old and new workers cannot write simultaneously. The new report
has its own filename. Your installed YouTubeNative runtime remains required.

## Why this is a performance test

The working 320×240/15fps version completed the user's 15-minute clip in sync.
Its longest recorded frame-copy call was 31.367ms, including any time Linux
paused that process. The 30fps period is only about 33.326ms. That one maximum
does not establish the average cost or whether 30fps can be sustained.

This version records mean and maximum native-copy call time, copies exceeding
the frame period, and time waiting/reading decoder pipes. Pipe wait includes
network, process scheduling and decoder waits; it is not decoder CPU time.
The final elapsed duration and your observations establish whether playback
actually keeps pace. Occasional long copy calls can be absorbed by buffering.

The eight slots now hold about 267ms of video when full (about half the
previous coverage). The stream must supply roughly 4.8MB/s of decoded A/V.
Network stalls or insufficient decoding/transfer throughput can cause pauses
or gaps in sound. If this happens, retain v0.12 and send the new report.

## Implementation

- Full 320×240 RGB565 picture, with video prepared at 30fps.
- Joint picture/audio commit every two native refreshes, about 30.006fps.
- 1600 stereo PCM samples per frame; source audio remains 48kHz.
- Eight 256KiB reusable RAM slots; 160000 used bytes per record.
- Same bounded native scalar copy method; no untested vector/device stores.
- No saved movie file, casting device or external transcoding server.

The nominal 30fps content runs about 0.021% fast, as the previous 15fps content
did; video and audio use the same cadence. This is not a 480i or 60fps release.
Completed public videos only; no live streams, playlists, seeking or sign-in.

Required runtime, already installed by the working version:
`/media/fat/YouTubeNative/resolver-2026.08.19-qjs-0.17.0/` (resolver and QuickJS),
`/media/fat/YouTubeNative/ffmpeg-7.0.2-armhf-static/ffmpeg`, and Python 3.9.

Source, tests, GPL license and certificate notices are included.
See `DEVELOPER.md`, `PROTOCOL.md` and `tests/RESULTS.txt`.
