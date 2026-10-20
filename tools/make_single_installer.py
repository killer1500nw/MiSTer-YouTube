"""Build release/Install_YouTube_CRT.sh: ONE self-contained script (the small installer zip is embedded in it).

Copy that single file to /media/fat/Scripts/ and run it from the MiSTer Scripts menu. Run make_sd_package.py first.
"""
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / 'release' / 'YouTubeCRT-installer.zip'
OUT = ROOT / 'release' / 'Install_YouTube_CRT.sh'

HEAD = r'''#!/bin/bash
# Self-contained YouTube CRT installer: the player and the core are inside this one file.
# Copy it to /media/fat/Scripts/ and run it from the MiSTer Scripts menu. Your videos list and settings are left alone.
set -Eeuo pipefail
echo "Unpacking the YouTube CRT files..."
python3 - "$0" <<'PY'
import base64, io, os, sys, tempfile, zipfile
from pathlib import PurePosixPath
text = open(sys.argv[1], 'rb').read().decode('ascii', 'ignore')
blob = text.split('\n#ZIP-BEGIN\n', 1)[1].split('\n#ZIP-END', 1)[0]
z = zipfile.ZipFile(io.BytesIO(base64.b64decode(''.join(l[1:] for l in blob.split('\n') if l.startswith('#')))))
n = 0
# Validate the full payload before writing any files.
if z.testzip() is not None:
    raise SystemExit('Installer payload is corrupt.')
for info in z.infolist():
    path = PurePosixPath(info.filename)
    if path.is_absolute() or '..' in path.parts or not info.filename.startswith(
            ('Scripts/.config/YouTubeCRT/', 'Scripts/YouTubeCRT/')):
        raise SystemExit('Unexpected installer path: ' + info.filename)
# Preserve an older visible runtime (including any local files) before replacing
# its menu folder with launchers. Re-running a launcher-only install needs no backup.
legacy = '/media/fat/Scripts/YouTubeCRT'
if os.path.isfile(legacy + '/youtube_crt.py') and os.path.isdir(legacy + '/ytcrt'):
    config = '/media/fat/Scripts/.config'
    os.makedirs(config, exist_ok=True)
    backup = tempfile.mkdtemp(prefix='YouTubeCRT-legacy-', dir=config)
    os.rename(legacy, backup + '/YouTubeCRT')
    print('Previous runtime preserved in ' + backup)
for info in z.infolist():
    name = info.filename
    if info.is_dir() or '__pycache__' in name or not name.lower().startswith(('scripts/', '_other/')):
        continue
    target = '/media/fat/' + name
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with z.open(info) as src, open(target, 'wb') as dst:
        dst.write(src.read())
    n += 1
print('Unpacked %d files.' % n)
PY
chmod +x /media/fat/Scripts/.config/YouTubeCRT/*.sh /media/fat/Scripts/YouTubeCRT/*.sh 2>/dev/null || true
bash /media/fat/Scripts/.config/YouTubeCRT/Install_YouTube_CRT.sh
exit $?
'''


def main():
    if not ZIP.is_file():
        sys.exit('Run tools/make_sd_package.py first.')
    b64 = base64.b64encode(ZIP.read_bytes()).decode('ascii')
    lines = ['#' + b64[i:i + 100] for i in range(0, len(b64), 100)]
    OUT.write_bytes((HEAD + '#ZIP-BEGIN\n' + '\n'.join(lines) + '\n#ZIP-END\n').encode('ascii'))
    print('Wrote %s (%.1f MB)' % (OUT, OUT.stat().st_size / 1e6))


main()
