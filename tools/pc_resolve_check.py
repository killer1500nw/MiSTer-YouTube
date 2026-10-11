#!/usr/bin/env python3
"""Run the player's exact yt-dlp command and stream choice against real videos on a PC.

    python tools/pc_resolve_check.py PATH_TO_yt-dlp.exe PATH_TO_qjs.exe VIDEO_ID [VIDEO_ID ...]

Environment: QUALITY=Auto|240p|360p|480p (default Auto), COST=ARM seconds per video second of the reference stream
(default: the built-in calibration; lower = faster ARM).

Windows/Linux x86 builds of yt-dlp and QuickJS are enough: the command line, catalogue parsing and
stream choice are the same code the MiSTer uses. Shows which stream would be chosen and why.
Needs internet.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'YouTubeCRT'))
from ytcrt import pipeline, profiles  # noqa: E402


def main(ytdlp, qjs, ids, quality='Auto', cost=None):
    rt = type('Rt', (), {'resolver': Path(ytdlp), 'qjs': Path(qjs), 'ffdir': Path('.')})()
    prof = profiles.BY_KEY['240p30']
    for vid in ids:
        cmd = [str(x) for x in pipeline.resolve_command(rt, prof, 'https://www.youtube.com/watch?v=' + vid)]
        cmd = [c for c in cmd if c not in ('--compat-options', 'no-certifi')]      # Windows build uses certifi
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        print('=== %s  (%.1fs, %d bytes of output, exit %d)' % (vid, time.time() - t0, len(p.stdout), p.returncode))
        info = formats = None
        for line in p.stdout.splitlines():
            if line.startswith('{') and info is None:
                info = json.loads(line)
            elif line.startswith('[') and formats is None:
                formats = json.loads(line)
        if info is None or formats is None:
            print('   no stream info.', (p.stderr.strip().splitlines() or [''])[-1][:160])
            continue
        print('   %s [%s] %ss, %d formats' % (info.get('title', '')[:60], info.get('live_status'),
                                              info.get('duration'), len(formats)))
        try:
            cal = dict(profiles.CAL_DEFAULT, cost=cost) if cost else None
            m = pipeline.media_from(info, formats, prof, quality, cal)
            v, a = m.video, m.audio
            c = parse_qs(urlparse(v['url']).query).get('c', ['?'])[0]
            print('   -> %s: %sx%s %sfps %s kbps, est. load %d%% (format %s) | audio %s%s' %
                  (m.label, v['width'], v['height'], v['fps'], round(v.get('tbr') or 0), round(m.cost * 100), v['format_id'],
                   a['format_id'] if a else 'none', ('  [' + m.why + ']') if m.why else ''))
        except ValueError as exc:
            print('   refused:', exc)


if __name__ == '__main__':
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    c = os.environ.get('COST')
    main(sys.argv[1], sys.argv[2], sys.argv[3:], os.environ.get('QUALITY', 'Auto'), float(c) if c else None)
