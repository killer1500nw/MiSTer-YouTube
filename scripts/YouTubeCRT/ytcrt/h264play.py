"""Play one video through the MiSTer H264 core from the command line (a test tool; the YouTube player will use the same pieces).

    python3 -m ytcrt.h264play FILE_OR_URL [--start SECONDS] [--audio FILE_OR_URL]

Needs the H264 core installed in _Other and no other program feeding it (quit the core's own video player first).
"""
import argparse
import sys
import time

from . import h264feed
from . import h264ring as hr
from . import pipeline


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('source')
    ap.add_argument('--audio', help='a separate audio source (default: the same as the video)')
    ap.add_argument('--start', type=float, default=0.0)
    ap.add_argument('--duration', type=float, default=0.0, help='the length in seconds, so that audio ending early is reopened')
    ap.add_argument('--root', default='/media/fat/YouTubeNative')
    ap.add_argument('--here', default='/media/fat/Scripts/.config/YouTubeCRT')
    ap.add_argument('--rbf', default='_Other/H264')
    a = ap.parse_args(argv)
    rt = pipeline.Runtime(a.root, a.here)
    video = {'url': a.source, 'headers': {}}
    audio = {'url': a.audio or a.source, 'headers': {}}

    def log(msg):
        print(time.strftime('%H:%M:%S'), msg, flush=True)

    log('Loading the H264 core...')
    hr.launch_core(a.rbf)
    ring, mem = hr.open_devmem()
    try:
        ring.alive(10.0)
        ring.new_session()
        log('Session started; ffmpeg: ' + str(rt.ffmpeg))
        feeder = h264feed.Feeder(ring, lambda s: h264feed.video_copy_command(rt, video, s),
                                 lambda s: h264feed.audio_pcm_command(rt, audio, s), log=log, duration_s=a.duration)
        feeder.start(a.start)
        last = [0.0]

        def tick():
            now = time.monotonic()
            if now - last[0] >= 2.0:
                last[0] = now
                st = ring.status()
                log('%-8s clock %7.2f s  written: %d pictures %d audio  seen %d  late %d  starved %d  skipped %d  ring free %d KB%s' % (
                    hr.STATES.get(st['state'], st['state']), st['stc'] / 90000.0, feeder.stats['au'], feeder.stats['audio'],
                    st['pictures_seen'], st['late'], st['starved'], st['skipped'], ring.free() // 1024,
                    ('  ERROR: ' + ring.state_text(st)) if st['state'] == 5 else ''))
        try:
            result = feeder.run(tick=tick)
            log('Done: ' + result)
        except KeyboardInterrupt:
            log('Stopped.')
        except (hr.RingError, RuntimeError) as exc:
            log('FAILED: %s' % exc)
            for kind, text in feeder.errors:
                log('  ffmpeg (%s): %s' % (kind, text.strip().splitlines()[-1] if text.strip() else ''))
            return 1
    finally:
        ring.close()
        mem.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
