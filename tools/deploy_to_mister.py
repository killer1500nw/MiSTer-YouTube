#!/usr/bin/env python3
"""Copy the player (and optionally the core) to a MiSTer over SSH, and fetch its log.

    python tools/deploy_to_mister.py --setup HOST      one time: make an SSH key and print the command that installs it
    python tools/deploy_to_mister.py [HOST]            copy scripts/YouTubeCRT to /media/fat/Scripts/YouTubeCRT
    python tools/deploy_to_mister.py --core            also copy the core to /media/fat/_Other (reload the core afterwards)
    python tools/deploy_to_mister.py --install         send and run the one-file installer (player and cores)
    python tools/deploy_to_mister.py --log             fetch YouTube-crt-log.txt to ./mister-log.txt (and print its tail)

HOST is read from the argument, $MISTER_HOST or tools/mister_host.txt (git-ignored). The running player notices
the new files and restarts itself within about 10 seconds when idle on the menu. No password is ever stored:
log in with the key made by --setup. Uses the OpenSSH client that ships with Windows 10/11.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_KEYS = [Path.home() / '.ssh' / 'mister_ed25519']
KEY = next((k for k in _KEYS if k.exists()), _KEYS[0])
REMOTE_SCRIPTS = '/media/fat/Scripts'
REMOTE_LOG = '/media/fat/YouTubeNative/YouTube-crt-log.txt'
CORE = 'YouTubeCRT_v0.16-fb.rbf'


def host_name(arg):
    if arg:
        return arg
    if os.environ.get('MISTER_HOST'):
        return os.environ['MISTER_HOST']
    f = ROOT / 'tools' / 'mister_host.txt'
    if f.is_file():
        return f.read_text().strip()
    sys.exit('Give the MiSTer address: an argument, $MISTER_HOST, or a line in tools/mister_host.txt')


def ssh_base(host):
    return ['ssh', '-i', str(KEY), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
            '-o', 'StrictHostKeyChecking=accept-new', 'root@' + host]


def setup(host):
    KEY.parent.mkdir(exist_ok=True)
    if not KEY.exists():
        subprocess.check_call(['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', 'mister-deploy', '-f', str(KEY)])
    pub = Path(str(KEY) + '.pub')
    print('\nKey ready. Run this once (it asks for the MiSTer password, default 1) to let this PC log in without one:\n')
    print('  type "%s" | ssh root@%s "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"\n' % (pub, host))
    print('Then:  python tools/deploy_to_mister.py %s' % host)


def deploy(host, core):
    src = ROOT / 'scripts' / 'YouTubeCRT'
    if not (src / 'ytcrt' / '__init__.py').is_file():
        sys.exit('scripts/YouTubeCRT not found')
    version = (src / 'ytcrt' / '__init__.py').read_text().split("'")[1]
    print('Copying player %s to %s:%s/YouTubeCRT ...' % (version, host, REMOTE_SCRIPTS))
    # tar over ssh: one connection, keeps the folder identical, no stray logs or caches.
    tar = subprocess.Popen(['tar', '-C', str(src.parent), '--exclude=__pycache__', '--exclude=*.pyc',
                            '--exclude=YouTube-*log*.txt', '--exclude=*.rbf', '-cf', '-', 'YouTubeCRT'],
                           stdout=subprocess.PIPE)
    remote = 'tar -C %s -xf - && find %s/YouTubeCRT -name "*.sh" -exec sed -i "s/\\r$//" {} + && chmod +x %s/YouTubeCRT/*.sh' % (
        REMOTE_SCRIPTS, REMOTE_SCRIPTS, REMOTE_SCRIPTS)
    r = subprocess.run(ssh_base(host) + [remote], stdin=tar.stdout)
    tar.stdout.close()
    if tar.wait() or r.returncode:
        sys.exit('Copy failed (is the MiSTer on, the address right, and the key installed? try --setup).')
    if core:
        rbf = ROOT / 'fpga' / 'output_files' / 'YouTubeCRT.rbf'
        rbf = rbf if rbf.is_file() else ROOT / 'release' / CORE
        print('Copying the core %s ...' % rbf.name)
        with open(rbf, 'rb') as f:
            r = subprocess.run(ssh_base(host) + ['cat > /media/fat/_Other/%s.part && mv /media/fat/_Other/%s.part /media/fat/_Other/%s' % (CORE, CORE, CORE)], stdin=f)
        if r.returncode:
            sys.exit('Core copy failed.')
        print('Core copied: reload it from the MiSTer menu to use it.')
    print('Done. The player restarts itself in about 10 seconds; Home will show version %s.' % version)


def install(host):
    """Send release/Install_YouTube_CRT_Test.sh to /media/fat/Scripts and run it there (what you would do from the Scripts menu)."""
    f = ROOT / 'release' / 'Install_YouTube_CRT.sh'
    if not f.is_file():
        sys.exit('release/Install_YouTube_CRT.sh not found')
    dest = REMOTE_SCRIPTS + '/' + f.name
    print('Sending %s (%.1f MB) ...' % (f.name, f.stat().st_size / 1e6))
    with open(f, 'rb') as fh:
        r = subprocess.run(ssh_base(host) + ['cat > %s.part && mv %s.part %s && sed -i "s/\r$//" %s && chmod +x %s' % ((dest,) * 5)], stdin=fh)
    if r.returncode:
        sys.exit('Copy failed (is the MiSTer on and the key installed?).')
    print('Running the installer on the MiSTer ...')
    print()
    sys.exit(subprocess.run(ssh_base(host) + ['cd /media/fat/Scripts && sh ./%s' % f.name]).returncode)


def fetch_log(host):
    out = ROOT / 'mister-log.txt'
    r = subprocess.run(ssh_base(host) + ['cat ' + REMOTE_LOG], stdout=subprocess.PIPE)
    if r.returncode:
        sys.exit('Could not read the log.')
    out.write_bytes(r.stdout)
    lines = r.stdout.decode(errors='replace').splitlines()
    print('Saved %d lines to %s. Last lines:\n' % (len(lines), out))
    print('\n'.join(lines[-25:]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('host', nargs='?')
    ap.add_argument('--setup', action='store_true')
    ap.add_argument('--core', action='store_true')
    ap.add_argument('--log', action='store_true')
    ap.add_argument('--install', action='store_true', help='send and run release/Install_YouTube_CRT.sh')
    a = ap.parse_args()
    host = host_name(a.host)
    if a.setup:
        setup(host)
    elif a.install:
        install(host)
    elif a.log:
        fetch_log(host)
    else:
        deploy(host, a.core)


if __name__ == '__main__':
    main()
