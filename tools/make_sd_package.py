#!/usr/bin/env python3
"""Assemble the SD-card package: copy the contents of release/SD_COPY to the root of the SD card.

    python tools/make_sd_package.py

Inputs: the built v0.16 core (fpga/output_files/YouTubeCRT.rbf, which must be newer than the RTL), the older
v0.15-yuv and v0.14-auto cores (release/*.rbf),
scripts/YouTubeCRT, and the video tools already unpacked in release/SD_COPY/YouTubeNative
(yt-dlp, QuickJS, ffmpeg ARM builds; see release/SD_COPY/YouTubeNative/TOOLS.txt).
Output: release/SD_COPY/ and release/MiSTer-YouTube-SD.zip
"""
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'release' / 'SD_COPY'
CORE = 'YouTubeCRT_v0.16-fb.rbf'
LEGACY = ('YouTubeCRT_v0.15-yuv.rbf', 'YouTubeCRT_v0.14-auto.rbf')      # the older cores the same player still drives (fallbacks)


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
    legacy = [ROOT / 'release' / name for name in LEGACY if (ROOT / 'release' / name).is_file()]
    for path in legacy:
        shutil.copyfile(path, OUT / '_Other' / path.name)
    # The folder people copy to the SD card carries the cores too (the installer puts them in _Other); keep it current.
    shutil.copyfile(core, ROOT / 'scripts' / 'YouTubeCRT' / CORE)
    for path in legacy:
        shutil.copyfile(path, ROOT / 'scripts' / 'YouTubeCRT' / path.name)
    scripts = OUT / 'Scripts' / 'YouTubeCRT'
    shutil.copytree(ROOT / 'scripts' / 'YouTubeCRT', scripts,
                    ignore=shutil.ignore_patterns('__pycache__', '*.rbf', '*.pyc', '*.txt.tmp', 'YouTube-*log*.txt'))
    import re
    ver = re.search(r"__version__ = '([^']*)'", (ROOT / 'scripts' / 'YouTubeCRT' / 'ytcrt' / '__init__.py').read_text()).group(1)
    (scripts / 'VERSION.txt').write_text(ver + '\n')
    (ROOT / 'scripts' / 'YouTubeCRT' / 'VERSION.txt').write_text(ver + '\n')   # keep the source folder in sync too
    for f in scripts.glob('*.sh'):                      # SD cards want LF endings
        f.write_bytes(f.read_bytes().replace(b'\r\n', b'\n'))
    (OUT / 'README.txt').write_text(
        'YouTube CRT: copy everything in this folder to the ROOT of your MiSTer SD card\n'
        '(it merges with your existing _Other, Scripts and YouTubeNative folders).\n\n'
        '1. Put the SD card back in the MiSTer and boot it.\n'
        '2. Scripts -> YouTubeCRT -> Install_YouTube_CRT (run this once).\n'
        '3. Load YouTubeCRT_v0.16-fb from _Other. The menu appears.\n\n'
        'Add channels to /media/fat/YouTubeNative/ytcrt/subscriptions.txt (IDs or @handles).\n'
        'Support: Scripts -> YouTubeCRT -> Status_YouTube_CRT shows what the player is doing.\n', encoding='utf-8')
    zpath = ROOT / 'release' / 'MiSTer-YouTube-SD.zip'
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file():
                z.write(p, p.relative_to(OUT).as_posix())
    # The small package: one folder. Install_YouTube_CRT.sh copies the core and downloads the tools itself.
    small = ROOT / 'release' / 'YouTubeCRT-installer.zip'
    with zipfile.ZipFile(small, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(scripts.rglob('*')):
            if p.is_file():
                z.write(p, 'Scripts/YouTubeCRT/' + p.relative_to(scripts).as_posix())
        z.write(core, 'Scripts/YouTubeCRT/' + CORE)
        for path in legacy:
            z.write(path, 'Scripts/YouTubeCRT/' + path.name)
    print('Core:', core)
    print('Full package (offline): %s (%.1f MB)' % (zpath, zpath.stat().st_size / 1e6))
    print('Small installer: %s (%.1f MB)' % (small, small.stat().st_size / 1e6))


if __name__ == '__main__':
    main()
