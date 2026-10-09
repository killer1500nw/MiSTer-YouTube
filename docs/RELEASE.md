# Suggested GitHub prerelease

Tag: v0.13.7
Title: v0.13.7 — experimental YouTube URL playback

Native on-device streaming to a MiSTer CRT/HDMI core. Enter a YouTube URL or ID;
blank input selects the reference talking clip. Uses the v0.13-smooth FPGA core.
The previous v0.13.6 player completed a 15-minute 30fps reference test with good
lip sync; v0.13.7 retains those playback settings and adds URL selection.

Attach the core RBF, player ZIP, corresponding source ZIP and SHA256SUMS.txt
from the outer release-assets folder. Mark this as a prerelease. External runtime
tools must already be installed; see docs/RUNTIME.md. No new FPGA functionality
is introduced by the URL-player version number.
