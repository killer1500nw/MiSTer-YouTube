#!/usr/bin/env python3
"""Run the whole v0.15 core (ModelSim) against a player-like DDR model and check what comes out of its pins.

    python tools/run_core_sim.py [480i|240p ...]

For each record the video lines at the VGA pins must equal the Python model (tools/yuv_ref.py), the PCM must come out in
order, and the mailbox must end with the right acknowledgement (one record behind the commit count), played count,
session and identity.
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
import run_yuv_sim  # noqa: E402
from ytcrt import yuv  # noqa: E402

MS = os.environ.get('MODELSIM_BIN', r'C:\intelFPGA_lite\17.0\modelsim_ase\win32aloem')
F = ROOT / 'fpga'
SRC = [F / 'rtl' / n for n in ('crt240.sv', 'linebuf.sv', 'linebuf64.sv', 'yuv_scaler.sv', 'ddr_arb.sv', 'ddr_stream.sv', 'movie_audio.sv')] + \
      [F / 'YouTubeCRT.sv', F / 'tests' / 'sim_stubs.sv', F / 'tests' / 'tb_core.sv']

# width, height, crop, flags, overlay buffer used (or None)
GEOMETRIES = [(640, 360, 'Off', 0, None), (480, 360, 'Off', 0, 0), (640, 360, 'Fill', yuv.FLAG_601, None),
              (416, 240, 'Off', 0, 2), (640, 360, 'Off', yuv.FLAG_FULL_RANGE, 1), (480, 360, 'Off', yuv.FLAG_DITHER, 0)]
OVERLAYS = {i: run_yuv_sim.make_overlay(40 + i) for i in range(3)}


def audio_for(index):
    out = bytearray()
    for n in range(1600):
        left = (index * 2000 + n) & 0x7FFF
        out += struct.pack('<hh', left, -(left + 1))
    return bytes(out)


REPEATS = {3: 2, 5: 4}
BY_ADDRESS = {5}                  # ...and record 5 names the picture by its physical address (header word 6) instead of by slot          # record -> the earlier record whose picture it shows again (header and audio only, other overlay)


def make_record(index, interlaced, again=None):
    w, h, crop, flags, ov = GEOMETRIES[index % len(GEOMETRIES)]
    if again is not None:                                    # a repeat: the referenced slot's planes, this record's own header
        w, h, crop, flags, _ = GEOMETRIES[again % len(GEOMETRIES)]
        ov = (GEOMETRIES[again % len(GEOMETRIES)][4] or 0) + 1 if (GEOMETRIES[again % len(GEOMETRIES)][4] or 0) < 2 else 0
        y, u, v = run_yuv_sim.make_picture('noise', w, h, seed=100 + again)
        by_addr = index in BY_ADDRESS
        header = yuv.header_for(w, h, crop, interlaced, flags, ov, None if by_addr else again % 8,
                                (0x30000000 + 0x1000 + (again % 8) * 0x80000) if by_addr else None)
        img = bytearray(yuv.SLOT_BYTES)
        img[0:64] = header
        img[yuv.AUDIO_OFF:yuv.AUDIO_OFF + yuv.AUDIO_BYTES] = audio_for(index)
        return bytes(img), (y, u, v, yuv.parse_header(header), OVERLAYS[ov])
    y, u, v = run_yuv_sim.make_picture('noise', w, h, seed=100 + index)
    header = yuv.header_for(w, h, crop, interlaced, flags, ov)
    img = bytearray(yuv.SLOT_BYTES)
    img[0:64] = header
    img[yuv.Y_OFF:yuv.Y_OFF + len(y)] = y
    img[yuv.U_OFF:yuv.U_OFF + len(u)] = u
    img[yuv.V_OFF:yuv.V_OFF + len(v)] = v
    img[yuv.AUDIO_OFF:yuv.AUDIO_OFF + yuv.AUDIO_BYTES] = audio_for(index)
    return bytes(img), (y, u, v, yuv.parse_header(header), OVERLAYS[ov] if ov is not None else None)


def words(img):
    return struct.unpack('<%dQ' % (len(img) // 8), img)


def write_hex(path, regions):
    with open(path, 'w') as f:
        for start, img in regions:
            f.write('@%x\n' % start)
            f.write('\n'.join('%016x' % w for w in words(img)) + '\n')


def run(mode, nrec, nlate, work):
    interlaced = mode == '480i'
    records = [make_record(i, interlaced, REPEATS.get(i) if i < nrec else None) for i in range(nrec + nlate)]
    regions = [(512 + i * 65536, records[i][0]) for i in range(nrec)]
    for idx, planes in OVERLAYS.items():                 # the three overlay buffers sit after the eight slots
        regions.append((0x80200 + idx * 16384, run_yuv_sim.overlay_image(*planes)))
    write_hex(work / 'mem.hex', regions)
    if nlate:
        with open(work / 'late.hex', 'w') as f:
            for i in range(nlate):
                f.write('@%x\n' % (i * 65536))
                f.write('\n'.join('%016x' % w for w in words(records[nrec + i][0])) + '\n')
    env = dict(os.environ)
    env['PATH'] = MS + os.pathsep + env['PATH']
    subprocess.run(['vlib', 'w'], cwd=work, check=True, capture_output=True, env=env)
    r = subprocess.run(['vlog', '-sv', '-quiet', '-work', 'w', '+incdir+' + str(F)] + [str(p) for p in SRC],
                       cwd=work, capture_output=True, text=True, env=env)
    if r.returncode or 'Error' in r.stdout:
        return 'FAIL compile: ' + (r.stdout + r.stderr)[-2500:]
    r = subprocess.run(['vsim', '-c', '-work', 'w', '+IL=%d' % interlaced, '+NREC=%d' % nrec, '+LATE=%d' % nlate,
                        '-do', 'run -all; quit -f', 'tb_core'], cwd=work, capture_output=True, text=True, env=env, timeout=7200)
    if 'DONE' not in r.stdout:
        return 'FAIL sim: ' + r.stdout[-2500:]
    problems = []
    header = (work / 'core_out.txt').read_text().split()
    stat = {header[i]: header[i + 1] for i in range(0, len(header) - 1, 2)}
    total = nrec + nlate
    if int(stat['commits']) != total:
        problems.append('commits %s != %d' % (stat['commits'], total))
    if int(stat['ack']) != total - 1:
        problems.append('ack %s != %d (one behind the commit count)' % (stat['ack'], total - 1))
    if int(stat['played']) != total:
        problems.append('played %s != %d' % (stat['played'], total))
    if int(stat['session']) != 77 or stat['ident'] != '36445459' or int(stat['error']) != 0:
        problems.append('session/identity/error: %s' % stat)
    # video: every record's 480 (or 240) lines must be right
    expected = {}
    seen = {}
    bad = 0
    first_bad = None
    for line in (work / 'video.txt').read_text().splitlines():
        p = line.split()
        rec, fld, ln = int(p[0]), int(p[1]), int(p[2])
        d = ln * 2 + (fld if interlaced else 0)
        if rec not in expected:
            y, u, v, hdr, ov = records[rec][1]
            expected[rec] = yuv_ref.render_frame(y, u, v, hdr, interlaced, ov)
            seen[rec] = set()
        for x, px in enumerate(p[3:]):
            if not interlaced and x >= 320:                 # 240p has 320 pixels per line: every second pixel of the 640-wide frame
                break
            e = expected[rec][d][x if interlaced else 2 * x]
            if int(px, 16) != (e[0] << 16 | e[1] << 8 | e[2]):
                bad += 1
                if first_bad is None:
                    first_bad = (rec, d, x, px)
        seen[rec].add(d)
    want_lines = 480 if interlaced else 240
    for rec in range(total):
        got = len(seen.get(rec, ()))
        if got != want_lines:
            problems.append('record %d: %d of %d lines shown' % (rec, got, want_lines))
    if bad:
        problems.append('%d pixels differ; first record %d line %d x %d got %s' % ((bad,) + first_bad))
    # audio
    per = {}
    for line in (work / 'audio.txt').read_text().splitlines():
        p = line.split()
        if len(p) == 3:
            per.setdefault(int(p[0]), []).append((int(p[1]), int(p[2])))
    for rec in range(total):
        exp = struct.unpack('<%dh' % 3200, audio_for(rec))
        want = [(exp[2 * n] & 0xFFFF, exp[2 * n + 1] & 0xFFFF) for n in range(1600)]
        got = [(a & 0xFFFF, b & 0xFFFF) for a, b in per.get(rec, [])]
        # the sample before the first one of a record may differ; compare from the first sample onwards
        if want[0] in got:
            got = got[got.index(want[0]):]
        n = min(len(got), 1590)
        if n < 1590 or got[:n] != want[:n]:
            problems.append('record %d audio: %d samples, first mismatch %s' % (
                rec, len(got), next((i for i in range(n) if got[i] != want[i]), None)))
    if problems:
        return 'FAIL %s: ' % mode + '; '.join(problems)
    return 'PASS %s: %d records: video identical to the model, PCM in order, ack %d, played %d, session/identity ok' % (
        mode, total, int(stat['ack']), int(stat['played']))


def main(argv):
    modes = argv[1:] or ['480i', '240p']
    ok = True
    for m in modes:
        with tempfile.TemporaryDirectory() as tmp:
            res = run(m, 6, 3 if m == '480i' else 2, Path(tmp))
        print(res)
        ok &= res.startswith('PASS')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
