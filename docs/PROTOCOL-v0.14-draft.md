# v0.14-auto: what changed in the core, and what is still not possible

## Implemented in `fpga/` (simulation-tested; not yet run on hardware)

**1. Automatic start — no Off/On handshake.**
* Streaming is now **always on** and the picture is always the player's: the old *RAM streaming*, *Display*, *Pattern*, *Test tone* and *Load frame* options were removed from the core menu and hardwired in the RTL (`stream_on` is a constant wire), so a saved config from an older core can never switch streaming off or force the test pattern. The colour bars still show until the first frame arrives, as an idle screen. The core menu now has only **Output**, **480i field order** and **Reset**.
* A stop command in the mailbox (run word 0, or session 0) now **clears a latched error** in `ddr_stream.sv`.
  Previously only toggling the (now removed) RAM streaming option Off/On could clear it, which is why the old player made you do the
  Off/On dance. Now the player writes the stop command, waits for the core to report idle with no error,
  zeroes the counters and publishes a fresh session. Same record format, same slots.
* Identity word is now `0x34445459` (was `0x33445459`) so the old v0.13 player and this core
  reject each other instead of mis-handshaking.

**2. Optional 480i output** (`rtl/crt240.sv`, core menu **Output**, **480i field order**).
* Same 424-pixel line (15.72 kHz). 525 lines per frame: field 0 has 262 lines, field 1 has 263, and field 1's
  vertical sync starts half a line (212 pixels) into line 244. Fields come out at 59.94 Hz.
* Both fields show the same 320×240 picture, so it is a compatibility mode, not extra resolution
  (320×480 would be 300 KiB, larger than the 256 KiB record slots).
* The mode is latched at the end of a frame; it never changes mid-picture. 240p timing is bit-for-bit unchanged
  (the original timing test still passes).
* `VGA_F1` carries the field flag for the HDMI scaler; **field order** swaps its polarity in case the scaler
  weaves the fields the wrong way round.
* Audio: the 1600-sample segment is stretched to the 480i frame pair (667,800 clocks instead of 666,528) so there is
  no 63 µs gap every 33 ms.

Simulation (`fpga/tests/run_modelsim.sh`, or `run.sh` with Icarus): 240p timing, 480i timing, frame store, stream
protocol including recovery from a latched error by a stop command alone, and full-emu scanout in both paths.
**Not verified:** Quartus timing on silicon, how your CRT/scaler handles the 480i sync, and field polarity on HDMI.

## Not possible / not done: 60 fps records

Your v0.13 measurement is the evidence: decode-and-pipe alone reached 32.4 fps at 426×240/30, almost no headroom
for 30 fps, and 60 fps needs twice that. YouTube also rarely offers 60 fps H.264 at 240p; its 60 fps H.264 streams are
720p/1080p, far beyond a 2×800 MHz Cortex-A9. The 60 Hz refresh you already have is unchanged. The two 60 fps modes in
the player stay disabled (they need a future core with 60 fps records and an ABI 131 copy helper that takes a length;
the shipped `libyt_sender.so` only copies 160000-byte records).

What helps 30 fps decoding is the **Decoder speed** setting and the **Speed test** in the player.
