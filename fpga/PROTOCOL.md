# v0.13-smooth DDR streaming protocol

Allocation and slot stride are unchanged from v0.12:
`0x30000000..0x30200fff`, length `0x201000` (2MiB + 4KiB).
The full-region platform memory guard is retained.

CPU-owned 32-bit little-endian mailbox words:

| Offset | Meaning |
| --- | --- |
| 0 | Run command `0x33515459`; zero stops |
| 4 | Nonzero session ID |
| 8 | Published record count |
| 12 | Final count; zero until EOF |

FPGA-owned words:

| Offset | Meaning |
| --- | --- |
| 16 | Consumed count at joint picture/audio commit |
| 20 | Acknowledged session |
| 24 | Played count |
| 28 | Error flag |
| 32 | Identity `0x33445459` |
| 36 | Heartbeat |

Slot address: `BASE + 0x1000 + (sequence % 8)*0x40000`.
Each record is **160000 bytes**: 153600 bytes of row-major 320×240 RGB565LE,
then 6400 bytes (1600 samples) of stereo S16LE PCM. Padding is not accessed.

The producer copies before publication, using aligned volatile 32-bit stores,
DMB SY, first/last-word readback and a second DMB SY. Slots are reused only
after acknowledgement. The FPGA reads exactly 20000 single 64-bit beats per
record and serialises bytes into the back picture and audio banks.

Commit occurs every two native refreshes: 666528 clocks at 20MHz, or
33.3264ms. The 1600 audio samples span the same period, preserving the
previous effective audio sample rate. Initial prefill remains four records;
a short complete stream may start earlier. Late data holds the picture and
mutes after the current audio segment, so resumed audio matches its picture.

Identity and run command deliberately differ from v0.12 to prevent starting
a stream with the wrong payload/audio format. Startup retains the tested
clear-controls and Off/On sequence. No larger DDR reservation is required.

## v0.14-auto additions (FPGA-owned, written every poll)

Identity is now `0x34445459`. A stop command (run word 0 or session 0) also clears a latched error.

| Offset | Meaning |
| --- | --- |
| 40 | `joystick_0` from hps_io: bits 0-3 right/left/down/up, then A, B, X, Y, L, R, Select, Start from bit 4 |
| 44 | `joystick_1` (same layout) |
| 48 | keyboard events 0 and 1: `ps2_key[10:0]` each in a 16-bit slot, newest in bits 15:0 |
| 52 | keyboard event 2 in bits 15:0; bits 31:16 = event sequence counter (increments per event) |

`ps2_key` = `{toggle, pressed, extended, scancode[7:0]}` (PS/2 set 2 make codes). The reader keeps the last
sequence number and processes the `n` newest events (up to 3) in order.
