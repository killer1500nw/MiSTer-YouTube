"""Crop an H.264 stream by rewriting the cropping fields of its sequence parameter set (SPS): no decoding, a few bytes per keyframe.

The H264 core shows each picture at the shape its stream declares, cropping included. So to show a 16:9 picture as 14:9 (the way many
TV stations trimmed widescreen in the 90s) the video is left alone and the SPS that precedes every keyframe is rewritten with a bigger
`frame_crop_left/right_offset`. Everything after the cropping fields (the VUI: timing, colour, aspect) is copied bit for bit.
"""

import re

SPS_START = re.compile(b'\x00\x00\x01[\x07\x27\x47\x67]')       # a start code followed by a NAL header of type 7
HIGH_PROFILES = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)


class SpsError(ValueError):
    pass


def unescape(data):
    """NAL bytes -> RBSP: drop the 0x03 that follows two zero bytes."""
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def escape(rbsp):
    """RBSP -> NAL bytes: put a 0x03 after two zero bytes when the next byte is 0..3."""
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


class Bits:
    def __init__(self, data):
        self.data, self.pos, self.n = data, 0, len(data) * 8

    def bit(self):
        if self.pos >= self.n:
            raise SpsError('ran past the end')
        b = (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def u(self, count):
        v = 0
        for _ in range(count):
            v = (v << 1) | self.bit()
        return v

    def ue(self):
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise SpsError('bad exp-golomb')
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)


class Out:
    def __init__(self):
        self.bits = []

    def u(self, value, count):
        for i in range(count - 1, -1, -1):
            self.bits.append((value >> i) & 1)

    def ue(self, value):
        v = value + 1
        length = v.bit_length()
        self.u(0, length - 1)
        self.u(v, length)

    def copy(self, data, start, stop):
        for p in range(start, stop):
            self.bits.append((data[p >> 3] >> (7 - (p & 7))) & 1)

    def tobytes(self):
        bits = list(self.bits)
        while len(bits) % 8:
            bits.append(0)
        return bytes(sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8))


def _scaling_list(r, size):
    last, nxt = 8, 8
    for _ in range(size):
        if nxt != 0:
            nxt = (last + r.se() + 256) % 256
        last = last if nxt == 0 else nxt


def parse(nal):
    """The fields of an SPS NAL (starting with its 1-byte header) that cropping needs.

    -> dict: width, height (coded, in pixels), crop (left, right, top, bottom in crop units), unit_x, unit_y,
    crop_flag_bit (bit index of frame_cropping_flag in the RBSP), vui_bit (where the bits after the cropping fields start),
    stop_bit (index of the RBSP's final 1 bit), rbsp."""
    if not nal or nal[0] & 0x1F != 7:
        raise SpsError('not an SPS')
    rbsp = unescape(nal[1:])
    r = Bits(rbsp)
    profile = r.u(8)
    r.u(8)                                                   # constraint flags and reserved bits
    r.u(8)                                                   # level
    r.ue()                                                   # sps id
    chroma = 1
    if profile in HIGH_PROFILES:
        chroma = r.ue()
        if chroma == 3:
            r.u(1)
        r.ue()
        r.ue()
        r.u(1)
        if r.u(1):                                           # seq_scaling_matrix_present_flag
            for i in range(8 if chroma != 3 else 12):
                if r.u(1):
                    _scaling_list(r, 16 if i < 6 else 64)
    r.ue()                                                   # log2_max_frame_num_minus4
    poc = r.ue()
    if poc == 0:
        r.ue()
    elif poc == 1:
        r.u(1)
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()                                                   # max_num_ref_frames
    r.u(1)                                                   # gaps_in_frame_num_value_allowed_flag
    width_mbs = r.ue() + 1
    height_units = r.ue() + 1
    frame_mbs_only = r.u(1)
    if not frame_mbs_only:
        r.u(1)                                               # mb_adaptive_frame_field_flag
    r.u(1)                                                   # direct_8x8_inference_flag
    flag_bit = r.pos
    crop = (0, 0, 0, 0)
    if r.u(1):
        crop = (r.ue(), r.ue(), r.ue(), r.ue())
    vui_bit = r.pos
    stop = len(rbsp) * 8 - 1
    while stop >= 0 and not (rbsp[stop >> 3] >> (7 - (stop & 7))) & 1:
        stop -= 1
    if stop < vui_bit:
        raise SpsError('no stop bit after the cropping fields')
    unit_x = 1 if chroma == 0 else 2 if chroma in (1, 2) else 1
    unit_y = (1 if chroma in (0, 2, 3) else 2) * (2 - frame_mbs_only)
    if chroma == 3:
        unit_x = 1
    return {'width': width_mbs * 16, 'height': height_units * 16 * (2 - frame_mbs_only), 'crop': crop, 'unit_x': unit_x, 'unit_y': unit_y,
            'crop_flag_bit': flag_bit, 'vui_bit': vui_bit, 'stop_bit': stop, 'rbsp': rbsp, 'header': nal[0]}


def visible(info):
    l, rr, t, b = info['crop']
    return (info['width'] - info['unit_x'] * (l + rr), info['height'] - info['unit_y'] * (t + b))


def crop_to(nal, aspect):
    """The SPS with its cropping widened so that the visible picture is no wider than `aspect` (num, den). Returns the NAL unchanged
    when the picture is already that narrow (or anything looks unusual)."""
    try:
        info = parse(nal)
    except SpsError:
        return nal
    vw, vh = visible(info)
    want = min(vw, vh * aspect[0] // aspect[1])
    unit = info['unit_x']
    want -= want % (2 * unit)                                # an even number of crop units, so the picture stays centred on a unit boundary
    extra = vw - want
    if extra <= 0 or want < 16:
        return nal
    left_px = (extra // (2 * unit)) * unit
    right_px = extra - left_px
    l, rr, t, b = info['crop']
    out = Out()
    out.copy(info['rbsp'], 0, info['crop_flag_bit'])
    out.u(1, 1)                                              # frame_cropping_flag
    out.ue(l + left_px // unit)
    out.ue(rr + right_px // unit)
    out.ue(t)
    out.ue(b)
    out.copy(info['rbsp'], info['vui_bit'], info['stop_bit'] + 1)       # the rest, including the stop bit
    return bytes([info['header']]) + escape(out.tobytes())


def crop_access_unit(au, aspect):
    """An access unit (Annex B) with every SPS in it cropped to `aspect`; the rest is untouched."""
    if aspect is None or SPS_START.search(au) is None:
        return au                                            # (most access units have no SPS: nothing to look at)
    out = bytearray()
    i = 0
    n = len(au)
    changed = False
    while i < n:
        j = au.find(b'\x00\x00\x01', i)
        if j < 0:
            out += au[i:]
            break
        out += au[i:j + 3]
        k = j + 3
        nxt = au.find(b'\x00\x00\x01', k)
        end = n if nxt < 0 else nxt
        nal = au[k:end]
        # (the zero byte that belongs to the next start code is not part of this NAL)
        stripped = nal.rstrip(b'\x00') if nxt >= 0 else nal
        tail = nal[len(stripped):]
        if stripped and stripped[0] & 0x1F == 7:
            new = crop_to(stripped, aspect)
            changed = changed or new is not stripped
            out += new + tail
        else:
            out += nal
        i = end
    return bytes(out) if changed else au
