#!/usr/bin/env python3
"""Assemble the SD-card package: copy the contents of release/SD_COPY to the root of the SD card.

    python tools/make_sd_package.py

Inputs: the built v0.16 core (fpga/output_files/YouTubeCRT.rbf, which must be newer than the RTL),
scripts/YouTubeCRT, and the video tools already unpacked in release/SD_COPY/YouTubeNative
(yt-dlp, QuickJS, ffmpeg ARM builds; see release/SD_COPY/YouTubeNative/TOOLS.txt).
Output: release/YouTubeCRT-installer.zip (the input of make_single_installer.py, which makes the one-file release installer)
"""
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'release' / 'SD_COPY'
CORE = 'YouTubeCRT_v0.16-fb.rbf'
RUNTIME = Path('Scripts/.config/YouTubeCRT')
MENU_SCRIPTS = ('Install_YouTube_CRT.sh', 'Install_H264_Core.sh', 'YouTube_H264.sh',
                'Status_YouTube_CRT.sh', 'Where_Is_The_Log.sh', 'Test_Node_JS.sh',
                'Uninstall_YouTube_CRT.sh')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in ('_Other', 'Scripts'):
        shutil.rmtree(OUT / sub, ignore_errors=True)
    built = ROOT / 'fpga' / 'output_files' / 'YouTubeCRT.rbf'
    kept = ROOT / 'release' / CORE                          # the last good v0.16 build, kept in the repository
    sources = [ROOT / 'fpga' / 'YouTubeCRT.sv'] + list((ROOT / 'fpga' / 'rtl').glob('*.sv'))
    if built.is_file() and built.stat().st_mtime >= max(p.stat().st_mtime for p in sources):
        core = built
        shutil.copyfile(built, kept)
    elif kept.is_file():
        core = kept
        print('NOTE: the RTL is newer than the last build; packaging the kept core release/%s' % CORE)
    else:
        sys.exit('No v0.16 core built yet: run the Quartus compile in fpga/ first.')
    (OUT / '_Other').mkdir(parents=True)
    shutil.copyfile(core, OUT / '_Other' / CORE)
    # The folder people copy to the SD card carries the cores too (the installer puts them in _Other); keep it current.
    shutil.copyfile(core, ROOT / 'scripts' / 'YouTubeCRT' / CORE)
    scripts = OUT / RUNTIME
    shutil.copytree(ROOT / 'scripts' / 'YouTubeCRT', scripts,
                    ignore=shutil.ignore_patterns('__pycache__', '*.rbf', '*.pyc', '*.txt.tmp', 'YouTube-*log*.txt'))
    import re
    ver = re.search(r"__version__ = '([^']*)'", (ROOT / 'scripts' / 'YouTubeCRT' / 'ytcrt' / '__init__.py').read_text()).group(1)
    (scripts / 'VERSION.txt').write_text(ver + '\n')
    (ROOT / 'scripts' / 'YouTubeCRT' / 'VERSION.txt').write_text(ver + '\n')   # keep the source folder in sync too
    for f in scripts.glob('*.sh'):                      # SD cards want LF endings
        f.write_bytes(f.read_bytes().replace(b'\r\n', b'\n'))
    # Only small user-facing launchers belong in the visible Scripts menu.
    menu = OUT / 'Scripts' / 'YouTubeCRT'
    menu.mkdir(parents=True)
    for name in MENU_SCRIPTS:
        launcher = menu / name
        launcher.write_text('#!/bin/bash\n# YouTube CRT menu launcher; runtime lives in Scripts/.config.\n'
                            'exec bash /media/fat/Scripts/.config/YouTubeCRT/%s "$@"\n' % name)
        launcher.chmod(0o755)
    # The one-file installer embeds the runtime, launchers and current core.
    small = ROOT / 'release' / 'YouTubeCRT-installer.zip'
    with zipfile.ZipFile(small, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted((OUT / 'Scripts').rglob('*')):
            if p.is_file():
                z.write(p, p.relative_to(OUT).as_posix())
        z.write(core, (RUNTIME / CORE).as_posix())
    print('Core:', core)
    print('Small installer: %s (%.1f MB)' % (small, small.stat().st_size / 1e6))


if __name__ == '__main__':
    main()
