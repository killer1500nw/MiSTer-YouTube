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
