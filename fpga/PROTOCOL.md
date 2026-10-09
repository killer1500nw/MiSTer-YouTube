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
