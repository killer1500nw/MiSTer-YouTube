#!/usr/bin/env python3
"""Download the video tools the YouTube CRT player needs, straight onto the MiSTer.

Run by Install_YouTube_CRT.sh. Needs only Python 3 and an internet connection.
Each file is checked against the publisher's checksum before it is used, and
anything already installed is left alone.

    yt-dlp  2026.08.19  (ARM onedir build)   Unlicense  github.com/yt-dlp/yt-dlp
    QuickJS v0.17.0     (qjs, ARM)           MIT        github.com/quickjs-ng/quickjs
    FFmpeg  7.0.2 static armhf (eugeneware/ffmpeg-static, John Van Sickle build) GPLv3
    Node.js v22         (linux-armv7l)       MIT        nodejs.org   (optional: solves YouTube's JS check in
                                                              seconds instead of minutes; QuickJS stays as the fallback)
"""
import argparse
import hashlib
import gzip
import io
import os
import shutil
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
YTDLP_TAG = '2026.08.19'
YTDLP_BASE = 'https://github.com/yt-dlp/yt-dlp/releases/download/%s/' % YTDLP_TAG
QJS_TAG = 'v0.17.0'
QJS_URL = 'https://github.com/quickjs-ng/quickjs/releases/download/%s/qjs-linux-armv7' % QJS_TAG
QJS_SHA256 = 'dbda6e499471c675bd991dbc477f14d6134c182409d15047e9b63e3df7bc3e64'
# The b6.1.1 release's ARMHF asset contains John Van Sickle's FFmpeg 7.0.2 build.
# Keep the release and GitHub-published SHA-256 digests pinned together.
FFMPEG_BASE = 'https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/'
FFMPEG_URL = FFMPEG_BASE + 'ffmpeg-linux-arm.gz'
FFMPEG_SHA256 = '64b115a12f0ab77c277e3c418aae8b40ef881e75e746a0e2d066a206b9bc5172'
FFMPEG_NOTICES = (
    ('linux-arm.LICENSE', 'GPLv3.txt', '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903'),
    ('linux-arm.README', 'readme.txt', 'faaea83a6d92419cb112d5de5ac673f51b72401830e068c4d003b3229ee1c89a'),
)
NODE_VER = 'v22.23.3'                      # yt-dlp needs Node 22 or newer
NODE_URL = 'https://nodejs.org/dist/%s/node-%s-linux-armv7l.tar.gz' % (NODE_VER, NODE_VER)
NODE_SHA256 = '8b99058ec6f4e372b9887f8ecb577a292adb132d59e5104356428a758fdb126c'
UA = 'YouTubeCRT-setup/1.0'
# The optional H264 core, by owenb321 (github.com/owenb321/MiSTer_h264_decoder, GPL-3.0-or-later). It is NOT part of this project's release:
# the script below fetches that one file from the author's own release page and checks it against the checksum GitHub publishes for it.
H264_TAG = 'v1.1.0'
H264_NAME = 'H264_20261014.rbf'
H264_URL = 'https://github.com/owenb321/MiSTer_h264_decoder/releases/download/%s/%s' % (H264_TAG, H264_NAME)
H264_SHA256 = '0790a8893789092c542aabc2e1135169b57a1f29bb7663a415fe8a5a33e1bdfb'
H264_MGL = '<mistergamedescription>\n  <rbf>_Other/H264</rbf>\n</mistergamedescription>\n'
H264_README = (
    'The H264 core in this folder (%s) is the work of owenb321: https://github.com/owenb321/MiSTer_h264_decoder\n'
    'GPL-3.0-or-later; its client library is MIT. Source code and licence: that page (release %s).\n'
    'It was downloaded from there by the YouTube CRT player\'s Install_H264_Core script. It is not part of the player.\n' % (H264_NAME, H264_TAG))


def ctx():
    ca = HERE / 'cacert.pem'
    return ssl.create_default_context(cafile=str(ca)) if ca.is_file() else ssl.create_default_context()


def fetch(url, dest=None, label=''):
    """Download to `dest` (or return bytes). Prints a progress line every ~10%."""
    last = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=60, context=ctx()) as r:
                total = int(r.headers.get('Content-Length') or 0)
                out = open(dest, 'wb') if dest else io.BytesIO()
                got, shown = 0, -1
                with out:
                    data_bytes = None
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        got += len(chunk)
                        if total and label and got * 10 // total != shown:
                            shown = got * 10 // total
                            print('  %s %d%%' % (label, shown * 10), flush=True)
                    if not dest:
                        data_bytes = out.getvalue()
                return None if dest else data_bytes
        except Exception as exc:                  # network hiccup: retry
            last = exc
            print('  attempt %d failed: %s' % (attempt, exc), flush=True)
            time.sleep(2)
    raise RuntimeError('Download failed (%s): %s' % (url, last))


def digest(path, algo):
    h = hashlib.new(algo)
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def safe_members(names):
    for n in names:
        if os.path.isabs(n) or '..' in Path(n).parts:
            raise RuntimeError('Unsafe path in archive: ' + n)


def setup_ytdlp_and_qjs(root, dl):
    res = root / ('resolver-%s-qjs-%s' % (YTDLP_TAG, QJS_TAG.lstrip('v')))
    ytdlp, qjs = res / 'bundle' / 'yt-dlp_linux_armv7l', res / 'qjs'
    if ytdlp.is_file() and qjs.is_file():
        print('yt-dlp and QuickJS already installed.')
        return
    print('Downloading yt-dlp %s (about 39 MB)...' % YTDLP_TAG, flush=True)
    zpath = dl / 'yt-dlp_linux_armv7l.zip'
    fetch(YTDLP_BASE + 'yt-dlp_linux_armv7l.zip', zpath, 'yt-dlp')
    sums = fetch(YTDLP_BASE + 'SHA2-256SUMS').decode()
    want = [line.split()[0] for line in sums.splitlines() if line.endswith('yt-dlp_linux_armv7l.zip')]
    if not want or digest(zpath, 'sha256') != want[0]:
        raise RuntimeError('yt-dlp checksum mismatch; not installing it.')
    print('  checksum OK', flush=True)
    print('Downloading QuickJS %s (about 2 MB)...' % QJS_TAG, flush=True)
    qpath = dl / 'qjs'
    fetch(QJS_URL, qpath, 'qjs')
    if digest(qpath, 'sha256') != QJS_SHA256:
        raise RuntimeError('QuickJS checksum mismatch; not installing it.')
    print('  checksum OK', flush=True)
    stage = root / ('_stage-' + res.name)
    shutil.rmtree(stage, ignore_errors=True)
    (stage / 'bundle').mkdir(parents=True)
    with zipfile.ZipFile(zpath) as z:
        safe_members(z.namelist())
        z.extractall(stage / 'bundle')
    shutil.copyfile(qpath, stage / 'qjs')
    for p in (stage / 'bundle' / 'yt-dlp_linux_armv7l', stage / 'qjs'):
        os.chmod(p, 0o755)
    shutil.rmtree(res, ignore_errors=True)
    os.rename(stage, res)                        # only a complete install ever has the final name
    print('yt-dlp and QuickJS installed.')


def node_version(path):
    try:
        return subprocess.check_output([str(path), '--version'], stderr=subprocess.STDOUT, timeout=30).decode().strip()
    except Exception:
        return ''


def setup_node(root, dl):
    """Node.js, only the `node` program (about 100 MB unpacked). Optional: a failure here is not fatal."""
    final = root / ('node-%s-armhf' % NODE_VER.lstrip('v'))
    if (final / 'node').is_file():
        print('Node.js already installed.')
        return
    stage = root / ('_stage-' + final.name)
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    test = root / 'node-test' / 'node'              # left by Test_Node_JS.sh: reuse it instead of downloading again
    if test.is_file() and node_version(test) == NODE_VER:
        shutil.copyfile(test, stage / 'node')
        print('Reusing the Node.js from the earlier test.')
    else:
        print('Downloading Node.js %s (about 49 MB)...' % NODE_VER, flush=True)
        tpath = dl / 'node.tar.gz'
        fetch(NODE_URL, tpath, 'node')
        if digest(tpath, 'sha256') != NODE_SHA256:
            raise RuntimeError('Node.js checksum mismatch; not installing it.')
        print('  checksum OK; unpacking...', flush=True)
        with tarfile.open(tpath, 'r:gz') as tar:
            safe_members(tar.getnames())
            for m in tar:
                if m.isfile() and m.name.endswith('/bin/node'):
                    with tar.extractfile(m) as src, open(stage / 'node', 'wb') as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    break
    if not (stage / 'node').is_file():
        raise RuntimeError('node not found in the archive.')
    os.chmod(stage / 'node', 0o755)
    got = node_version(stage / 'node')
    if got != NODE_VER:
        shutil.rmtree(stage, ignore_errors=True)
        raise RuntimeError('Node.js does not run on this MiSTer (%r).' % got)
    shutil.rmtree(final, ignore_errors=True)
    os.rename(stage, final)
    print('Node.js installed (%s).' % got)


def setup_ffmpeg(root, dl):
    if list(root.glob('ffmpeg-*-armhf-static/ffmpeg')):
        print('ffmpeg already installed.')
        return
    print('Downloading ffmpeg 7.0.2 ARMHF...', flush=True)
    tpath = dl / 'ffmpeg.gz'
    fetch(FFMPEG_URL, tpath, 'ffmpeg')
    if digest(tpath, 'sha256') != FFMPEG_SHA256:
        raise RuntimeError('ffmpeg checksum mismatch; not installing it.')
    print('  checksum OK; unpacking...', flush=True)
    final = root / 'ffmpeg-7.0.2-armhf-static'
    stage = root / ('_stage-' + final.name)
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        with gzip.open(tpath, 'rb') as src, open(stage / 'ffmpeg', 'wb') as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        for remote, local, checksum in FFMPEG_NOTICES:
            notice = stage / local
            fetch(FFMPEG_BASE + remote, notice)
            if digest(notice, 'sha256') != checksum:
                raise RuntimeError('ffmpeg notice checksum mismatch; not installing it.')
        os.chmod(stage / 'ffmpeg', 0o755)
        shutil.rmtree(final, ignore_errors=True)
        os.rename(stage, final)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    print('ffmpeg installed (%s).' % final.name)


def setup_h264_core(fat):
    """Put the H264 core in /media/fat/_Other (checked against its published SHA-256), plus the menu entry that loads it."""
    other = Path(fat) / '_Other'
    other.mkdir(parents=True, exist_ok=True)
    dest = other / H264_NAME
    if dest.is_file() and digest(dest, 'sha256') == H264_SHA256:
        print('The H264 core %s is already installed.' % H264_NAME)
    else:
        print('Downloading the H264 core %s (about 4.4 MB) from github.com/owenb321/MiSTer_h264_decoder ...' % H264_NAME, flush=True)
        part = other / (H264_NAME + '.part')
        try:
            fetch(H264_URL, part, 'H264 core')
            if digest(part, 'sha256') != H264_SHA256:
                raise RuntimeError('H264 core checksum mismatch; not installing it.')
            print('  checksum OK', flush=True)
            os.replace(part, dest)
        finally:
            if part.exists():
                part.unlink()
        print('The H264 core is installed: %s' % dest)
    (other / 'YouTube (H264).mgl').write_text(H264_MGL)
    (other / 'H264_core_README.txt').write_text(H264_README)
    print('Menu entry written: Other > YouTube (H264)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='/media/fat/YouTubeNative')
    ap.add_argument('--h264-core', action='store_true', help='only install the optional H264 core (see setup_h264_core)')
    ap.add_argument('--fat', default='/media/fat')
    args = ap.parse_args()
    if args.h264_core:
        try:
            setup_h264_core(args.fat)
        except Exception as exc:
            print('SETUP FAILED: %s' % exc, flush=True)
            return 1
        return 0
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    dl = root / '_download'
    shutil.rmtree(dl, ignore_errors=True)
    dl.mkdir()
    try:
        setup_ytdlp_and_qjs(root, dl)
        setup_ffmpeg(root, dl)
    except Exception as exc:
        print('SETUP FAILED: %s' % exc, flush=True)
        return 1
    try:
        setup_node(root, dl)
    except Exception as exc:                      # optional: QuickJS still works, only slower
        print('NOTE: Node.js was not installed (%s). Videos that need the JS security check will be slow.' % exc, flush=True)
    finally:
        shutil.rmtree(dl, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
