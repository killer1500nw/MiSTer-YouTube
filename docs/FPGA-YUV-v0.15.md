# v0.15 YUV scaler core: specification

Goal: the ARM only decodes. The core scales the decoded picture to the CRT, converts colour in 8 bits and draws a true
480i (or 640x240) picture. Measured on a real MiSTer (Speed test), scaling plus RGB565 conversion take about half of
ffmpeg's time at 360p (1.49x with them, 2.97x without), and the picture is squeezed into 320x240 before the CRT sees it.

Status: **design + reference model** (`tools/yuv_ref.py`). The RTL is written against this document and checked
pixel for pixel against the model.

## Video timing

| | v0.14 | v0.15 |
| --- | --- | --- |
| `clk_sys` | 20 MHz | **40 MHz** (PLL parameter only) |
| pixel enable | every 3 clocks = 6.67 MHz | every 3 clocks = **13.33 MHz** |
| line | 424 pixels = 63.6 us | **848** pixels = 63.6 us (15.72 kHz, unchanged) |
| active | 320 x 240 | **640 x 240** per field (480 lines per frame in 480i) |
| frame | 262 lines (240p) / 525 lines (480i) | unchanged |

Horizontal sync is 64 pixels (4.8 us) starting at pixel 672. The 480i second-field half-line offset is 424 pixels.
Audio: one 1600-sample segment per two refreshes = 1,333,056 clocks (240p) / 1,335,600 (480i) at 40 MHz (the same time).

## Memory map (`BASE` = 0x30000000, unchanged mailbox)

Mailbox words (offsets 0..0x37) are unchanged from v0.14 except the identity, now `0x35445459`.
Slots: **8 slots of 512 KiB**, slot `n` at `BASE + 0x1000 + (n % 8) * 0x80000` (4 MiB + 4 KiB region).

Slot layout:

| Offset | Content |
| --- | --- |
| 0x00000 | 64-byte header (below) |
| 0x00100 | Y plane, `ystride` bytes per row, `src_h` rows (max 640 x 480) |
| 0x4C000 | U plane, `cstride` bytes per row, `(src_h+1)/2` rows |
| 0x60000 | V plane, same shape |
| 0x74000 | 1600 stereo S16LE samples (6400 bytes) |

Header (little-endian 64-bit words):

| Word | Bits | Field |
| --- | --- | --- |
| 0 | 31:0 | magic `0x35565559` ("YUV5") |
| 0 | 34:32 | flags: bit0 BT.601 (else BT.709), bit1 full range (else limited), bit2 dither to 6 bits |
| 1 | 15:0 / 31:16 | `src_w` (<= 640, multiple of 2), `src_h` (<= 480) |
| 1 | 47:32 / 63:48 | `ystride`, `cstride` (bytes, multiples of 8) |
| 2 | 15:0 / 31:16 | `dx0`, `dw` (destination rectangle in the 640 x 480 frame) |
| 2 | 47:32 / 63:48 | `dy0`, `dh` |
| 3 | 31:0 / 63:32 | `hstep`, `vstep`: source samples per destination pixel / line, unsigned 16.16 (vstep <= 2.0) |
| 4 | 31:0 / 63:32 | `sx0`, `sy0`: source coordinate of the first destination pixel / line, unsigned 16.16 |

The player computes the steps and start offsets (crop, aspect, letterbox) so the RTL stays simple. Pixel centres:
`sx0 = max(0, (0.5 * hstep - 0.5 + crop_x)) * 65536`, likewise `sy0`.

The ARM must not overwrite the slot being displayed, so the acknowledged `consumed` count lags one record behind the
commit count (the displayed record is released only when the next one is committed). With 8 slots the player keeps at
most 6 queued.

## Scaler arithmetic (bit-exact; `tools/yuv_ref.py` is the reference)

Destination pixel `x` (0..639) on frame line `d` (480i: `d = 2 * vc + field`; 240p: `d = 2 * vc`), inside the rectangle
when `dx0 <= x < dx0+dw` and `dy0 <= d < dy0+dh`, else black.

```
sx = sx0 + (x - dx0) * hstep           sy = sy0 + (d - dy0) * vstep           (32-bit)
luma:   x0 = min(sx >> 16, src_w-1)    x1 = min(x0+1, src_w-1)   fx = (sx >> 8) & 255
        y0 = min(sy >> 16, src_h-1)    y1 = min(y0+1, src_h-1)   fy = (sy >> 8) & 255
chroma: csx = sx >> 1                  (left-sited horizontally)
        csy = max(0, (sy >> 1) - 0x4000)   (centred vertically)
        same split into index / fraction, clamped to the chroma plane size
lerp:   h_a = (a*(256-fx) + b*fx + 128) >> 8     per row, then
        v   = (h_a*(256-fy) + h_b*fy + 128) >> 8
colour: C = Y - 16 (limited) or Y,  D = U - 128,  E = V - 128
        BT.709 limited: R = (298C + 459E + 128) >> 8   G = (298C - 55D - 136E + 128) >> 8   B = (298C + 541D + 128) >> 8
        BT.601 limited: 298C + 409E,  298C - 100D - 208E,  298C + 516D
        full range:     256 for 298, BT.709 403/48/120/475, BT.601 359/88/183/454 (C = Y)
        saturate 0..255
dither: out = min(255, v + M[d&3][x&3]) & 0xFC, M = 0 2 0 2 / 3 1 3 1 / 0 2 0 2 / 3 1 3 1   (flag bit2)
```

## Hardware structure

* **Line buffers**: per plane four row buffers (row `r` lives in buffer `r mod 4`), each split into an even-pixel and an
  odd-pixel bank so the two neighbours of a bilinear tap are read from different RAMs in one clock.
* **Prefetch**: during line `n` the rows for line `n+1` are fetched from DDR (one burst per plane row) into the buffers
  not in use (`vstep <= 2` guarantees no conflict). All buffers are invalidated at a frame commit.
* **DDR arbiter**: the scaler's fetches take the DDR port between the stream controller's single-beat requests.
* **Pixel pipeline**: fully pipelined bilinear interpolation (about 20 small multipliers, DSP blocks) and the colour matrix; eight
  clock latency, all sync signals delayed equally.
* **Commit**: the stream controller reads the 64-byte header and copies only the audio (6400 bytes) into the audio
  banks; the video stays in DDR. At the commit boundary the header is latched and the slot becomes the displayed one. In 480i a record is
  committed only at a field-0 boundary so both fields come from the same frame.
