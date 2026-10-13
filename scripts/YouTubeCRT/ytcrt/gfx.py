"""Tiny RGB565LE software canvas. Pure stdlib; every operation is a bytes
slice assignment, because per-pixel Python loops are far too slow on the
800 MHz Cortex-A9."""
import struct
import unicodedata
import zlib

from .font8x8 import FONT


def rgb(r, g, b):
    return struct.pack('<H', ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3))


def fold(text):
    """Reduce text to the printable ASCII the 8x8 font can draw."""
    out = []
    for ch in unicodedata.normalize('NFKD', text):
        if unicodedata.combining(ch):
            continue
        o = ord(ch)
        out.append(ch if 32 <= o < 127 else ('?' if o > 127 and not ch.isspace() else ' '))
    return ''.join(out)


_glyphs = {}


def _glyph_rows(ch, fg, bg, sx, sy):
    key = (ch, fg, bg, sx, sy)
    rows = _glyphs.get(key)
    if rows is None:
        data = FONT[ord(ch)]
        rows = []
        for r in range(8):
            bits = data[r]
            line = b''.join((fg if bits >> x & 1 else bg) * sx for x in range(8))
            rows.extend([line] * sy)
        if len(_glyphs) > 4000:
            _glyphs.clear()
        _glyphs[key] = rows
    return rows


class Canvas:
    """w*h RGB565LE pixels in `buf` starting at byte `offset`."""

    def __init__(self, w=320, h=240, buf=None, offset=0):
        self.w, self.h = w, h
        self.buf = buf if buf is not None else bytearray(w * h * 2)
        self.off = offset

    def fill(self, c):
        n = self.w * self.h * 2
        self.buf[self.off:self.off + n] = c * (self.w * self.h)

    def rect(self, x, y, w, h, c):
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, self.w), min(y + h, self.h)
        if x1 <= x0 or y1 <= y0:
            return
        row = c * (x1 - x0)
        n = len(row)
        for yy in range(y0, y1):
            o = self.off + (yy * self.w + x0) * 2
            self.buf[o:o + n] = row

    def text(self, x, y, s, fg, bg, sx=1, sy=1, maxchars=None):
        """Draw opaque text (cell background `bg`). Returns pixel width."""
        s = fold(s)
        if maxchars is not None:
            s = s[:maxchars]
        if x < 0:
            skip = (-x + 8 * sx - 1) // (8 * sx)
            s, x = s[skip:], x + skip * 8 * sx
        s = s[:max(0, (self.w - x) // (8 * sx))]
        if not s:
            return 0
        cells = [_glyph_rows(ch, fg, bg, sx, sy) for ch in s]
        width = 8 * sx * len(s)
        for r in range(8 * sy):
            yy = y + r
            if 0 <= yy < self.h:
                o = self.off + (yy * self.w + x) * 2
                self.buf[o:o + width * 2] = b''.join(c[r] for c in cells)
        return width

    @staticmethod
    def text_width(s, sx=1):
        return len(s) * 8 * sx

    def blit(self, x, y, w, h, data):
        """Copy raw RGB565LE rows (w*h*2 bytes), clipped to the canvas."""
        for r in range(h):
            yy = y + r
            if not 0 <= yy < self.h or x < 0 or x + w > self.w:
                continue
            o = self.off + (yy * self.w + x) * 2
            self.buf[o:o + w * 2] = data[r * w * 2:(r + 1) * w * 2]

    def frame(self, x, y, w, h, c, t=1):
        self.rect(x, y, w, t, c)
        self.rect(x, y + h - t, w, t, c)
        self.rect(x, y, t, h, c)
        self.rect(x + w - t, y, t, h, c)

    def to_rgb888(self):
        out = bytearray(self.w * self.h * 3)
        mv = memoryview(self.buf)[self.off:self.off + self.w * self.h * 2]
        words = struct.unpack('<%dH' % (self.w * self.h), mv)
        i = 0
        for v in words:
            out[i] = (v >> 8) & 0xF8 | (v >> 13)
            out[i + 1] = (v >> 3) & 0xFC | (v >> 9 & 3)
            out[i + 2] = (v << 3) & 0xF8 | (v >> 2 & 7)
            i += 3
        return bytes(out)

    def save_png(self, path, scale=3):
        """Write a PNG; `scale` repeats pixels for easy viewing."""
        raw = self.to_rgb888()
        rows = []
        for y in range(self.h):
            line = raw[y * self.w * 3:(y + 1) * self.w * 3]
            if scale > 1:
                line = b''.join(line[i:i + 3] * scale for i in range(0, len(line), 3))
            rows.extend([b'\x00' + line] * scale)

        def chunk(tag, body):
            c = struct.pack('>I', len(body)) + tag + body
            return c + struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
        png = (b'\x89PNG\r\n\x1a\n' +
               chunk(b'IHDR', struct.pack('>IIBBBBB', self.w * scale, self.h * scale, 8, 2, 0, 0, 0)) +
               chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))
        with open(path, 'wb') as f:
            f.write(png)


# ---------------------------------------------------------------------------------------------------------------------
# YUV 4:2:0 canvas for the v0.15 core. Same drawing calls as Canvas (colours are the same two-byte RGB565 values), but the
# pixels live in three planes the core reads directly. Used as the overlay (GUI / on-screen display): luma 0 is
# transparent, and every colour drawn has luma >= 16, so drawn pixels are always opaque.

_YUV_CACHE = {}


def yuv_of(color, bt601=False):
    """RGB565 colour (2 bytes) -> (Y, U, V), limited range, BT.709 (or BT.601)."""
    key = (bytes(color), bt601)
    got = _YUV_CACHE.get(key)
    if got is None:
        v = struct.unpack('<H', bytes(color))[0]
        r5, g6, b5 = v >> 11, (v >> 5) & 63, v & 31
        r, g, b = (r5 << 3) | (r5 >> 2), (g6 << 2) | (g6 >> 4), (b5 << 3) | (b5 >> 2)
        if bt601:
            y = 16 + 0.25679 * r + 0.50413 * g + 0.09791 * b
            u = 128 - 0.14822 * r - 0.29099 * g + 0.43922 * b
            vv = 128 + 0.43922 * r - 0.36779 * g - 0.07143 * b
        else:
            y = 16 + 0.18260 * r + 0.61423 * g + 0.06200 * b
            u = 128 - 0.10064 * r - 0.33857 * g + 0.43922 * b
            vv = 128 + 0.43922 * r - 0.39894 * g - 0.04027 * b
        got = (max(1, min(255, int(round(y)))), max(0, min(255, int(round(u)))), max(0, min(255, int(round(vv)))))
        _YUV_CACHE[key] = got
    return got


_yglyphs = {}


def _yuv_glyph(ch, fg, bg, sx, sy):
    """Cached glyph as (luma rows, U rows, V rows): lists of bytes. A 2x2 block of pixels takes the text colour's chroma if
    any of its pixels is text (so coloured text stays coloured at half chroma resolution)."""
    key = (ch, fg, bg, sx, sy)
    got = _yglyphs.get(key)
    if got is None:
        data = FONT[ord(ch)]
        w, h = 8 * sx, 8 * sy
        bits = []
        for r in range(8):
            line = [(data[r] >> x) & 1 for x in range(8) for _ in range(sx)]
            bits.extend([line] * sy)
        fy, fu, fv = fg
        by, bu, bv = bg
        luma = [bytes(fy if b else by for b in row) for row in bits]
        urows, vrows = [], []
        for r in range(0, h, 2):
            ur, vr = bytearray(), bytearray()
            for c in range(0, w, 2):
                on = bits[r][c] or bits[r][c + 1] or bits[r + 1][c] or bits[r + 1][c + 1]
                ur.append(fu if on else bu)
                vr.append(fv if on else bv)
            urows.append(bytes(ur))
            vrows.append(bytes(vr))
        if len(_yglyphs) > 4000:
            _yglyphs.clear()
        got = _yglyphs[key] = (luma, urows, vrows)
    return got


class YuvCanvas:
    """w x h (both even) planar YUV 4:2:0. `y`, `u`, `v` are bytearrays."""

    def __init__(self, w=320, h=240, bt601=False):
        self.w, self.h, self.bt601 = w, h, bt601
        self.cw, self.ch = w // 2, h // 2
        self.y = bytearray(w * h)
        self.u = bytearray([128]) * (self.cw * self.ch)
        self.v = bytearray([128]) * (self.cw * self.ch)

    def _yuv(self, c):
        return yuv_of(c, self.bt601)

    def clear(self):
        """Make everything transparent (luma 0)."""
        self.y[:] = bytes(self.w * self.h)
        self.u[:] = bytes([128]) * (self.cw * self.ch)
        self.v[:] = bytes([128]) * (self.cw * self.ch)

    def fill(self, c):
        yy, uu, vv = self._yuv(c)
        self.y[:] = bytes([yy]) * (self.w * self.h)
        self.u[:] = bytes([uu]) * (self.cw * self.ch)
        self.v[:] = bytes([vv]) * (self.cw * self.ch)

    def rect(self, x, y, w, h, c):
        x0, y0 = max(x, 0), max(y, 0)
        x1, y1 = min(x + w, self.w), min(y + h, self.h)
        if x1 <= x0 or y1 <= y0:
            return
        yy, uu, vv = self._yuv(c)
        row = bytes([yy]) * (x1 - x0)
        for j in range(y0, y1):
            self.y[j * self.w + x0:j * self.w + x1] = row
        cx0, cx1, cy0, cy1 = x0 >> 1, (x1 + 1) >> 1, y0 >> 1, (y1 + 1) >> 1
        urow, vrow = bytes([uu]) * (cx1 - cx0), bytes([vv]) * (cx1 - cx0)
        for j in range(cy0, cy1):
            self.u[j * self.cw + cx0:j * self.cw + cx1] = urow
            self.v[j * self.cw + cx0:j * self.cw + cx1] = vrow

    def text(self, x, y, s, fg, bg, sx=1, sy=1, maxchars=None):
        """Draw opaque text (cell background `bg`). Returns pixel width."""
        s = fold(s)
        if maxchars is not None:
            s = s[:maxchars]
        if x < 0:
            skip = (-x + 8 * sx - 1) // (8 * sx)
            s, x = s[skip:], x + skip * 8 * sx
        s = s[:max(0, (self.w - x) // (8 * sx))]
        if not s:
            return 0
        fgy, bgy = self._yuv(fg), self._yuv(bg)
        cells = [_yuv_glyph(ch, fgy, bgy, sx, sy) for ch in s]
        width = 8 * sx * len(s)
        for r in range(8 * sy):
            yy = y + r
            if 0 <= yy < self.h:
                self.y[yy * self.w + x:yy * self.w + x + width] = b''.join(c[0][r] for c in cells)
        cx, cwid = x >> 1, 4 * sx * len(s)
        for r in range(4 * sy):
            cy = (y >> 1) + r
            if 0 <= cy < self.ch:
                self.u[cy * self.cw + cx:cy * self.cw + cx + cwid] = b''.join(c[1][r] for c in cells)
                self.v[cy * self.cw + cx:cy * self.cw + cx + cwid] = b''.join(c[2][r] for c in cells)
        return width

    @staticmethod
    def text_width(s, sx=1):
        return len(s) * 8 * sx

    def blit(self, x, y, w, h, data):
        """Copy a planar YUV 4:2:0 picture (Y w*h, then U and V of ((w+1)//2) x ((h+1)//2) each), clipped to the canvas."""
        cw, ch = (w + 1) // 2, (h + 1) // 2
        for r in range(h):
            yy = y + r
            if 0 <= yy < self.h and x >= 0 and x + w <= self.w:
                self.y[yy * self.w + x:yy * self.w + x + w] = data[r * w:(r + 1) * w]
        base_u, base_v = w * h, w * h + cw * ch
        for r in range(ch):
            cy = (y >> 1) + r
            if 0 <= cy < self.ch and x >= 0 and x + w <= self.w:
                o = cy * self.cw + (x >> 1)
                n = min(cw, self.cw - (x >> 1))
                self.u[o:o + n] = data[base_u + r * cw:base_u + r * cw + n]
                self.v[o:o + n] = data[base_v + r * cw:base_v + r * cw + n]

    def frame(self, x, y, w, h, c, t=1):
        self.rect(x, y, w, t, c)
        self.rect(x, y + h - t, w, t, c)
        self.rect(x, y, t, h, c)
        self.rect(x + w - t, y, t, h, c)

    def to_rgb888(self, backdrop=(0, 0, 0)):
        """For viewing: BT.709 limited range -> RGB; transparent pixels show `backdrop`."""
        out = bytearray(self.w * self.h * 3)
        i = 0
        for j in range(self.h):
            for k in range(self.w):
                yy = self.y[j * self.w + k]
                if yy == 0:
                    out[i:i + 3] = bytes(backdrop)
                else:
                    ci = (j >> 1) * self.cw + (k >> 1)
                    c, d, e = yy - 16, self.u[ci] - 128, self.v[ci] - 128
                    if self.bt601:
                        r, g, b = (298 * c + 409 * e + 128) >> 8, (298 * c - 100 * d - 208 * e + 128) >> 8, (298 * c + 516 * d + 128) >> 8
                    else:
                        r, g, b = (298 * c + 459 * e + 128) >> 8, (298 * c - 55 * d - 136 * e + 128) >> 8, (298 * c + 541 * d + 128) >> 8
                    out[i:i + 3] = bytes((0 if r < 0 else 255 if r > 255 else r, 0 if g < 0 else 255 if g > 255 else g,
                                          0 if b < 0 else 255 if b > 255 else b))
                i += 3
        return bytes(out)

    def save_png(self, path, scale=3, backdrop=(0, 0, 0)):
        raw = self.to_rgb888(backdrop)
        rows = []
        for yy in range(self.h):
            line = raw[yy * self.w * 3:(yy + 1) * self.w * 3]
            if scale > 1:
                line = b''.join(line[i:i + 3] * scale for i in range(0, len(line), 3))
            rows.extend([b'\x00' + line] * scale)

        def chunk(tag, body):
            c = struct.pack('>I', len(body)) + tag + body
            return c + struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
        png = (b'\x89PNG\r\n\x1a\n' +
               chunk(b'IHDR', struct.pack('>IIBBBBB', self.w * scale, self.h * scale, 8, 2, 0, 0, 0)) +
               chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))
        with open(path, 'wb') as f:
            f.write(png)
