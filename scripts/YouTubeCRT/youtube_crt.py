#!/usr/bin/env python3
"""YouTube CRT daemon: the GUI that lives inside the YouTubeCRT core (v0.15-yuv, or the older v0.14-auto).

Started once at boot (see Install_YouTube_CRT.sh). It idles, reading one
memory word a few times a second, until you load the YouTubeCRT core from the
MiSTer menu; then it starts the streaming session by itself and the menu
appears on screen. Load another core and it goes back to waiting; load
YouTubeCRT again and the menu comes back. No script is run per session.
"""
import argparse
import fcntl
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROOT = Path('/media/fat/YouTubeNative')
DATA = ROOT / 'ytcrt'
STOP = Path('/tmp/YouTubeCRT.stop')
LOCK = Path('/tmp/YouTubeCRT.daemon.lock')
VERSION = '2.0.0'


def startup_report(log, rt):
    """One-time snapshot of the environment, to make problems diagnosable from the log alone."""
    import platform
    import socket
    log('--- startup report ---')
    log('python %s on %s %s' % (sys.version.split()[0], platform.system(), platform.machine()))
    log('clock: %s (a badly wrong MiSTer clock can break HTTPS)' % time.strftime('%Y-%m-%d %H:%M:%S %Z'))
    for name, p in (('yt-dlp', rt.resolver), ('qjs', rt.qjs), ('ffmpeg', rt.ffmpeg), ('cacert', rt.ca)):
        log('%s: %s %s' % (name, 'ok' if p.is_file() else 'MISSING', p))
    log('node: %s' % (rt.node if getattr(rt, 'node', None) else 'not installed (security-check videos will be slow; run Install_YouTube_CRT)'))
    try:
        fams = sorted({'IPv6' if a[0] == socket.AF_INET6 else 'IPv4'
                       for a in socket.getaddrinfo('www.youtube.com', 443, type=socket.SOCK_STREAM)})
        log('www.youtube.com resolves to: ' + ', '.join(fams))
    except OSError as exc:
        log('DNS lookup of www.youtube.com failed: %s' % exc)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        log('local IPv4: ' + s.getsockname()[0])
        s.close()
    except OSError as exc:
        log('no IPv4 route: %s' % exc)
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(('2001:4860:4860::8888', 80))
        log('local IPv6: ' + s.getsockname()[0])
        s.close()
    except (OSError, AttributeError):
        log('no IPv6 route')
    log('--- end startup report ---')


def source_signature():
    """Names, sizes and mtimes of the player's own files, to notice when they are replaced."""
    sig = []
    for folder in (HERE, HERE / 'ytcrt'):
        for p in sorted(folder.glob('*')):
            if p.suffix in ('.py', '.sh', '.pem'):
                try:
                    st = p.stat()
                    sig.append((p.name, st.st_size, st.st_mtime_ns))
                except OSError:
                    pass
    return tuple(sig)


def daemon():
    from ytcrt import app as appmod, core_link, feeds, inputs, pipeline, store

    lock = LOCK.open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('YouTube CRT is already running.')
        return

    def log(msg):
        print(time.strftime('%Y-%m-%d %H:%M:%S ') + msg, flush=True)
        try:
            os.fsync(sys.stdout.fileno())      # FAT keeps size/dir entry in RAM otherwise; force it to the card
        except (OSError, ValueError, AttributeError):
            pass

    def cancel():
        if STOP.exists():
            raise core_link.Cancelled('Stopped by the stop file.')
    log('YouTube CRT %s daemon. %s' % (VERSION, datetime.now(timezone.utc).isoformat()))
    rt = pipeline.Runtime(ROOT, HERE)
    if rt.missing():
        log('NOTE: external tools missing (playback disabled): ' + ', '.join(rt.missing()))
    try:
        from ytcrt import __version__
        log('player version ' + __version__)
        startup_report(log, rt)
    except Exception as exc:
        log('startup report failed: %s' % exc)
    st = store.Store(str(DATA))
    st.ensure_subscriptions_file()
    fd = feeds.Feeds(str(DATA / 'cache'), cafile=str(rt.ca))
    inp = inputs.Input(evdev=False, tty=False)   # input comes from the core, not Linux
    link = core_link.Link(log, cancel)
    link.open()
    boot_sig = source_signature()
    pending = {'sig': None, 'since': 0}

    def restart_if_updated():
        """Only while idle: if the player's files were replaced, restart on the new code."""
        sig = source_signature()
        if sig == boot_sig:
            pending['sig'] = None
            return
        if sig != pending['sig']:
            pending['sig'], pending['since'] = sig, time.monotonic()     # still changing (copy in progress)
        elif time.monotonic() - pending['since'] > 5:
            log('Player files changed on the card; restarting on the new version...')
            inp.close()
            link.close()
            lock.close()
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), '--daemon'])
    try:
        while True:
            try:
                log('Waiting for the YouTubeCRT core to be loaded...')
                link.wait_for_core(poll=0.5, idle=restart_if_updated)
                log('Core detected; starting session.')
                link.start_session()
                inp.attach_core(link.box)
                application = appmod.App(link, st, fd, rt, inp, log)
                application.run()
            except core_link.Cancelled:
                log('Stopped.')
                break
            except Exception as exc:
                log('Session ended: %s' % str(exc)[:200])
            finally:
                inp.detach_core()
                link.end_session()
            time.sleep(1)
    finally:
        inp.close()
        link.close()


if __name__ == '__main__':
    # A thread that finishes a bulk copy (the GIL is released while it copies) waits at most this long for the interpreter;
    # the default 5 ms made each of the five copies per picture wait for a busy thread.
    sys.setswitchinterval(0.001)
    parser = argparse.ArgumentParser()
    parser.add_argument('--daemon', action='store_true')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    try:
        if args.stop:
            STOP.touch()
            print('Stop requested; the daemon exits and stays off until the next boot.')
        elif args.daemon:
            STOP.unlink(missing_ok=True)
            daemon()
        else:
            parser.print_help()
    except (Exception, KeyboardInterrupt) as exc:
        print('FAIL: ' + str(exc)[:1000], flush=True)
        sys.exit(1)
