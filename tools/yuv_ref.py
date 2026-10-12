#!/usr/bin/env python3
"""Reference model of the v0.15 core's scaler (docs/FPGA-YUV-v0.15.md): bit-exact, pure Python.

    python tools/yuv_ref.py out.ppm [WxH]       render a test picture the way the core would draw it

The RTL testbenches compare their output pixel for pixel against `render_frame`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'YouTubeCRT'))
from ytcrt import yuv  # noqa: E402

BAYER = ((0, 2, 0, 2), (3, 1, 3, 1), (0, 2, 0, 2), (3, 1, 3, 1))

# (y gain, Cr->R, Cb->G, Cr->G, Cb->B, y offset), all x256
COEF = {
    (False, False): (298, 459, 55, 136, 541, 16),      # BT.709 limited
    (True, False): (298, 409, 100, 208, 516, 16),      # BT.601 limited
    (False, True): (256, 403, 48, 120, 475, 0),        # BT.709 full
    (True, True): (256, 359, 88, 183, 454, 0),         # BT.601 full
}


def lerp(a, b, f):
    return (a * (256 - f) + b * f + 128) >> 8


def sat(v):
    return 0 if v < 0 else 255 if v > 255 else v


def colour(y, u, v, flags):
    gain, rv, gu, gv, bu, off = COEF[(bool(flags & yuv.FLAG_601), bool(flags & yuv.FLAG_FULL_RANGE))]
    c, d, e = y - off, u - 128, v - 128
    return (sat((gain * c + rv * e + 128) >> 8),
            sat((gain * c - gu * d - gv * e + 128) >> 8),
            sat((gain * c + bu * d + 128) >> 8))


def split(coord, size):
    """16.16 source coordinate -> (index0, index1, 8-bit fraction), both indices clamped into the plane."""
    i = coord >> 16
    return min(i, size - 1), min(i + 1, size - 1), (coord >> 8) & 255


def render_frame(y, u, v, hdr, interlaced=True, overlay=None):
    """The 640 x 480 frame the core draws from a decoded picture: list of 480 lines, each 640 (r, g, b) tuples.

    y/u/v are the plane bytes exactly as stored (strides from the header). In 480i field `f` shows lines f::2; in 240p
    the core shows lines 0::2. `overlay` = (oy, ou, ov) planes of the 320x240 overlay (luma 0 is transparent), used when
    the header enables it: each overlay pixel covers 2x2 frame pixels."""
    w, h = hdr['src_w'], hdr['src_h']
    cw, chh = (w + 1) // 2, (h + 1) // 2
    ys, cs, flags = hdr['ystride'], hdr['cstride'], hdr['flags']
    dither = bool(flags & yuv.FLAG_DITHER)
    xs = []
    for x in range(yuv.FRAME_W):
        if hdr['dx0'] <= x < hdr['dx0'] + hdr['dw']:
            sx = (hdr['sx0'] + (x - hdr['dx0']) * hdr['hstep']) & 0xFFFFFFFF
            xs.append((split(sx, w), split(sx >> 1, cw)))
        else:
            xs.append(None)
    black = (0, 0, 0)
    frame = []
    use_ov = overlay is not None and hdr.get('ov_en')
    for d in range(yuv.FRAME_H):
        if not hdr['dy0'] <= d < hdr['dy0'] + hdr['dh']:
            line = [black] * yuv.FRAME_W
            if use_ov:
                _overlay_line(line, overlay, d, flags, dither)
            frame.append(line)
            continue
        sy = (hdr['sy0'] + (d - hdr['dy0']) * hdr['vstep']) & 0xFFFFFFFF
        r0, r1, fy = split(sy, h)
        csy = max(0, (sy >> 1) - 0x4000)
        c0, c1, fcy = split(csy, chh)
        line = []
        for x in range(yuv.FRAME_W):
            if xs[x] is None:
                line.append(black)
                continue
            (x0, x1, fx), (cx0, cx1, fcx) = xs[x]
            ya = lerp(y[r0 * ys + x0], y[r0 * ys + x1], fx)
            yb = lerp(y[r1 * ys + x0], y[r1 * ys + x1], fx)
            yy = lerp(ya, yb, fy)
            ua = lerp(u[c0 * cs + cx0], u[c0 * cs + cx1], fcx)
            ub = lerp(u[c1 * cs + cx0], u[c1 * cs + cx1], fcx)
            uu = lerp(ua, ub, fcy)
            va = lerp(v[c0 * cs + cx0], v[c0 * cs + cx1], fcx)
            vb = lerp(v[c1 * cs + cx0], v[c1 * cs + cx1], fcx)
            vv = lerp(va, vb, fcy)
            r, g, b = colour(yy, uu, vv, flags)
            if dither:
                t = BAYER[d & 3][x & 3]
                r, g, b = min(255, r + t) & 0xFC, min(255, g + t) & 0xFC, min(255, b + t) & 0xFC
            line.append((r, g, b))
        if use_ov:
            _overlay_line(line, overlay, d, flags, dither)
        frame.append(line)
    return frame


def _overlay_line(line, overlay, d, flags, dither):
    """Draw the overlay over one finished frame line (same colour matrix and dither as the picture)."""
    oy, ou, ov = overlay
    row = d >> 1
    for x in range(yuv.FRAME_W):
        yy = oy[row * yuv.OV_W + (x >> 1)]
        if yy == 0:
            continue
        ci = (row >> 1) * (yuv.OV_W // 2) + (x >> 2)
        r, g, b = colour(yy, ou[ci], ov[ci], flags)
        if dither:
            t = BAYER[d & 3][x & 3]
            r, g, b = min(255, r + t) & 0xFC, min(255, g + t) & 0xFC, min(255, b + t) & 0xFC
        line[x] = (r, g, b)


def planes_from_function(w, h, fn):
    """Build y/u/v bytes from fn(x, y) -> (Y, U, V) sampled at luma resolution (chroma takes every even sample)."""
    yp, up, vp = bytearray(w * h), bytearray(w * h // 4), bytearray(w * h // 4)
    for j in range(h):
        for i in range(w):
            yy, uu, vv = fn(i, j)
            yp[j * w + i] = yy
            if i % 2 == 0 and j % 2 == 0:
                up[(j // 2) * (w // 2) + i // 2], vp[(j // 2) * (w // 2) + i // 2] = uu, vv
    return bytes(yp), bytes(up), bytes(vp)


def test_picture(w, h):
    """Colour bars over a grey ramp: easy to eyeball and hits every plane."""
    bars = [(235, 128, 128), (210, 16, 146), (170, 166, 16), (145, 54, 34), (106, 202, 222), (81, 90, 240), (41, 240, 110), (16, 128, 128)]

    def fn(i, j):
        if j < h * 3 // 4:
            return bars[min(7, i * 8 // w)]
        return (16 + (i * 219) // w, 128, 128)
    return planes_from_function(w, h, fn)


def main(argv):
    out = argv[1] if len(argv) > 1 else 'yuv_ref.ppm'
    w, h = (int(n) for n in (argv[2] if len(argv) > 2 else '640x360').split('x'))
    y, u, v = test_picture(w, h)
    hdr = yuv.parse_header(yuv.header_for(w, h))
    frame = render_frame(y, u, v, hdr)
    with open(out, 'wb') as f:
        f.write(b'P6 %d %d 255\n' % (yuv.FRAME_W, yuv.FRAME_H))
        for line in frame:
            f.write(bytes(c for px in line for c in px))
    print('wrote', out)


if __name__ == '__main__':
    main(sys.argv)
