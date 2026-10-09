# Development notes

Based on hardware-tested v0.12-sharp: 320×240, 13421 A/V records / 894.73s,
894.57s playback elapsed, user-confirmed lip sync to the end, logged queue
5–8, maximum native-copy call 31.367ms. These are baseline measurements,
not measurements of v0.13.

## Changes

- A one-bit movie phase commits every two display refreshes rather than four.
- Audio segments contain 1600 samples rather than 3200 and span 666528 rather
  than 1333056 core clocks. Phase accumulation and synchronous RAM fetch logic
  are retained. Each of the two audio banks shrinks to 1600 samples.
- DDR payload shrinks from 166400 to 160000 bytes; video bytes are unchanged.
- FFmpeg outputs 30fps and the sender pairs each picture with 6400 audio bytes.
- Native helper ABI version 130; same bounded scalar copy algorithm.
- Distinct protocol IDs, folder, OSD version and report name.
- Mean/max copy timing, over-period copy count and pipe-read wait diagnostics.

CRT scan timing, PLL, sys framework, full-frame video banks and Quartus
constraints are unchanged. No claimed copy-speed improvement: the measurements
will determine whether sustained performance is adequate on MiSTer.

## Native helper build

Run inside `MiSTer-Scripts/YouTubeStreamSmooth`, with Zig 0.13.0:

```sh
zig build-lib -target arm-linux-musleabihf -mcpu cortex_a9 \
  -O ReleaseFast -dynamic -fPIC -fno-compiler-rt -fno-stack-protector \
  -fstrip -fsoname=libyt_sender.so \
  -cflags -O2 -fno-strict-aliasing -fno-vectorize -fno-slp-vectorize \
  -fno-unwind-tables -fno-asynchronous-unwind-tables -- native_sender.c \
  -femit-bin=libyt_sender.so
```

The library is ARM Cortex-A9 EABI5 hard-float, with no dynamic dependencies or
relocations. Its exports are yt_native_version and yt_copy_slot. Each record
uses exactly 40000 aligned scalar 32-bit stores. Two DMB SY barriers surround
the first/last-word readback. No vector stores to the device mapping.

## Validation

`bash tests/run.sh` uses Icarus Verilog 12, Python 3, GCC, FFmpeg and OpenSSL.
It covers native CRT timing, frame banks, both immediate and delayed Avalon
responses, all PCM samples and joint commits, slot wrap, missing-data silence
and restart, EOF, stop, overrun rejection and disable under bus backpressure.
The full emu top level is tested at the new actual 666528-clock record period.
Both still and streamed scanout check every displayed pixel and sync alignment.
Eight host tests cover HTTP/HTTPS decoding, a mapped ring with a 30fps consumer,
video-only streams, errors, cancellation, URL/format handling and startup/lock.

`python3 tests/test_arm_helper.py` requires Unicorn and pyelftools. It executes
the distributed ARM library across all eight slots, verifies every write,
surrounding canaries and invalid argument rejection. The host integration
suite compiles a temporary HOST helper without changing the ARM library.

Tests do not establish Quartus fit/timing or on-device throughput. Send the
Windows build-result.zip for review before using its RBF. Source licensing,
upstream attribution and bundled certificate notices are retained. Resolver,
FFmpeg and QuickJS binaries remain separately installed.

Distributed ARM helper SHA256:
`052ccec5ab64cf1f310be91ab749413acb3764be33e1cdc4c083d23338f220e6`
