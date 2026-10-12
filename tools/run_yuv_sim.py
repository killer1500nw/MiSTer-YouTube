#!/usr/bin/env python3
"""Run the yuv_scaler RTL (ModelSim) on a picture and compare it pixel for pixel with the Python model.

    python tools/run_yuv_sim.py [case ...]        cases: see CASES; default: all

Needs ModelSim (the Quartus Lite one: set MODELSIM_BIN if it is not in the usual place). Prints PASS/FAIL per case.
"""
import os
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
RTL = [ROOT / 'fpga' / 'rtl' / n for n in ('crt240.sv', 'linebuf.sv', 'yuv_scaler.sv')]
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
}


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
    w, h, crop, interlaced, flags, kind = CASES[name]
    y, u, v = make_picture(kind, w, h)
    header = yuv.header_for(w, h, crop, interlaced, flags)
    hdr = yuv.parse_header(header)
    img = slot_image(header, y, u, v)
    words = struct.unpack('<%dQ' % (len(img) // 8), img)
    (work / 'slot.hex').write_text('\n'.join('%016x' % wd for wd in words) + '\n')
    env = dict(os.environ)
    env['PATH'] = MS + os.pathsep + env['PATH']
    subprocess.run(['vlib', 'w'], cwd=work, check=True, capture_output=True, env=env)
    cmd = ['vlog', '-sv', '-quiet', '-work', 'w'] + [str(p) for p in RTL] + [str(TB)]
    r = subprocess.run(cmd, cwd=work, capture_output=True, text=True, env=env)
    if r.returncode or 'Error' in r.stdout:
        return 'FAIL compile: ' + (r.stdout + r.stderr)[-1500:]
    r = subprocess.run(['vsim', '-c', '-work', 'w', '+IL=%d' % interlaced, '-do', 'run -all; quit -f', 'tb_yuv_scaler'],
                       cwd=work, capture_output=True, text=True, env=env, timeout=1800)
    if 'DONE' not in r.stdout:
        return 'FAIL sim: ' + r.stdout[-1500:]
    expected = yuv_ref.render_frame(y, u, v, hdr, interlaced)
    seen = bad = 0
    first = None
    for line in (work / 'out.txt').read_text().splitlines():
        parts = line.split()
        d, pix = int(parts[0]), parts[1:]
        seen += 1
        for x, p in enumerate(pix):
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
            name, bad, seen * 640, first[0], first[1], first[2], first[3])
    return 'PASS %s: %d lines x 640 pixels identical to the model' % (name, seen)


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
