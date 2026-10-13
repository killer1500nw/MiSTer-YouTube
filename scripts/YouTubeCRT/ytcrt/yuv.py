"""The v0.15 core's YUV record format: header, slot layout and picture geometry.

See docs/FPGA-YUV-v0.15.md. The ARM decodes to planar YUV 4:2:0 at (almost) the video's own size and the core scales
it to the CRT, so everything here is arithmetic on sizes; no pixels are touched on the MiSTer.
"""
import struct

MAGIC = 0x35565559                 # "YUV5"
HEADER_BYTES = 64
Y_OFF, U_OFF, V_OFF, AUDIO_OFF = 0x00100, 0x4C000, 0x60000, 0x74000
SLOT_BYTES = 0x80000
SLOT_BASE = 0x1000                 # first slot, after the mailbox words
SLOTS = 8
MAX_W, MAX_H = 640, 480            # largest source picture the v0.15 core's slots hold
BIG_MAX_W = 864                    # v0.16 core with pictures in the framebuffer: up to 864 x 480 (the line buffers hold 1,024 pixels)
FRAME_W, FRAME_H = 640, 480        # the CRT frame the picture is scaled into (240 lines per field in 480i)
AUDIO_BYTES = 6400
# Overlay buffers (the GUI / on-screen display, 320x240 4:2:0, drawn 2x over the picture): three of 128 KiB after the slots.
OVERLAY_BASE = SLOT_BASE + SLOTS * SLOT_BYTES
OVERLAY_BYTES = 0x20000
OVERLAYS = 3
OV_W, OV_H = 320, 240
OV_Y_OFF, OV_U_OFF, OV_V_OFF = 0x00000, 0x13000, 0x18000     # inside an overlay buffer; luma 0 = transparent
MAP_BYTES = OVERLAY_BASE + OVERLAYS * OVERLAY_BYTES          # the mailbox mapping covers the mailbox, slots and overlays


def overlay_offset(index):
    return OVERLAY_BASE + (index % OVERLAYS) * OVERLAY_BYTES

FLAG_601, FLAG_FULL_RANGE, FLAG_DITHER = 1, 2, 4


# v0.16 core: the picture planes can live in the Linux framebuffer (physical start from FBIOGET_FSCREENINFO, 1920x1080x4 bytes). Slot n of
# the pictures sits at fb_start + FB_SLOTS_OFFSET + n * FB_SLOT_STRIDE: far from the top of the buffer, where console text would be drawn.
FB_SLOT_STRIDE = 0xA0000          # 655,360 bytes: room for the planes of an 864 x 480 picture (about 622 KB with the plane gaps)
FB_SLOTS_OFFSET = 0x100000        # the lowest the slots may start; the player puts them at the far end of the buffer (see transport.Framebuffer)
FB_MIN_LENGTH = FB_SLOTS_OFFSET + 8 * FB_SLOT_STRIDE


def fb_pic_base(fb_start, number):
    """Physical address of the picture-plane base for record `number` when the planes are written to the framebuffer."""
    return fb_start + FB_SLOTS_OFFSET + (number % 8) * FB_SLOT_STRIDE


def slot_offset(number):
    return SLOT_BASE + (number % SLOTS) * SLOT_BYTES


def plane_offsets(src_w, src_h):
    """(U offset, V offset) in bytes from the picture base: the fixed positions for pictures that fit them, else packed after Y."""
    if src_w * src_h <= MAX_W * MAX_H and src_w <= MAX_W:
        return U_OFF, V_OFF
    u_off = (Y_OFF + src_w * src_h + 0xFFF) & ~0xFFF
    v_off = u_off + (((src_w // 2) * (src_h // 2) + 0xFFF) & ~0xFFF)
    if v_off + (src_w // 2) * (src_h // 2) > FB_SLOT_STRIDE:
        raise ValueError('picture too big for a framebuffer slot')
    return u_off, v_off


def pack_header(src_w, src_h, ystride, cstride, dx0, dw, dy0, dh, hstep, vstep, sx0, sy0, flags=0, overlay=None, ref_slot=None,
                pic_base=None):
    """64-byte slot header. hstep/vstep/sx0/sy0 are unsigned 16.16 fixed point. `overlay` = None or the index (0..2)
    of the overlay buffer to draw over this picture. `ref_slot` = None, or the slot whose Y/U/V planes this record shows again
    (header word 5 bit 4 + slot in bits 7:5): the record then carries only a header and its audio.
    `pic_base` (v0.16 core) = None, or the physical byte address of the picture planes' base (word 6, bit 63 set): the planes are then
    read from there (the Linux framebuffer, which the CPU can fill ten times faster) instead of from this record's slot."""
    for name, v, hi in (('src_w', src_w, BIG_MAX_W if pic_base is not None else MAX_W), ('src_h', src_h, MAX_H)):
        if not 2 <= v <= hi or v % 2:
            raise ValueError('%s %d is outside 2..%d or odd' % (name, v, hi))
    if ystride % 8 or cstride % 8 or ystride < src_w or cstride < src_w // 2:
        raise ValueError('bad strides')
    if dx0 < 0 or dy0 < 0 or dw < 1 or dh < 1 or dx0 + dw > FRAME_W or dy0 + dh > FRAME_H:
        raise ValueError('destination rectangle outside the frame')
    words = [MAGIC | (flags << 32),
             src_w | (src_h << 16) | (ystride << 32) | (cstride << 48),
             dx0 | (dw << 16) | (dy0 << 32) | (dh << 48),
             hstep | (vstep << 32),
             sx0 | (sy0 << 32),
             (0 if overlay is None else (1 | ((overlay % OVERLAYS) << 1))) | (0 if ref_slot is None else (16 | ((ref_slot % 8) << 5))),
             0 if pic_base is None else ((1 << 63) | (pic_base & 0xFFFFFFF8)),
             0]
    if pic_base is not None:
        u_off, v_off = plane_offsets(src_w, src_h)
        if (u_off, v_off) != (U_OFF, V_OFF):                          # a big picture: word 7 says where its chroma planes are
            words[7] = u_off | (v_off << 32)
    return struct.pack('<8Q', *words)


def parse_header(data):
    w = struct.unpack('<8Q', bytes(data[:HEADER_BYTES]))
    if w[0] & 0xFFFFFFFF != MAGIC:
        raise ValueError('not a YUV5 header')
    return {'flags': (w[0] >> 32) & 7, 'src_w': w[1] & 0xFFFF, 'src_h': (w[1] >> 16) & 0xFFFF,
            'ystride': (w[1] >> 32) & 0xFFFF, 'cstride': (w[1] >> 48) & 0xFFFF,
            'dx0': w[2] & 0xFFFF, 'dw': (w[2] >> 16) & 0xFFFF, 'dy0': (w[2] >> 32) & 0xFFFF, 'dh': (w[2] >> 48) & 0xFFFF,
            'hstep': w[3] & 0xFFFFFFFF, 'vstep': (w[3] >> 32) & 0xFFFFFFFF,
            'sx0': w[4] & 0xFFFFFFFF, 'sy0': (w[4] >> 32) & 0xFFFFFFFF,
            'ov_en': bool(w[5] & 1), 'ov_idx': (w[5] >> 1) & 3,
            'ref': (w[5] >> 5) & 7 if w[5] & 16 else None,
            'pic_base': (w[6] & 0xFFFFFFFF) if w[6] >> 63 else None,
            'u_off': (w[7] & 0xFFFFFFFF) or U_OFF, 'v_off': (w[7] >> 32) or V_OFF}


def aligned_size(width, height):
    """The size ffmpeg should hand over: even height, width a multiple of 16 (so every row starts on a 64-bit boundary)."""
    return max(16, width - width % 16), max(2, height - height % 2)


def layout(src_w, src_h, crop='Off', interlaced=True):
    """Where and how the core draws a src_w x src_h picture: -> dict with the header geometry fields.

    crop 'Off' shows the whole picture inside the 4:3 frame (black bars where the aspect differs), '14:9' trims the sides
    of wider pictures to 14:9, 'Fill' trims them to 4:3 so they fill the screen (same meaning as the old ffmpeg crop).
    `prescale` is the (w, h) ffmpeg must shrink the picture to first when the core could not step through it fast
    enough (more than 2 source rows per frame line); None when no pre-scaling is needed."""
    cw = float(src_w)
    if crop == '14:9':
        cw = min(cw, src_h * 14 / 9.0)
    elif crop == 'Fill':
        cw = min(cw, src_h * 4 / 3.0)
    cx = (src_w - cw) / 2.0
    aspect = cw / src_h
    if aspect >= FRAME_W / float(FRAME_H):
        dw = FRAME_W
        dh = int(round(FRAME_W / aspect)) & ~1
    else:
        dh = FRAME_H
        dw = int(round(FRAME_H * aspect)) & ~1
    dh, dw = max(2, min(dh, FRAME_H)), max(2, min(dw, FRAME_W))
    dx0, dy0 = (FRAME_W - dw) // 2, ((FRAME_H - dh) // 2) & ~1
    hstep, vstep = cw / dw, src_h / float(dh)
    sx0 = max(0.0, cx + 0.5 * hstep - 0.5)
    sy0 = max(0.0, 0.5 * vstep - 0.5)
    if not interlaced:
        # 240p: each output pixel and line stands for a 2x2 cell of the 640x480 frame (the core keeps every second pixel and line),
        # so sample at the centre of that cell. Bilinear sampling there averages the neighbours that would be skipped, and the
        # picture sits where it belongs instead of a quarter of an output line too high.
        sx0 += 0.5 * hstep
        sy0 += 0.5 * vstep
    limit = 2.0                      # the core's eight row buffers cope with 2 source rows per frame line (4 per displayed line) in 480i and 240p
    prescale = None
    if vstep > limit:
        ph = int(dh * limit) & ~1
        prescale = (max(16, int(round(src_w * ph / float(src_h))) & ~15), ph)
    return {'dx0': dx0, 'dw': dw, 'dy0': dy0, 'dh': dh,
            'hstep': int(round(hstep * 65536)), 'vstep': int(round(vstep * 65536)),
            'sx0': int(round(sx0 * 65536)), 'sy0': int(round(sy0 * 65536)), 'prescale': prescale}


def header_for(src_w, src_h, crop='Off', interlaced=True, flags=0, overlay=None, ref_slot=None, pic_base=None):
    """Header for a frame of src_w x src_h samples (already aligned with aligned_size)."""
    g = layout(src_w, src_h, crop, interlaced)
    return pack_header(src_w, src_h, src_w, src_w // 2, g['dx0'], g['dw'], g['dy0'], g['dh'],
                       g['hstep'], g['vstep'], g['sx0'], g['sy0'], flags, overlay, ref_slot, pic_base)


def pieces(header, y, u, v, audio, number):
    """The four (slot offset, bytes) writes of one record. Y follows the header directly."""
    base = slot_offset(number)
    if len(audio) != AUDIO_BYTES:
        raise ValueError('audio must be %d bytes' % AUDIO_BYTES)
    return [(base, header + bytes(Y_OFF - HEADER_BYTES) + bytes(y)),
            (base + U_OFF, bytes(u)), (base + V_OFF, bytes(v)), (base + AUDIO_OFF, bytes(audio))]
