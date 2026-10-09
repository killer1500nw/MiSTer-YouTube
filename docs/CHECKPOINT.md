# Development checkpoint — 9 October 2026

Goal: native URL-based YouTube playback on MiSTer with CRT analog/component and
HDMI output, without casting or saving full media files. Preserve working CRT
settings. User cannot access Scripts while this core is loaded.

Working combination: v0.13-smooth core + v0.13.7 URL player. v0.13.6 full-clip
playback is hardware validated; v0.13.7 adds only URL selection and naming.
The reference clip is M7kB0lis3xg. Report summaries are in README; private user
logs and host-specific Quartus reports are deliberately omitted.

Performance findings:
- Earlier 640-wide source selection sustained about 24fps with nearly empty queues.
- A four-record producer queue alone did not fix that shortfall.
- Decode/pipe-only measurement reached 32.448fps, little spare capacity.
- Selecting 426x240 H.264 and fast bilinear scaling sustained 30fps, with full
  producer and FPGA queues across the reference clip and confirmed good lip sync.
- Keep eight FPGA slots, four producer records, 160000-byte A/V records,
  153600 RGB565 bytes + 6400 audio bytes; native helper ABI 130.

Next tasks: test other URLs; improve visible resolver errors before core launch;
create a verified runtime installer; simplify startup; later consider a CRT menu
inspired by 240-MP. No title search, login, pause/seek or selectable 480i yet.
Do not claim general video compatibility from the one long reference test.
