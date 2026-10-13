#!/usr/bin/env python3
"""Run the YouTube CRT app on a PC against fake data and save screenshots.

    python tools/ytcrt_sim.py OUTDIR [yuv]

With `yuv` it drives the v0.15 core's path (FakeYuvLink): YUV pictures, the GUI and playback bar as overlays, and every
screenshot is the 640x480 frame the FPGA would draw (tools/yuv_ref.py composites picture + overlay).

No MiSTer, network or ffmpeg needed. Uses FakeLink (software 30 fps consumer)
and a fake pipeline that produces moving test frames, so menus, resume
prompts, the playback OSD, pause and seek can all be looked at.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'YouTubeCRT'))

from ytcrt import app as appmod, core_link, feeds, inputs, pipeline, store, yuv  # noqa: E402
from ytcrt.gfx import Canvas, YuvCanvas, rgb  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yuv_ref  # noqa: E402

NOW = time.time()
TITLES = [
    ('How CRT televisions actually work', 'Technology Connections', 3), ('Building a MiSTer FPGA from scratch - full guide', 'RetroRGB', 26),
    ('Sega Saturn: why it was so hard to program', 'Modern Vintage Gamer', 70), ('Best scaler settings for 240p consoles', 'RetroRGB', 120),
    ('Playing N64 on a PVM in 2026', 'Retro Game Corps', 200), ('Short: scanlines explained', 'Retro Game Corps', 300),
    ('Why do old games look better on CRTs? Phosphor decay, scanlines and bloom explained', 'Techmoan', 600),
    ('Amiga 500 restoration part 4 - the recap', '8-Bit Guy', 1400), ('Unboxing a Japanese-import Famicom Disk System', 'Game Sack', 3000),
    ('Live Q&A: ask me anything', 'Technology Connections', 5000),
]


def fake_videos():
    out = []
    for i, (t, ch, mins) in enumerate(TITLES):
        out.append({'id': 'vid%08d' % i, 'title': t, 'channel': ch, 'channel_id': 'UC' + 'a' * 22,
                    'published': NOW - mins * 60, 'short': t.startswith('Short'), 'views': 1000 * (i + 3) * 17})
    return out


def fake_thumb(seed, yuv_format=False):
    if yuv_format:
        c = YuvCanvas(feeds.THUMB_W, feeds.THUMB_H + 1)             # an even height; the last row is cut off below
        for y in range(feeds.THUMB_H):
            for x in range(0, feeds.THUMB_W, 2):
                c.rect(x, y, 2, 1, rgb(40 + seed * 17 % 160, 60 + x * 2 % 180, 80 + y * 4 % 160))
        cw, ch = feeds.THUMB_W // 2, (feeds.THUMB_H + 1) // 2
        return bytes(c.y[:feeds.THUMB_W * feeds.THUMB_H]) + bytes(c.u[:cw * ch]) + bytes(c.v[:cw * ch])
    px = bytearray()
    for y in range(feeds.THUMB_H):
        for x in range(feeds.THUMB_W):
            px += rgb(40 + seed * 17 % 160, 60 + x * 2 % 180, 80 + y * 4 % 160)
    return bytes(px)


class FakePipe:
    """Stands in for RecordPipeline: endless moving frames."""
    class Dec:
        @staticmethod
        def colour_flags(height):
            return 0

    def __init__(self, start_s, fps, limit=None, yuv_size=None):
        self.decoder = self.Dec()
        self.yuv_size = yuv_size
        self.n = int(start_s * fps)
        self.fps = fps
        self.t0 = time.monotonic()
        self.limit = limit
        self.sent = 0

    def poll(self):
        if self.limit is not None and self.sent >= self.limit:
            return 'eof', None
        if self.sent >= (time.monotonic() - self.t0) * 400:
            return 'wait', None
        self.sent += 1
        if self.yuv_size:                                            # a YUV picture of the video's own size
            w, h = self.yuv_size
            c = YuvCanvas(w, h)
            c.fill(rgb(20, 90, 40))
            c.rect((self.n * 8) % (w - 80), h // 3, 80, 80, rgb(220, 200, 40))
            c.text(40, 30, 'frame %d' % self.n, rgb(255, 255, 255), rgb(20, 90, 40), 4, 4)
            self.n += 1
            return 'record', (bytearray(c.y + c.u + c.v), bytes(6400))
        c = Canvas()
        c.fill(rgb(20, 90, 40))
        x = (self.n * 4) % 280
        c.rect(x, 80, 40, 40, rgb(220, 200, 40))
        c.text(20, 20, 'frame %d' % self.n, rgb(255, 255, 255), rgb(20, 90, 40), 2, 2)
        self.n += 1
        return 'record', bytes(c.buf) + bytes(6400)

    def close(self, wait=True):
        pass


def main(outdir, yuv_mode=False):
    os.makedirs(outdir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='ytcrt-sim-')
    st = store.Store(tmp)
    st.set('shorts', 'On')
    vids = fake_videos()
    fd = feeds.Feeds(os.path.join(tmp, 'cache'))
    fd.page_fetch = lambda pid, start, count: ('T', [dict(v, approx=True) for v in fake_videos()][:0])   # no more pages
    chans = {}
    for v in vids:
        chans.setdefault(v['channel'], []).append(v)
    subs = []
    for i, (name, vs) in enumerate(chans.items()):
        cid = 'UC%022d' % i
        fd.channels[cid] = {'name': name, 'videos': vs, 'at': time.time(), 'ok': True}
        subs.append(cid)
    with open(st.p_subs, 'w') as f:
        f.write('\n'.join(subs) + '\n')
    for i, v in enumerate(vids):
        fd.thumbs[v['id']] = fake_thumb(i, yuv_mode)
    st.toggle_later(vids[1])
    with open(st.p_subs, 'a') as f:
        f.write('PL' + 'q' * 32 + '\nMy Retro Mix | PL' + 'r' * 32 + '\nPL' + 'x' * 32 + '\n')
        f.write('\n'.join(v['id'] for v in vids[:3]) + '\nOld Clip | ' + vids[5]['id'] + '\n')
    fd.playlists['PL' + 'q' * 32] = {'title': 'Arcade Racers', 'videos': [dict(v, approx=True, published=0) for v in vids[:5]], 'at': time.time()}
    fd.playlists['PL' + 'r' * 32] = {'title': 'x', 'videos': [dict(v, approx=True, published=0) for v in vids[5:]], 'at': time.time()}
    fd.status['PL' + 'x' * 32] = 'The playlist does not exist.'
    for v in vids[:3]:
        fd.titles[v['id']] = {'title': v['title'], 'channel': v['channel']}
    st.record_play(vids[2], 754000, 1800000)

    class Rt:
        ffmpeg = Path(tmp) / 'nope'
        resolver = qjs = ca = ffmpeg

        def missing(self):
            return []
    pipeline.resolve = lambda rt, prof, vid, cancel, log, note=None, **kw: pipeline.Media(
        vid, 'Fake video ' + vid, 1800, {'width': 426, 'height': 240, 'fps': 30, 'tbr': 210}, None, '240p', None, '', 0.4)
    opened = []
    pipeline.open_pipeline = lambda rt, prof, media, start_s, speed, cancel, crop='Off', target=None: (
        opened.append((round(start_s, 1), crop)), FakePipe(start_s, 30, yuv_size=(target['w'], target['h']) if target else None))[1]
    link = core_link.FakeYuvLink() if yuv_mode else core_link.FakeLink()
    inp = inputs.Input(evdev=False, tty=False)
    a = appmod.App(link, st, fd, Rt(), inp)
    a.refresher = None
    n = [0]

    def write_png(path, raw, w, h, scale=1):
        import struct
        import zlib
        rows = []
        for y in range(h):
            line = raw[y * w * 3:(y + 1) * w * 3]
            if scale > 1:
                line = b''.join(line[i:i + 3] * scale for i in range(0, len(line), 3))
            rows.extend([b'\x00' + line] * scale)

        def chunk(tag, body):
            c = struct.pack('>I', len(body)) + tag + body
            return c + struct.pack('>I', zlib.crc32(tag + body) & 0xffffffff)
        with open(path, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w * scale, h * scale, 8, 2, 0, 0, 0)) +
                    chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))

    def screen_png(path):
        """The 640x480 frame the v0.15 core would draw for the newest picture (picture + overlay), written as a PNG."""
        frame, audio, w, h, crop, flags, ov = link.pictures[-1]
        hdr = yuv.parse_header(yuv.header_for(w, h, crop, link.interlaced, flags, ov))
        y, u, v = frame[:w * h], frame[w * h:w * h * 5 // 4], frame[w * h * 5 // 4:]
        px = yuv_ref.render_frame(y, u, v, hdr, True, link.overlays.get(ov) if ov is not None else None)
        raw = bytearray()
        for line in px:
            for p_ in line:
                raw += bytes(p_)
        write_png(path, bytes(raw), 640, 480, 1)

    def shot(name):
        if a.screen:
            a.render()
        if yuv_mode:
            for _ in range(8):                         # let the menu picture that references the new overlay go out
                a.pump(0.001)
            screen_png(os.path.join(outdir, '%02d_%s.png' % (n[0], name)))
        else:
            a.canvas.save_png(os.path.join(outdir, '%02d_%s.png' % (n[0], name)), 3)
        n[0] += 1

    def keys(*acts, settle=6):
        for act in acts:
            inp.push(act)
            for _ in range(settle):
                a.pump(0.001)

    def goto(name):
        a.go_home()
        a.open_home_entry(name)
        settle()

    def settle(k=8):
        for _ in range(k):
            a.pump(0.001)

    settle()
    shot('home')
    goto('Subscriptions')
    shot('subscriptions')
    keys('down', 'down')
    shot('subscriptions_scrolled')
    keys('alt')
    shot('watch_later_toast')
    goto('Channels')
    shot('channels')
    keys('ok')
    shot('channel_videos')
    keys('down')
    shot('channel_load_more_row')
    goto('Playlists')
    shot('playlists')
    a.screen.activate(a, a.screen.items(a)[0])
    settle()
    shot('playlist_videos')
    goto('My Videos')
    shot('my_videos')
    goto('Watch Later')
    shot('watch_later')
    goto('History')
    shot('history')
    keys('ok')                                  # video with a saved position -> resume prompt
    shot('resume_prompt')
    import threading

    def snap(name):
        if yuv_mode:
            screen_png(os.path.join(outdir, '%02d_%s.png' % (n[0], name)))
        else:
            rec = link.last
            Canvas(320, 240, bytearray(rec[:153600])).save_png(os.path.join(outdir, '%02d_%s.png' % (n[0], name)), 3)
        n[0] += 1

    def controller():
        time.sleep(2.0)
        snap('playing_osd')
        inp.push('ok')
        time.sleep(1.0)
        snap('paused')
        inp.push('ok')
        for _ in range(3):                      # three quick taps: must become ONE seek of +30 s
            inp.push('right')
            time.sleep(0.15)
        time.sleep(0.2)
        snap('seek_pending_osd')
        time.sleep(1.5)
        snap('after_seek')
        time.sleep(5.0)
        snap('osd_hidden')
        inp.push('menu')                        # X: the More panel
        time.sleep(0.4)
        snap('more_panel')
        inp.push('right')                       # Crop: Off -> 14:9
        time.sleep(0.3)
        inp.push('right')                       # Crop: 14:9 -> Fill (coalesces into one restart)
        time.sleep(0.4)
        snap('more_panel_fill_pending')
        time.sleep(1.5)
        inp.push('down')
        inp.push('ok')                          # Watch Later toggle from the panel
        time.sleep(0.4)
        snap('more_panel_later')
        inp.push('back')                        # closes the panel (does not stop playback)
        time.sleep(1.0)
        snap('panel_closed_still_playing')
        inp.push('back')                        # now stop
    threading.Thread(target=controller, daemon=True).start()
    keys('ok')                                  # "Resume at ..." -> plays until the controller presses back
    settle()
    shot('back_in_history')
    goto('Play URL / ID')
    for ch in 'youtu.be/M7kB0lis3xg':
        inp.push('ch:' + ch)
    keys('down', 'right', 'right', settle=3)
    shot('typing_screen')
    goto('Settings')
    shot('settings')
    keys('down', 'down', 'right', 'down', 'right')
    shot('settings_changed')
    print('pipelines opened at (s):', opened)
    print('history after play:', st.history_list()[0]['pos'], 'ms; screenshots in', outdir)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'sim_out', 'yuv' in sys.argv[2:])
