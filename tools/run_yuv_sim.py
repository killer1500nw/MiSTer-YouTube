#!/usr/bin/env python3
"""Run the yuv_scaler RTL (ModelSim) on a picture and compare it pixel for pixel with the Python model.

    python tools/run_yuv_sim.py [case ...]        cases: see CASES; default: all

Needs ModelSim (the Quartus Lite one: set MODELSIM_BIN if it is not in the usual place). Prints PASS/FAIL per case.
"""
import os
import re
import random
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import yuv_ref  # noqa: E402
from ytcrt import yuv  # noqa: E402

MS = os.environ.get('MODELSIM_BIN', r'C:\intelFPGA_lite\17.0\modelsim_ase\win32aloem')
RTL = [ROOT / 'fpga' / 'rtl' / n for n in ('crt240.sv', 'linebuf.sv', 'linebuf64.sv', 'yuv_scaler.sv')]
TB = ROOT / 'fpga' / 'tests' / 'tb_yuv_scaler.sv'

# name: (width, height, crop, interlaced, flags, picture)
CASES = {
    'bars360':   (640, 360, 'Off', True, 0, 'bars'),
    'noise360':  (640, 360, 'Off', True, 0, 'noise'),
    'noise43':   (480, 360, 'Off', True, 0, 'noise'),       # 4:3: magnified 1.333x
    'fill':      (640, 360, 'Fill', True, 0, 'noise'),      # cropped sides, magnified
    'c149':      (640, 360, '14:9', True, yuv.FLAG_601, 'noise'),
    'small240':  (416, 240, 'Off', True, yuv.FLAG_FULL_RANGE, 'noise'),   # magnified 1.54x
    'tall':      (256, 480, 'Off', True, 0, 'noise'),       # vertical picture: pillarboxed
    'p240':      (640, 360, 'Off', False, 0, 'noise'),      # 240p output
    'dither':    (480, 360, 'Off', True, yuv.FLAG_DITHER, 'noise'),
    # with an overlay (GUI/OSD) drawn over the picture: the 7th item is the overlay buffer used
    'ov360':     (640, 360, 'Off', True, 0, 'noise', 2),
    'ov43':      (480, 360, 'Off', True, yuv.FLAG_601, 'noise', 0),
    'ovp240':    (640, 360, 'Fill', False, yuv.FLAG_DITHER, 'noise', 1),
    # the 480i flicker filter (8th item: its level 1..3)
    'flk1':      (640, 360, 'Off', True, 0, 'noise', None, 1),
    'flk2':      (480, 360, 'Off', True, 0, 'noise', None, 2),
    'flk3':      (640, 360, 'Off', True, yuv.FLAG_601, 'noise', 0, 3),
    # composite-style blur (9th item: level 1..3), here together with an overlay, dither and colour options
    'soft1':     (640, 360, 'Off', True, 0, 'noise', None, 0, 1),
    'soft2':     (480, 360, 'Off', True, yuv.FLAG_DITHER, 'noise', 1, 0, 2),
    'soft3':     (640, 360, 'Off', True, yuv.FLAG_601, 'bars', 0, 2, 3),
    'soft1p':    (640, 360, 'Fill', False, 0, 'noise', 2, 0, 1),
    'soft2p':    (640, 360, 'Off', False, yuv.FLAG_DITHER, 'noise', None, 0, 2),
    'soft3p':    (480, 360, 'Off', False, yuv.FLAG_FULL_RANGE, 'bars', 0, 0, 3),
    # the picture planes found by the physical address in header word 6 (10th item): a slot other than the record's own
    'pic360':    (640, 360, 'Off', True, 0, 'noise', 1, 0, 0, True),
    'pic240p':   (480, 360, 'Fill', False, yuv.FLAG_601, 'noise', None, 0, 0, True),
}


def make_overlay(seed):
    """A 320x240 overlay: transparent (luma 0) with opaque boxes of random colour, a band along the bottom (outside the picture
    in the letterboxed cases) and a frame line, so every plane and edge is exercised."""
    rng = random.Random(seed)
    oy = bytearray(yuv.OV_W * yuv.OV_H)
    ou = bytearray([128]) * (yuv.OV_W * yuv.OV_H // 4)
    ov = bytearray([128]) * (yuv.OV_W * yuv.OV_H // 4)

    def box(x0, y0, w, h, yy, uu, vv):
        x1 = min(yuv.OV_W, x0 + w)
        for j in range(y0, min(yuv.OV_H, y0 + h)):
            oy[j * yuv.OV_W + x0:j * yuv.OV_W + x1] = bytes([yy]) * (x1 - x0)
        for j in range(y0 // 2, min(yuv.OV_H // 2, (y0 + h + 1) // 2)):
            for i in range(x0 // 2, min(yuv.OV_W // 2, (x0 + w + 1) // 2)):
                ou[j * (yuv.OV_W // 2) + i], ov[j * (yuv.OV_W // 2) + i] = uu, vv
    for _ in range(14):
        box(rng.randrange(0, 300), rng.randrange(0, 220), rng.randrange(8, 70), rng.randrange(8, 50),
            rng.randrange(1, 255), rng.randrange(0, 256), rng.randrange(0, 256))
    box(8, 214, 304, 18, 40, 128, 128)                  # a dark band near the bottom (the OSD bar)
    box(0, 0, 320, 2, 235, 128, 128)                    # a frame line at the very top edge
    return bytes(oy), bytes(ou), bytes(ov)


def overlay_image(oy, ou, ov):
    img = bytearray(yuv.OVERLAY_BYTES)
    img[yuv.OV_Y_OFF:yuv.OV_Y_OFF + len(oy)] = oy
    img[yuv.OV_U_OFF:yuv.OV_U_OFF + len(ou)] = ou
    img[yuv.OV_V_OFF:yuv.OV_V_OFF + len(ov)] = ov
    return bytes(img)


def make_picture(kind, w, h, seed=1):
    if kind == 'bars':
        return yuv_ref.test_picture(w, h)
    rng = random.Random(seed)
    # smooth-ish noise: random but not all-extremes, so saturation and plain values both occur
    y = bytes(rng.randrange(16, 236) if rng.random() < 0.9 else rng.randrange(0, 256) for _ in range(w * h))
    u = bytes(rng.randrange(0, 256) for _ in range(w * h // 4))
    v = bytes(rng.randrange(0, 256) for _ in range(w * h // 4))
    return y, u, v


def slot_image(header, y, u, v):
    img = bytearray(yuv.SLOT_BYTES)
    img[0:64] = header
    img[yuv.Y_OFF:yuv.Y_OFF + len(y)] = y
    img[yuv.U_OFF:yuv.U_OFF + len(u)] = u
    img[yuv.V_OFF:yuv.V_OFF + len(v)] = v
    return bytes(img)


def run_case(name, work):
    w, h, crop, interlaced, flags, kind = CASES[name][:6]
    ov_index = CASES[name][6] if len(CASES[name]) > 6 else None
    flicker = CASES[name][7] if len(CASES[name]) > 7 else 0
    soft = CASES[name][8] if len(CASES[name]) > 8 else 0
    by_address = bool(CASES[name][9]) if len(CASES[name]) > 9 else False
    y, u, v = make_picture(kind, w, h)
    header = yuv.header_for(w, h, crop, interlaced, flags, ov_index, None, (0x30000000 + 0x1000 + 3 * 0x80000) if by_address else None)
    hdr = yuv.parse_header(header)
    img = slot_image(header, y, u, v)
    words = struct.unpack('<%dQ' % (len(img) // 8), img)
    (work / 'slot.hex').write_text('\n'.join('%016x' % wd for wd in words) + '\n')
    overlay = None
    ovimg = bytearray(3 * yuv.OVERLAY_BYTES)
    if ov_index is not None:
        overlay = make_overlay(7 + ov_index)
        ovimg[ov_index * yuv.OVERLAY_BYTES:(ov_index + 1) * yuv.OVERLAY_BYTES] = overlay_image(*overlay)
    (work / 'overlay.hex').write_text('\n'.join('%016x' % wd for wd in struct.unpack('<%dQ' % (len(ovimg) // 8), bytes(ovimg))) + '\n')
    env = dict(os.environ)
    env['PATH'] = MS + os.pathsep + env['PATH']
    subprocess.run(['vlib', 'w'], cwd=work, check=True, capture_output=True, env=env)
    cmd = ['vlog', '-sv', '-quiet', '-work', 'w'] + [str(p) for p in RTL] + [str(TB)]
    r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, env=env)
    if r.returncode or 'Error' in r.stdout:
        return 'FAIL compile: ' + (r.stdout + r.stderr)[-1500:]
    r = subprocess.run(['vsim', '-c', '-work', 'w', '+IL=%d' % interlaced, '+FLK=%d' % flicker, '+SOFT=%d' % soft, '+SLOT=%d' % (3 if by_address else 0), '+PIC=%d' % int(by_address), '-do', 'run -all; quit -f', 'tb_yuv_scaler'],
                       cwd=work, capture_output=True, text=True, env=env, timeout=1800)
    if 'DONE' not in r.stdout:
        return 'FAIL sim: ' + r.stdout[-1500:]
    m = re.search(r'hold_bad=(\d+)', r.stdout)
    if m and int(m.group(1)):
        return 'FAIL %s: the output changed within a pixel period %s times (the analogue output needs it held)' % (name, m.group(1))
    expected = yuv_ref.render_frame(y, u, v, hdr, interlaced, overlay, flicker, soft)
    seen = bad = 0
    first = None
    for line in (work / 'out.txt').read_text().splitlines():
        parts = line.split()
        d, pix = int(parts[0]), parts[1:]
        seen += 1
        for x, p in enumerate(pix):
            if not interlaced and x % 2:                    # 240p: the core outputs every second pixel of the 640-wide frame
                continue
            got = int(p, 16)
            exp = expected[d][x]
            want = (exp[0] << 16) | (exp[1] << 8) | exp[2]
            if got != want:
                bad += 1
                if first is None:
                    first = (d, x, '%06x' % got, '%06x' % want)
    need = 480 if interlaced else 240
    if seen != need:
        return 'FAIL %s: captured %d lines, wanted %d' % (name, seen, need)
    if bad:
        return 'FAIL %s: %d of %d pixels differ; first at line %d x %d: got %s expected %s' % (
            name, bad, seen * (640 if interlaced else 320), first[0], first[1], first[2], first[3])
    return 'PASS %s: %d lines x %d pixels identical to the model' % (name, seen, 640 if interlaced else 320)


def main(argv):
    names = argv[1:] or list(CASES)
    ok = True
    for name in names:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_case(name, Path(tmp))
        print(result)
        ok &= result.startswith('PASS')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
