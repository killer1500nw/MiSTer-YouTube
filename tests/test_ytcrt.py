import signal
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'YouTubeCRT'))
from ytcrt import core_link, feeds, gfx, inputs, pipeline, profiles, store, ui, urls  # noqa: E402

def offline_feeds(path):
    """A Feeds object that cannot reach the network: tests must never talk to YouTube (slow, flaky offline)."""
    f = feeds.Feeds(path)

    def refuse(*args, **kwargs):
        raise OSError('network disabled in tests')
    f._get = f._find_in_page = refuse
    return f


ATOM = b'''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Chan</title><author><name>Chan Name</name></author>
 <entry><yt:videoId>AAAAAAAAAAA</yt:videoId><yt:channelId>UCx</yt:channelId><title>Old one</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=AAAAAAAAAAA"/><author><name>Chan Name</name></author>
  <published>2026-01-01T10:00:00+00:00</published>
  <media:group><media:community><media:statistics views="1234"/></media:community></media:group></entry>
 <entry><yt:videoId>BBBBBBBBBBB</yt:videoId><title>New short</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/BBBBBBBBBBB"/>
  <published>2026-02-01T10:00:00+00:00</published></entry>
</feed>'''


class Gfx(unittest.TestCase):
    def test_rect_text_clip(self):
        c = gfx.Canvas(32, 16)
        red = gfx.rgb(255, 0, 0)
        c.rect(-5, -5, 10, 10, red)
        self.assertEqual(c.buf[0:2], red)
        self.assertEqual(c.buf[10 * 2:10 * 2 + 2], b'\0\0')
        c.rect(30, 14, 50, 50, red)               # clipped, must not raise or grow
        self.assertEqual(len(c.buf), 32 * 16 * 2)
        c.text(0, 8, 'Hi é中', gfx.rgb(255, 255, 255), gfx.rgb(0, 0, 0))
        self.assertEqual(len(c.buf), 32 * 16 * 2)

    def test_fold(self):
        self.assertEqual(gfx.fold('Café 中'), 'Cafe ?')

    def test_565_roundtrip_png(self):
        c = gfx.Canvas(4, 4)
        c.fill(gfx.rgb(255, 255, 255))
        self.assertEqual(c.to_rgb888()[:3], b'\xff\xff\xff')
        with tempfile.TemporaryDirectory() as d:
            c.save_png(str(Path(d) / 'a.png'), 2)
            self.assertTrue((Path(d) / 'a.png').read_bytes().startswith(b'\x89PNG'))


class Feeds(unittest.TestCase):
    def test_parse_sorted_and_flags(self):
        name, vids = feeds.parse_feed(ATOM)
        self.assertEqual(name, 'Chan Name')
        self.assertEqual([v['id'] for v in vids], ['BBBBBBBBBBB', 'AAAAAAAAAAA'])
        self.assertTrue(vids[0]['short'])
        self.assertFalse(vids[1]['short'])
        self.assertEqual(vids[1]['views'], 1234)

    def test_age(self):
        self.assertEqual(feeds.age_text(1000, 1000 + 3 * 3600 + 5), '3h')
        self.assertEqual(feeds.age_text(1000, 1000 + 40 * 86400), '1mo')
        self.assertEqual(feeds.age_text(0), '')


class Store(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.s = store.Store(self.d.name)

    def tearDown(self):
        self.d.cleanup()

    def test_subscriptions_parse(self):
        Path(self.s.p_subs).write_text(
            '# comment\nMy Name | UC' + 'a' * 22 + '\nhttps://www.youtube.com/channel/UC' + 'b' * 22 +
            '\n@SomeHandle\nUC' + 'a' * 22 + '\njunk line\n')
        subs = self.s.subscriptions()
        self.assertEqual(subs, [('My Name', 'UC' + 'a' * 22), (None, 'UC' + 'b' * 22), (None, '@SomeHandle')])

    def test_resume_and_finish(self):
        v = {'id': 'x' * 11, 'title': 't', 'channel': 'c', 'published': 1}
        self.s.record_play(v, 300000, 1200000)
        self.assertEqual(self.s.saved_pos(v['id']), 300000)
        self.s.record_play(v, 1190000, 1200000)           # within 15 s of the end: finished
        self.assertEqual(self.s.saved_pos(v['id']), 0)
        self.assertEqual(len(self.s.history_list()), 1)
        again = store.Store(self.d.name)                  # persisted
        self.assertEqual(len(again.history_list()), 1)

    def test_watch_later_toggle_and_history_cap(self):
        v = {'id': 'y' * 11, 'title': 't', 'channel': 'c', 'published': 1}
        self.assertTrue(self.s.toggle_later(v))
        self.assertTrue(self.s.in_later(v['id']))
        self.assertFalse(self.s.toggle_later(v))
        for i in range(store.MAX_HISTORY + 5):
            self.s.record_play({'id': 'v%010d' % i, 'title': ''}, 1000, 0)
        self.assertEqual(len(self.s.history), store.MAX_HISTORY)

    def test_bad_settings_file_ignored(self):
        Path(self.s.p_settings).write_text('{not json')
        self.assertEqual(store.Store(self.d.name).settings['quality'], 'Auto')


class Profiles(unittest.TestCase):
    def test_the_one_profile_matches_the_core(self):
        p = profiles.PROFILE
        self.assertEqual((p.w, p.h, p.fps), (320, 240, 30))
        self.assertEqual((p.video_bytes, p.audio_bytes, p.record_bytes), (153600, 6400, 160000))
        self.assertIs(profiles.BY_KEY[profiles.DEFAULT], p)

    def test_settings_screen_lists_only_settings_that_do_something(self):
        a = type('A', (), {'store': store.Store(tempfile.mkdtemp())})()
        rows = [r[0] for r in ui.Settings().rows(a)]
        self.assertEqual(rows, ['quality', 'crop', 'resume', 'shorts', 'thumbnails', 'decode_speed', 'bench', 'diag',
                                'clear_later', 'clear_history'])
        self.assertNotIn('profile', rows)
        self.assertNotIn('about', rows)
        self.assertNotIn('profile', store.SETTINGS_DEFAULTS)

    def test_an_old_settings_file_with_a_profile_key_is_ignored(self):
        d = tempfile.mkdtemp()
        Path(d, 'settings.json').write_text('{"profile": "480i30", "quality": "360p"}')
        st = store.Store(d)
        self.assertEqual(st.settings['quality'], '360p')
        self.assertNotIn('profile', st.settings)

    def test_filter_and_decoder_flags(self):
        self.assertIn('fps=30', profiles.video_filter(profiles.BY_KEY['240p30']))
        self.assertIn('fast_bilinear', profiles.video_filter(profiles.BY_KEY['240p30']))
        self.assertEqual(profiles.decode_speed_args('Normal'), [])
        self.assertEqual(profiles.decode_speed_args('Fast')[:2], ['-skip_loop_filter', 'noref'])   # 'nonref' is NOT valid
        self.assertEqual(profiles.decode_speed_args('Fastest')[:2], ['-skip_loop_filter', 'all'])
        self.assertEqual(profiles.speed_for(640 * 360, 'Normal', None), 'Fast')       # big picture: never the slow default
        self.assertEqual(profiles.speed_for(640 * 360, 'Normal', 'Fastest'), 'Fastest')
        self.assertEqual(profiles.speed_for(426 * 240, 'Normal', 'Fastest'), 'Normal')  # small picture: as asked


@unittest.skipIf(sys.platform == "win32", "POSIX pipes/process groups (runs on the MiSTer or WSL)")
class Pipeline(unittest.TestCase):
    def test_real_pipes_pairing_and_audio_padding(self):
        commands = {}
        for kind, size, count, chunk in [('video', 153600, 6, 7777), ('audio', 6400, 5, 997)]:
            code = 'import os\ndata=b"".join(bytes([i])*%d for i in range(%d))\n' % (size, count)
            if kind == 'audio':
                code += 'data+=bytes([5])*3200\n'
            code += 'for i in range(0,len(data),%d):\n os.write(1,data[i:i+%d])\n' % (chunk, chunk)
            commands[kind] = [sys.executable, '-c', code]
        q = pipeline.RecordPipeline(pipeline.Decoders(commands, 153600, 6400))
        try:
            for i in range(6):
                r = q.next_record(lambda: None)
                self.assertEqual(r[:153600], bytes([i]) * 153600)
                tail = bytes([i]) * 6400 if i < 5 else bytes([5]) * 3200 + bytes(3200)
                self.assertEqual(r[153600:], tail)
            self.assertIsNone(q.next_record(lambda: None))
        finally:
            q.close()

    def test_decoder_error_surfaces(self):
        cmd = {'video': [sys.executable, '-c', 'import sys;sys.stderr.write("boom");sys.exit(3)']}
        q = pipeline.RecordPipeline(pipeline.Decoders(cmd, 153600, 6400))
        try:
            with self.assertRaisesRegex(RuntimeError, 'boom'):
                for _ in range(100):
                    q.next_record(lambda: None)
        finally:
            q.close()



class FakeBox:
    """Models the v0.14-auto core's mailbox: stale error until a stop command."""

    def __init__(self, error=True, clears=True):
        self.w = {0: 0x33515459, 4: 77, 8: 99, 12: 0, 16: 0, 20: 77, 28: 1 if error else 0, 32: 0x34445459, 36: 0}
        self.clears = clears
        self.cmds = []

    def read(self, off):
        if off == 36:
            self.w[36] += 1                    # heartbeat ticks on every look
        return self.w[off]

    def write(self, off, value):
        self.w[off] = value

    def command(self, session):
        self.cmds.append(session)
        self.w[0] = 0x33515459 if session else 0
        self.w[4] = session
        if session == 0:
            self.w[20] = 0
            if self.clears:
                self.w[28] = 0
        else:
            self.w[20] = session

    def status(self, session):
        return self.w[16]


class Handshake(unittest.TestCase):
    def link(self, box):
        link = core_link.Link(lambda m: None, lambda: None)
        link.box = box
        link.settle_timeout = 0.2
        return link

    def test_recovers_stale_error_without_off_on(self):
        box = FakeBox(error=True)
        link = self.link(box)
        link.start_session()
        self.assertEqual(box.cmds[0], 0)                  # stop first, which clears the error
        self.assertEqual(box.w[8], 0)
        self.assertEqual(box.w[28], 0)
        self.assertEqual(box.w[20], link.session)
        self.assertNotEqual(link.session, 77)

    def test_old_core_that_never_clears_error_is_reported(self):
        link = self.link(FakeBox(error=True, clears=False))
        with self.assertRaisesRegex(RuntimeError, 'error flag stuck'):
            link.start_session()

    def test_wait_for_core_needs_heartbeat_motion(self):
        box = FakeBox()
        box.read = lambda off: 0x34445459 if off == 32 else 5      # identity present but frozen heartbeat
        link = self.link(box)
        with self.assertRaisesRegex(RuntimeError, 'No live'):
            link.wait_for_core(timeout=0.3, poll=0.05)
        live = self.link(FakeBox())
        live.wait_for_core(timeout=1, poll=0.01)


class Crop(unittest.TestCase):
    def test_crop_filters(self):
        p = profiles.BY_KEY['240p30']
        off = profiles.video_filter(p)
        self.assertEqual(off, profiles.video_filter(p, crop='Off'))
        self.assertTrue(off.startswith('fps=30'))                      # Off is exactly the validated filter
        wide = profiles.video_filter(p, crop='14:9')
        fill = profiles.video_filter(p, crop='Fill')
        self.assertTrue(wide.startswith("crop=w='min(iw,ih*14/9)':h=ih,fps=30"))
        self.assertTrue(fill.startswith("crop=w='min(iw,ih*4/3)':h=ih,fps=30"))
        self.assertTrue(wide.endswith(off))                            # crop only prepends; scaling/padding unchanged

    def test_crop_never_widens_a_narrow_picture(self):
        # The crop width is min(iw, ih*aspect): emulate it for a few real shapes.
        def crop_w(iw, ih, num, den):
            return min(iw, ih * num / den)
        self.assertAlmostEqual(crop_w(1280, 720, 4, 3), 960)           # 16:9 -> 4:3 (Fill)
        self.assertAlmostEqual(crop_w(426, 240, 14, 9), 373.33, 1)     # 16:9 -> 14:9
        self.assertEqual(crop_w(320, 240, 14, 9), 320)                 # 4:3 stays whole
        self.assertEqual(crop_w(240, 426, 4, 3), 240)                  # vertical short stays whole

    def test_decoder_command_carries_the_crop(self):
        from pathlib import Path
        rt = type('R', (), {'ffmpeg': Path('ffmpeg'), 'ca': Path('ca.pem')})()
        src = {'url': 'http://h/x', 'headers': {}}
        cmd = pipeline.decoder_command(rt, profiles.BY_KEY['240p30'], src, 'video', crop='Fill')
        self.assertIn("crop=w='min(iw,ih*4/3)':h=ih", cmd[cmd.index('-vf') + 1])
        audio = pipeline.decoder_command(rt, profiles.BY_KEY['240p30'], src, 'audio', crop='Fill')
        self.assertNotIn('-vf', audio)


class UiText(unittest.TestCase):
    def test_wrap_and_clip(self):
        self.assertEqual(ui.wrap('aaaa bbbb cccc', 9, 3), ['aaaa bbbb', 'cccc'])
        self.assertEqual(len(ui.wrap('x' * 100, 10, 2)), 2)
        self.assertEqual(ui.clip('abcdefghij', 6), 'abcd..')
        self.assertEqual(ui.mmss(3725000), '1:02:05')




class CoreInput(unittest.TestCase):
    """Controller/keyboard as the core publishes them in the mailbox."""

    def setUp(self):
        self.w = {40: 0, 44: 0, 48: 0, 52: 0}
        box = type('B', (), {'read': lambda _s, off: self.w[off]})()
        self.inp = inputs.Input(evdev=False, tty=False)
        self.inp.attach_core(box)

    def drain(self):
        out = []
        while True:
            a = self.inp.get()
            if a is None:
                return out
            out.append(a)

    def test_dpad_and_buttons(self):
        self.w[40] = 1 << 3                                   # up
        self.assertEqual(self.drain(), ['up'])
        self.w[40] = (1 << 3) | (1 << 4)                       # up still held + A
        self.assertEqual(self.drain(), ['ok'])
        self.w[40] = 0
        self.assertEqual(self.drain(), [])
        self.assertEqual(self.inp.held, {})
        self.w[44] = 1 << 5                                    # second pad, B
        self.assertEqual(self.drain(), ['back'])

    def test_stale_state_at_attach_is_ignored(self):
        self.w[40] = 1 << 4
        box = type('B', (), {'read': lambda _s, off: self.w[off]})()
        self.inp.attach_core(box)
        self.assertEqual(self.drain(), [])

    def test_keyboard_events_in_order_and_seq(self):
        make = lambda code, ext=0, pressed=1: (pressed << 9) | (ext << 8) | code
        # three events since last poll: Down(ext 0x72) press, Enter press, Enter release
        e_old, e_mid, e_new = make(0x72, 1), make(0x5A), make(0x5A, pressed=0)
        self.w[48] = e_new | (e_mid << 16)
        self.w[52] = e_old | (3 << 16)
        self.assertEqual(self.drain(), ['down', 'ok'])
        self.w[48] = make(0x76) ; self.w[52] = 4 << 16        # Esc
        self.assertEqual(self.drain(), ['back'])


class MenuStreaming(unittest.TestCase):
    def test_menu_queues_enough_for_the_core_to_start(self):
        # The real core waits for 4 queued records; a menu that queues fewer shows nothing.
        from ytcrt import app as appmod
        self.assertGreaterEqual(appmod.MENU_DEPTH, core_link.FakeLink.PREFILL)
        link = core_link.FakeLink()
        for _ in range(appmod.MENU_DEPTH):
            if link.queued < appmod.MENU_DEPTH:
                link.push(bytes(160000))
        time.sleep(0.2)
        self.assertGreater(link.consumed, 0)


FIX = Path(__file__).resolve().parent / 'fixtures'


def fixture(name):
    import json
    lines = (FIX / (name + '.json')).read_text(encoding='utf-8').splitlines()
    return json.loads(lines[0]), json.loads(lines[1])      # (info, formats) as yt-dlp --print gives them


def stream(w, h, tbr, fps=30, fid=None, vcodec='avc1.4D401E'):
    return {'format_id': fid or '%dx%d' % (w, h), 'protocol': 'https', 'vcodec': vcodec, 'acodec': 'none',
            'width': w, 'height': h, 'fps': fps, 'tbr': tbr, 'ext': 'mp4',
            'url': 'https://example.invalid/videoplayback?c=VISIONOS', 'http_headers': {'User-Agent': 'x'}}


AUDIO = [{'format_id': '140', 'protocol': 'https', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'ext': 'm4a', 'tbr': 129.5,
          'url': 'https://example.invalid/a', 'http_headers': {}},
         {'format_id': '140-drc', 'protocol': 'https', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'ext': 'm4a', 'tbr': 130,
          'url': 'https://example.invalid/b', 'http_headers': {}},
         {'format_id': '251', 'protocol': 'https', 'vcodec': 'none', 'acodec': 'opus', 'ext': 'webm', 'tbr': 120,
          'url': 'https://example.invalid/c', 'http_headers': {}}]


class CostModel(unittest.TestCase):
    def test_the_calibration_stream_costs_what_was_measured(self):
        cal = profiles.CAL_DEFAULT
        ref = {'width': 640, 'height': 360, 'tbr': 591, 'fps': 30}
        self.assertAlmostEqual(profiles.predicted_cost(ref, cal), cal['cost'], places=3)

    def test_fewer_frames_cost_less(self):
        a = {'width': 640, 'height': 360, 'tbr': 350, 'fps': 24}
        b = dict(a, fps=30)
        self.assertAlmostEqual(profiles.predicted_cost(a) / profiles.predicted_cost(b), 0.8, places=3)

    def test_more_pixels_and_more_bits_cost_more(self):
        small = {'width': 426, 'height': 240, 'tbr': 210, 'fps': 30}
        self.assertLess(profiles.predicted_cost(small), profiles.predicted_cost(dict(small, width=640, height=360)))
        self.assertLess(profiles.predicted_cost(small), profiles.predicted_cost(dict(small, tbr=600)))
        self.assertAlmostEqual(profiles.predicted_cost(small, mult=2.0), 2 * profiles.predicted_cost(small), places=6)

    def test_labels(self):
        self.assertEqual(profiles.label_for(640, 360), '360p')
        self.assertEqual(profiles.label_for(480, 360), '360p')      # 4:3 360p
        self.assertEqual(profiles.label_for(240, 426), '240p')      # vertical short
        self.assertEqual(profiles.label_for(854, 480), '480p')


class ChooseStreams(unittest.TestCase):
    """Stream choice: real yt-dlp output (URLs anonymised) plus constructed 90s-TV style catalogues."""

    def setUp(self):
        self.p = profiles.BY_KEY['240p30']

    def pick(self, formats, quality='Auto', **kw):
        info = {'id': 'x' * 11, 'title': 't', 'live_status': 'not_live', 'http_headers': {}}
        return pipeline.choose_streams(info, formats + AUDIO, self.p, quality, **kw)

    def test_real_busy_video_picks_240p_in_auto(self):
        info, fm = fixture('landscape')                              # its 360p stream is 452 kbps: too heavy here
        v, a, label, cost, why = pipeline.choose_streams(info, fm, self.p, 'Auto')
        self.assertEqual((v['format_id'], v['width'], v['height'], label), ('133', 426, 240, '240p'))
        self.assertEqual(a['format_id'], '140')                      # AAC medium: not opus, not the DRC copy
        self.assertIn('would need', why)

    def test_manual_choices_ignore_cost(self):
        info, fm = fixture('landscape')
        self.assertEqual(pipeline.choose_streams(info, fm, self.p, '240p')[0]['format_id'], '133')
        self.assertEqual(pipeline.choose_streams(info, fm, self.p, '360p')[0]['format_id'], '134')
        v = pipeline.choose_streams(info, fm, self.p, '480p')[0]
        self.assertEqual((v['width'], v['height']), (854, 480))

    def test_vertical_short(self):
        info, fm = fixture('vertical')
        v = pipeline.choose_streams(info, fm, self.p, '240p')[0]
        self.assertEqual((v['width'], v['height']), (240, 426))
        v = pipeline.choose_streams(info, fm, self.p, '360p')[0]
        self.assertEqual((v['width'], v['height']), (360, 640))

    def test_a_light_90s_tv_stream_gets_360p(self):
        # Low-motion SD upload: 360p is only 155 kbps (like a real sitcom upload).
        v, a, label, cost, why = self.pick([stream(426, 240, 89), stream(640, 360, 155), stream(854, 480, 229)])
        self.assertEqual(label, '360p')
        self.assertLessEqual(cost, profiles.LIMIT)

    def test_24_fps_content_is_allowed_a_bigger_picture_than_30_fps(self):
        # Same pixels and bitrate; only the frame rate differs. 24 fps decodes 20% faster, which tips the choice.
        v24 = self.pick([stream(426, 240, 126, 24), stream(640, 360, 349, 24)])
        v30 = self.pick([stream(426, 240, 126, 30), stream(640, 360, 349, 30)])
        self.assertEqual(v24[2], '360p')
        self.assertEqual(v30[2], '240p')

    def test_4_3_content_picks_480x360_not_a_widescreen_box(self):
        v, a, label, cost, why = self.pick([stream(320, 240, 90), stream(480, 360, 150), stream(640, 480, 260)])
        self.assertEqual((v['width'], v['height'], label), (480, 360, '360p'))

    def test_a_faster_calibration_unlocks_480p(self):
        fast = {'px': 640 * 360, 'tbr': 591.0, 'fps': 30.0, 'cost': 0.35}     # an ARM twice as fast as measured
        v, a, label, cost, why = self.pick([stream(426, 240, 89), stream(640, 360, 155), stream(854, 480, 229)], cal=fast)
        self.assertEqual(label, '480p')

    def test_nothing_fits_uses_the_lightest(self):
        v, a, label, cost, why = self.pick([stream(426, 240, 400), stream(640, 360, 900)], mult=10.0)
        self.assertEqual(label, '240p')
        self.assertIn('lightest', why)

    def test_h264_beats_other_codecs(self):
        vp9 = stream(640, 360, 200, fid='vp9', vcodec='vp09.00.21.08')
        v, a, label, cost, why = self.pick([stream(426, 240, 100, fid='avc240'), vp9], quality='360p')
        self.assertEqual(v['format_id'], 'avc240')

    def test_past_livestream_is_refused(self):
        info, fm = fixture('postlive')
        with self.assertRaisesRegex(ValueError, 'livestream'):
            pipeline.choose_streams(info, fm, self.p, 'Auto')

    def test_segmented_formats_alone_are_refused_clearly(self):
        info, fm = fixture('postlive')
        info = dict(info, live_status='not_live')
        with self.assertRaisesRegex(ValueError, 'past livestream'):
            pipeline.choose_streams(info, fm, self.p, 'Auto')

    def test_bad_urls_and_headers_are_rejected(self):
        info, fm = fixture('landscape')
        bad = [dict(f, url='file:///etc/passwd') if f['format_id'] == '133' else f for f in fm]
        v = pipeline.choose_streams(info, bad, self.p, '240p')[0]
        self.assertNotEqual(v['format_id'], '133')                   # a non-http URL is never chosen
        evil = [dict(f, http_headers={'X': 'a\r\nHost: evil'}) if f['format_id'] == '133' else f for f in fm]
        with self.assertRaisesRegex(ValueError, 'header'):
            pipeline.choose_streams(info, evil, self.p, '240p')

    def test_step_down_gives_a_lighter_stream_from_the_same_catalogue(self):
        info, fm = fixture('landscape')
        media = pipeline.media_from(info, fm, self.p, '360p')
        self.assertEqual((media.label, round(media.tbr)), ('360p', 452))
        smaller = pipeline.step_down(media, self.p)
        self.assertEqual((smaller.label, smaller.video['format_id']), ('240p', '133'))
        self.assertLess(smaller.cost, media.cost)
        self.assertIsNone(pipeline.step_down(smaller, self.p))       # nothing lighter than 240p

    def test_command_prints_small_catalogue_and_has_no_format_expression(self):
        rt = type('R', (), {'resolver': Path('yt-dlp'), 'qjs': Path('qjs'), 'ffdir': Path('.')})()
        cmd = pipeline.resolve_command(rt, self.p, 'https://www.youtube.com/watch?v=x')
        self.assertNotIn('-f', cmd)
        self.assertEqual(cmd.count('--print'), 2)


class FakePipe:
    """Stands in for RecordPipeline: `rate` records/s, or raises `failures` times first."""

    def __init__(self, rate=500, failures=()):
        self.rate, self.t0, self.n, self.failures = rate, time.monotonic(), 0, list(failures)

    def poll(self):
        if self.failures:
            raise RuntimeError(self.failures.pop(0))
        if self.n >= (time.monotonic() - self.t0) * self.rate:
            return 'wait', None
        self.n += 1
        return 'record', bytes(160000)

    def close(self, wait=True):
        pass


class PlayPolicy(unittest.TestCase):
    """The player's quality and retry behaviour, driven with a fake decoder."""

    def make(self, quality='Auto', cost=0.3, mult=1.0):
        from ytcrt import app as appmod
        self.appmod = appmod
        tmp = tempfile.mkdtemp()
        st = store.Store(tmp)
        st.settings.update(quality=quality, cost_mult=mult,
                           cal={'px': 640 * 360, 'tbr': 591.0, 'fps': 30.0, 'cost': cost})
        rt = type('Rt', (), {'ffmpeg': Path(tmp) / 'nope', 'missing': lambda self: []})()
        self.inp = inputs.Input(evdev=False, tty=False)
        self.app = appmod.App(core_link.FakeLink(), st, offline_feeds(tmp + '/cache'), rt, self.inp, lambda m: None)
        self.resolved, self.opened = [], []
        self.saved = (pipeline.resolve, pipeline.open_pipeline, appmod.STARVE_GRACE, appmod.STARVE_WINDOW,
                      appmod.STARVE_LIMIT, appmod.SMOOTH_AFTER)
        info, fm = fixture('landscape')

        def fake_resolve(rt, prof, vid, cancel, log, note=None, quality='Auto', cal=None, mult=1.0):
            self.resolved.append(quality)
            return pipeline.media_from(info, fm, prof, quality, cal, mult)
        pipeline.resolve = fake_resolve
        appmod.STARVE_GRACE, appmod.STARVE_WINDOW, appmod.STARVE_LIMIT = 0.3, 1.0, 0.2
        return st

    def tearDown(self):
        pipeline.resolve, pipeline.open_pipeline = self.saved[0], self.saved[1]
        (self.appmod.STARVE_GRACE, self.appmod.STARVE_WINDOW, self.appmod.STARVE_LIMIT,
         self.appmod.SMOOTH_AFTER) = self.saved[2:]

    def run_play(self, pipes, stop_when):
        import threading
        pipes = list(pipes)

        def fake_open(rt, prof, media, start_s, speed, cancel, crop='Off'):
            self.opened.append((media.label, round(start_s)))
            return pipes.pop(0) if len(pipes) > 1 else pipes[0]
        pipeline.open_pipeline = fake_open

        def stopper():
            end = time.monotonic() + 20
            while time.monotonic() < end and not stop_when():
                time.sleep(0.05)
            time.sleep(0.4)
            self.inp.push('back')
        threading.Thread(target=stopper, daemon=True).start()
        self.app.play({'id': 'v' * 11, 'title': 't'}, 0)

    def test_a_fast_arm_gets_360p_and_a_slow_one_steps_down_and_learns(self):
        st = self.make(cost=0.7)                         # calibration says 360p@452k needs ~62% of the ARM
        self.run_play([FakePipe(rate=4), FakePipe()], lambda: len(self.opened) >= 2)
        self.assertEqual([t for t, _ in self.opened], ['360p', '240p'])   # it really starved, so it stepped down
        self.assertEqual(len(self.resolved), 1)          # the catalogue was reused: no second yt-dlp run
        self.assertGreater(st.settings['cost_mult'], 1.0)                 # and the estimate was corrected upwards

    def test_clean_playback_trusts_the_estimate_a_little_more(self):
        st = self.make(cost=0.9)                         # 360p@452k predicted ~0.79: allowed, and close to the limit
        self.appmod.SMOOTH_AFTER = 0.5
        self.run_play([FakePipe()], lambda: st.settings['cost_mult'] < 1.0)
        self.assertEqual(self.opened[0][0], '360p')
        self.assertLess(st.settings['cost_mult'], 1.0)

    def test_the_default_calibration_sends_a_busy_stream_to_240p(self):
        self.make(cost=profiles.CAL_DEFAULT['cost'])
        self.run_play([FakePipe()], lambda: self.app.link.sent > 20)
        self.assertEqual(self.opened[0][0], '240p')

    def test_manual_choices_never_fall_back(self):
        st = self.make(quality='360p')
        self.run_play([FakePipe(rate=4)], lambda: self.app.link.sent > 6)
        self.assertEqual([t for t, _ in self.opened], ['360p'])       # slow, but it stays: that is what was asked
        self.assertEqual(st.settings['cost_mult'], 1.0)

    def test_a_random_403_is_retried_and_playback_continues(self):
        self.make(quality='240p')
        refusal = 'video decoder failed (8): Server returned 403 Forbidden (access denied)'
        self.run_play([FakePipe(failures=[refusal, refusal]), FakePipe(failures=[refusal]), FakePipe()],
                      lambda: self.app.link.sent > 20)
        self.assertEqual(len(self.opened), 3)            # opened, retried, retried
        self.assertEqual(len(self.resolved), 1)          # no new yt-dlp run needed

    def test_a_persistent_403_gets_fresh_urls_once_then_reports(self):
        self.make(quality='240p')
        refusal = 'video decoder failed (8): Server returned 403 Forbidden (access denied)'
        always = FakePipe(failures=[refusal] * 40)
        self.run_play([always], lambda: len(self.app.stack) > 1 and type(self.app.screen).__name__ == 'TextView')
        self.assertEqual(len(self.resolved), 2)          # one refresh of the stream URLs
        self.assertEqual(type(self.app.screen).__name__, 'TextView')   # then a clear error screen
        self.assertIn('403', ' '.join(self.app.screen.lines))


class TypedLinks(unittest.TestCase):
    def test_links_and_ids(self):
        for text in ('M7kB0lis3xg', ' https://www.youtube.com/watch?v=M7kB0lis3xg&t=90s ', 'youtu.be/M7kB0lis3xg?si=x',
                     'm.youtube.com/watch?v=M7kB0lis3xg', 'https://youtube.com/shorts/M7kB0lis3xg'):
            self.assertEqual(urls.normalise_url(text)[1], 'M7kB0lis3xg', text)
        self.assertEqual(urls.normalise_url('M7kB0lis3xg')[0], 'https://www.youtube.com/watch?v=M7kB0lis3xg')

    def test_refusals_have_messages(self):
        for text in ('', 'hello', 'https://www.youtube.com/@RetroRGB', 'https://www.youtube.com/playlist?list=PL1',
                     'https://example.com/watch?v=M7kB0lis3xg', 'ftp://youtu.be/M7kB0lis3xg',
                     'https://user:pw@www.youtube.com/watch?v=M7kB0lis3xg'):
            with self.assertRaises(ValueError, msg=text):
                urls.normalise_url(text)

    def test_channel_refs_and_adding(self):
        d = tempfile.mkdtemp()
        st = store.Store(d)
        uc = 'UC' + 'a' * 22
        self.assertEqual(st.parse_channel_ref(uc), uc)
        self.assertEqual(st.parse_channel_ref('https://www.youtube.com/channel/' + uc), uc)
        self.assertEqual(st.parse_channel_ref('https://www.youtube.com/@RetroRGB/videos'), '@RetroRGB')
        self.assertIsNone(st.parse_channel_ref('just some words'))
        self.assertTrue(st.add_subscription(uc))
        self.assertFalse(st.add_subscription(uc))                     # no duplicates
        self.assertTrue(st.add_subscription('@RetroRGB'))
        self.assertEqual([r for _, r in st.subscriptions()], [uc, '@RetroRGB'])


class Typing(unittest.TestCase):
    def keyboard_input(self):
        w = {40: 0, 44: 0, 48: 0, 52: 0}
        box = type('B', (), {'read': lambda _s, off: w[off]})()
        inp = inputs.Input(evdev=False, tty=False)
        inp.attach_core(box)
        seq = [0]

        def press(*events):                                   # events: (ext, code, down), oldest first (max 3)
            ev = [(e[2] << 9) | (e[0] << 8) | e[1] for e in events][::-1] + [0, 0]
            w[48], w[52] = ev[0] | (ev[1] << 16), ev[2] | ((seq[0] + len(events)) << 16)
            seq[0] += len(events)
        return inp, press

    def drain(self, inp):
        out = []
        while True:
            a = inp.get()
            if a is None:
                return out
            out.append(a)

    def test_real_keyboard_types_characters_with_shift(self):
        inp, press = self.keyboard_input()
        inp.text_mode = True
        press((0, 0x12, 1), (0, 0x1C, 1), (0, 0x12, 0))               # Shift down, a, Shift up
        self.assertEqual(self.drain(inp), ['ch:A'])
        press((0, 0x4A, 1), (0, 0x4A, 0), (0, 0x16, 1))                # /, release, 1
        self.assertEqual(self.drain(inp), ['ch:/', 'ch:1'])
        press((0, 0x66, 1), (0, 0x5A, 1), (0, 0x76, 1))                # Backspace, Enter, Esc
        self.assertEqual(self.drain(inp), ['bksp', 'submit', 'back'])
        press((0, 0x12, 1), (0, 0x4C, 1), (0, 0x12, 0))                # Shift+; = ':'
        self.assertEqual(self.drain(inp), ['ch:' + ':'])

    def test_keys_navigate_when_no_text_box_is_showing(self):
        inp, press = self.keyboard_input()
        inp.text_mode = False
        press((1, 0x75, 1), (1, 0x75, 0), (0, 0x5A, 1))                # Up, release, Enter
        self.assertEqual(self.drain(inp), ['up', 'ok'])

    def make_app(self):
        from ytcrt import app as appmod
        tmp = tempfile.mkdtemp()
        st = store.Store(tmp)
        rt = type('Rt', (), {'ffmpeg': Path(tmp) / 'nope', 'missing': lambda self: []})()
        a = appmod.App(core_link.FakeLink(), st, offline_feeds(tmp + '/cache'), rt,
                       inputs.Input(evdev=False, tty=False), lambda m: None)
        return a, st

    def test_on_screen_keyboard_types_and_plays(self):
        a, st = self.make_app()
        played = []
        a.start_video = lambda v: played.append(v)
        a.open_home_entry('Play URL / ID')
        screen = a.screen
        self.assertIsInstance(screen, ui.TextInput)
        for ch in 'M7kB0lis3xg':                                     # type via the physical-keyboard path
            screen.handle(a, 'ch:' + ch)
        self.assertEqual(screen.text, 'M7kB0lis3xg')
        screen.handle(a, 'bksp')
        screen.handle(a, 'ch:g')
        # now drive the grid: Y shifts, A presses the highlighted key (row 1 = q w e r ...; start at 'q')
        screen.handle(a, 'alt')
        self.assertTrue(screen.shift)
        screen.handle(a, 'ok')
        self.assertTrue(screen.text.endswith('Q'))
        screen.handle(a, 'menu')                                      # X deletes
        self.assertTrue(screen.text.endswith('g'))
        screen.handle(a, 'submit')                                    # Enter on a real keyboard
        self.assertEqual([v['id'] for v in played], ['M7kB0lis3xg'])
        self.assertIsInstance(a.screen, ui.Home)                      # the typing screen is gone
        self.assertFalse(getattr(a.screen, 'wants_text', False))

    def test_bad_text_shows_an_error_and_stays(self):
        a, st = self.make_app()
        a.open_home_entry('Play URL / ID')
        screen = a.screen
        for ch in 'nonsense':
            screen.handle(a, 'ch:' + ch)
        screen.handle(a, 'start')
        self.assertIn('not a single video link', screen.error)
        self.assertIs(a.screen, screen)

    def test_grid_navigation_and_special_keys(self):
        a, st = self.make_app()
        a.open_home_entry('Add to Lists')
        s = a.screen
        s.handle(a, 'ok')                                             # row 1 col 0 = 'q'
        self.assertEqual(s.text, 'q')
        for _ in range(4):
            s.handle(a, 'down')                                      # rows 2,3,4 then the special row
        self.assertEqual((s.row, s.col), (len(s.ROWS), 0))
        s.handle(a, 'right')                                          # Shift -> Del
        s.handle(a, 'ok')
        self.assertEqual(s.text, '')                                  # Del removed the 'q'
        for ch in '@RetroRGB':
            s.handle(a, 'ch:' + ch)
        s.handle(a, 'submit')
        self.assertEqual([r for _, r in st.subscriptions()], ['@RetroRGB'])
        self.assertIsInstance(a.screen, ui.Home)

    def test_add_channel_by_handle(self):
        a, st = self.make_app()
        a.open_home_entry('Add Channel via @handle')
        s = a.screen
        self.assertEqual(s.text, '@')                                 # starts with the @ already typed
        for ch in 'retrorgb':
            s.handle(a, 'ch:' + ch)
        s.handle(a, 'submit')
        self.assertEqual([r for _, r in st.subscriptions()], ['@retrorgb'])
        self.assertIsInstance(a.screen, ui.Home)
        a.open_home_entry('Add Channel via @handle')
        a.screen.handle(a, 'bksp')                                    # forgot the @: it is added for you
        for ch in 'newchannel':
            a.screen.handle(a, 'ch:' + ch)
        a.screen.handle(a, 'submit')
        self.assertEqual([r for _, r in st.subscriptions()], ['@retrorgb', '@newchannel'])
        a.open_home_entry('Add Channel via @handle')
        a.screen.handle(a, 'ch:a')
        a.screen.handle(a, 'submit')                                  # "@a" is too short to be a handle
        self.assertIn('@youtuberuser', a.screen.error)
        self.assertEqual(len(st.subscriptions()), 2)

    def test_screen_draws(self):
        a, st = self.make_app()
        a.open_home_entry('Play URL / ID')
        a.render()
        self.assertEqual(len(a.record), 160000)


class ChannelProblems(unittest.TestCase):
    def make(self):
        d = tempfile.mkdtemp()
        f = feeds.Feeds(d)
        logged = []
        f.log = logged.append
        return f, logged

    def test_a_missing_handle_is_reported_not_swallowed(self):
        import urllib.error
        f, logged = self.make()

        def fake_find(url, patterns, limit=0):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        f._find_in_page = fake_find
        f._refresh_one('@8-BitGuy', None, False)
        self.assertEqual(f.status['@8-BitGuy'], 'not found (check the spelling)')
        self.assertTrue(any('8-BitGuy' in m and 'not found' in m for m in logged))
        rows = f.channel_list([(None, '@8-BitGuy')])
        self.assertEqual(rows, [('@8-BitGuy', '@8-BitGuy', -1, 'not found (check the spelling)')])

    def test_failed_and_working_channels_are_both_listed(self):
        f, logged = self.make()
        uc = 'UC' + 'b' * 22
        f.channels[uc] = {'name': 'Good', 'videos': [{'id': 'x' * 11, 'title': 't', 'published': 1, 'short': False}],
                          'at': time.time(), 'ok': True}
        rows = f.channel_list([(None, uc), ('Typo', '@nope')])
        self.assertEqual(rows[0][2:], (1, ''))
        self.assertEqual(rows[1][1], 'Typo')
        self.assertEqual(rows[1][2], -1)
        self.assertTrue(rows[1][3])

    def test_handle_lookup_reads_only_the_start_of_the_page(self):
        import http.server
        import threading
        uc = 'UC' + 'c' * 22
        head = ('<html><head><link rel="canonical" href="https://www.youtube.com/channel/%s"></head>' % uc).encode()
        sent = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                try:
                    self.wfile.write(head)
                    for _ in range(100):                          # a huge page: 3 MB of padding
                        self.wfile.write(b'x' * 32768)
                        sent.append(1)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            f, logged = self.make()
            got = f._find_in_page('http://127.0.0.1:%d/@x' % srv.server_address[1],
                                  (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',))
        finally:
            srv.shutdown()
        self.assertEqual(got, uc)
        self.assertLess(len(sent), 60)                                # stopped early, did not download it all


class FlakyRss(unittest.TestCase):
    """YouTube's RSS endpoint returns 404/500 at random; channels must still load."""

    def setUp(self):
        self.saved = feeds.RSS_BACKOFF
        feeds.RSS_BACKOFF = 0
        self.f = feeds.Feeds(tempfile.mkdtemp())
        self.logged = []
        self.f.log = self.logged.append
        self.uc = 'UC' + 'd' * 22

    def tearDown(self):
        feeds.RSS_BACKOFF = self.saved

    def http(self, code):
        import urllib.error
        return urllib.error.HTTPError('u', code, 'x', {}, None)

    def test_random_errors_are_retried(self):
        calls = []

        def get(url, **kw):
            calls.append(url)
            if len(calls) < 3:
                raise self.http(404 if len(calls) == 1 else 500)
            return ATOM
        self.f._get = get
        self.f._refresh_one(self.uc, 'Chan', True)
        self.assertEqual(len(calls), 3)
        self.assertTrue(self.f.status[self.uc].startswith('ok'))
        self.assertEqual(len(self.f.channel_videos(self.uc)), 2)

    def test_yt_dlp_fallback_when_rss_keeps_failing(self):
        self.f._get = lambda url, **kw: (_ for _ in ()).throw(self.http(404))
        self.f.fallback = lambda cid: [{'id': 'a' * 11, 'title': 'T1', 'channel': 'Real Name', 'published': 100,
                                        'approx': True, 'short': False, 'views': 0}]
        self.f._refresh_one(self.uc, None, True)
        self.assertIn('no dates', self.f.status[self.uc])
        rows = self.f.channel_list([(None, self.uc)])
        self.assertEqual((rows[0][1], rows[0][2], rows[0][3]), ('Real Name', 1, ''))
        self.assertTrue(any('asking yt-dlp' in m for m in self.logged))

    def test_failure_everywhere_is_reported(self):
        self.f._get = lambda url, **kw: (_ for _ in ()).throw(self.http(500))
        self.f.fallback = lambda cid: (_ for _ in ()).throw(RuntimeError('yt-dlp broke'))
        self.f._refresh_one(self.uc, None, True)
        self.assertEqual(self.f.status[self.uc], 'YouTube answered HTTP 500')
        self.assertEqual(self.f.channel_list([(None, self.uc)])[0][2], -1)

    def test_a_cached_channel_survives_a_failed_refresh(self):
        self.f.channels[self.uc] = {'name': 'Old', 'videos': [{'id': 'b' * 11, 'title': 'x', 'published': 5, 'short': False}],
                                    'at': 1, 'ok': True}
        self.f._get = lambda url, **kw: (_ for _ in ()).throw(self.http(500))
        self.f._refresh_one(self.uc, None, True)
        self.assertEqual(len(self.f.channel_videos(self.uc)), 1)       # still shows what it had

    def test_undated_videos_show_no_age(self):
        a = type('A', (), {'store': store.Store(tempfile.mkdtemp())})()
        lst = ui.VideoList('t', lambda app: [])
        self.assertEqual(lst.right(a, {'id': 'x' * 11, 'published': 1, 'approx': True}), '')
        self.assertNotEqual(lst.right(a, {'id': 'x' * 11, 'published': time.time() - 7200}), '')


class MixedFile(unittest.TestCase):
    """subscriptions.txt holds channels, playlists and single videos together."""

    UC = 'UC' + 'a' * 22
    PL = 'PL' + 'b' * 32

    def make(self, text):
        d = tempfile.mkdtemp()
        st = store.Store(d)
        Path(st.p_subs).write_text(text, encoding='utf-8')
        return st

    def test_each_line_is_sorted_by_kind(self):
        st = self.make('# my list\n' + self.UC + '\n@RetroRGB\nRetro Mix | ' + self.PL + '\n'
                       'https://www.youtube.com/playlist?list=' + self.PL + '\n'          # duplicate playlist
                       'M7kB0lis3xg\nOld Clip | https://youtu.be/EafWURqb2vc?t=30\nnonsense line\n')
        self.assertEqual([r for _, r in st.subscriptions()], [self.UC, '@RetroRGB'])
        self.assertEqual(st.playlists(), [('Retro Mix', self.PL)])                          # named, de-duplicated
        self.assertEqual(st.video_list(), [(None, 'M7kB0lis3xg'), ('Old Clip', 'EafWURqb2vc')])

    def test_classify(self):
        c = store.Store.classify
        self.assertEqual(c('M7kB0lis3xg'), ('video', 'M7kB0lis3xg'))
        self.assertEqual(c('https://www.youtube.com/watch?v=M7kB0lis3xg&list=' + self.PL), ('playlist', self.PL))   # filed as the playlist
        self.assertEqual(c('https://www.youtube.com/watch?v=M7kB0lis3xg&list=RD' + 'a' * 20), ('video', 'M7kB0lis3xg'))   # a Mix is not
        self.assertEqual(c('https://www.youtube.com/playlist?list=' + self.PL), ('playlist', self.PL))
        self.assertEqual(c(self.PL), ('playlist', self.PL))
        self.assertEqual(c('UU' + 'c' * 22), ('playlist', 'UU' + 'c' * 22))        # a channel's uploads playlist
        self.assertEqual(c(self.UC), ('channel', self.UC))
        self.assertEqual(c('https://www.youtube.com/@RetroRGB/videos'), ('channel', '@RetroRGB'))
        self.assertEqual(c('nonsense')[0], None)
        self.assertEqual(c('')[0], None)

    def test_add_entry_goes_to_the_file_without_duplicates(self):
        st = self.make('')
        self.assertTrue(st.add_entry('playlist', self.PL))
        self.assertFalse(st.add_entry('playlist', self.PL))
        self.assertTrue(st.add_entry('video', 'M7kB0lis3xg'))
        self.assertEqual(len(st.entries()), 2)
        self.assertEqual(Path(st.p_subs).read_text().split(), [self.PL, 'M7kB0lis3xg'])

    def test_adding_to_a_file_without_a_final_newline_does_not_glue_lines(self):
        st = self.make(self.UC)                          # no trailing newline, like a hand-edited file
        self.assertTrue(st.add_entry('playlist', self.PL))
        self.assertEqual(Path(st.p_subs).read_text().split(), [self.UC, self.PL])
        self.assertEqual(len(st.entries()), 2)

    def test_old_channels_only_files_still_work(self):
        st = self.make('Name | ' + self.UC + '\n@RetroRGB\n')
        self.assertEqual(st.subscriptions(), [('Name', self.UC), (None, '@RetroRGB')])
        self.assertEqual((st.playlists(), st.video_list()), ([], []))


class ListsInTheGui(unittest.TestCase):
    PL = 'PL' + 'b' * 32

    def make_app(self, text=''):
        from ytcrt import app as appmod
        tmp = tempfile.mkdtemp()
        st = store.Store(tmp)
        Path(st.p_subs).write_text(text, encoding='utf-8')
        rt = type('Rt', (), {'ffmpeg': Path(tmp) / 'nope', 'missing': lambda self: []})()
        a = appmod.App(core_link.FakeLink(), st, offline_feeds(tmp + '/cache'), rt,
                       inputs.Input(evdev=False, tty=False), lambda m: None)
        return a, st

    def test_playlist_contents_and_failures_are_listed(self):
        a, st = self.make_app(self.PL + '\nPL' + 'z' * 32 + '\n')
        vids = [{'id': 'a' * 11, 'title': 'First', 'channel': 'C', 'published': 0, 'approx': True, 'short': False, 'views': 0}]

        def fetch(pid):
            if pid.endswith('zz'):
                raise RuntimeError('ERROR: playlist does not exist')
            return 'My Playlist', vids
        a.feeds.playlist_fetch = fetch
        a.feeds.refresh_lists(st.playlists(), st.video_list(), True).join(10)
        rows = a.feeds.playlist_list(st.playlists())
        self.assertEqual(rows[0][1:], ('My Playlist', 1, ''))
        self.assertEqual(rows[1][2], -1)
        self.assertIn('does not exist', rows[1][3])
        self.assertEqual([v['title'] for v in a.feeds.playlist_videos(self.PL)], ['First'])
        a.open_home_entry('Playlists')
        self.assertIsInstance(a.screen, ui.PlaylistList)
        a.screen.activate(a, rows[0])                                  # opens the playlist's videos
        self.assertIsInstance(a.screen, ui.VideoList)
        self.assertEqual(a.screen.right(a, vids[0]), '')               # playlist entries show no age
        a.pop()
        a.screen.activate(a, rows[1])                                  # a failed one explains itself
        self.assertIsInstance(a.screen, ui.TextView)

    def test_video_titles_come_from_a_light_lookup_and_the_file_name_wins(self):
        a, st = self.make_app('M7kB0lis3xg\nOld Clip | EafWURqb2vc\nbadbadbad12\n')
        calls = []

        def oembed(vid):
            calls.append(vid)
            if vid == 'badbadbad12':
                raise RuntimeError('gone')
            return {'title': 'Real Title', 'channel': 'Some Channel'}
        a.feeds._oembed = oembed
        before = a.feeds.listed_videos(st.video_list())
        self.assertEqual(before[0]['title'], '(loading) M7kB0lis3xg')
        a.feeds.refresh_lists([], st.video_list(), True).join(10)
        got = a.feeds.listed_videos(st.video_list())
        self.assertEqual(got[0]['title'], 'Real Title')
        self.assertEqual(got[1]['title'], 'Old Clip')                  # a name in the file beats the lookup
        self.assertTrue(got[2]['title'].startswith('(unavailable)'))
        a.feeds.refresh_lists([], st.video_list(), False).join(10)     # cached: only the failed one is retried
        self.assertEqual(calls.count('M7kB0lis3xg'), 1)

    def test_add_to_lists_files_each_kind_in_the_right_place(self):
        a, st = self.make_app()
        a.feeds.playlist_fetch = lambda pid: ('P', [])
        a.feeds._oembed = lambda vid: {'title': 't', 'channel': 'c'}
        for text, check in (('@RetroRGB', lambda: [r for _, r in st.subscriptions()] == ['@RetroRGB']),
                            ('https://www.youtube.com/playlist?list=' + self.PL, lambda: st.playlists() == [(None, self.PL)]),
                            ('youtu.be/M7kB0lis3xg', lambda: st.video_list() == [(None, 'M7kB0lis3xg')])):
            a.open_home_entry('Add to Lists')
            for ch in text:
                a.screen.handle(a, 'ch:' + ch)
            a.screen.handle(a, 'submit')
            self.assertTrue(check(), text)
            self.assertIsInstance(a.screen, ui.Home)                   # the typing screen closed itself
        a.open_home_entry('Add to Lists')
        for ch in 'gibberish':
            a.screen.handle(a, 'ch:' + ch)
        a.screen.handle(a, 'submit')
        self.assertIn('Not a channel, playlist or video', a.screen.error)   # and bad input stays put with a message

    def test_home_counts_and_screens(self):
        a, st = self.make_app('@RetroRGB\n' + self.PL + '\nM7kB0lis3xg\nEafWURqb2vc\n')
        home = a.screen
        self.assertEqual(home.right(a, 'Playlists'), '1')
        self.assertEqual(home.right(a, 'My Videos'), '2')
        a.open_home_entry('My Videos')
        a.render()
        self.assertEqual(len(a.record), 160000)


class SpeedTest(unittest.TestCase):
    def run_test(self, resolver):
        import threading
        from ytcrt import app as appmod
        tmp = tempfile.mkdtemp()
        st = store.Store(tmp)
        rt = type('Rt', (), {'ffmpeg': Path(tmp) / 'nope', 'resolver': Path(tmp) / 'nope', 'missing': lambda self: []})()
        a = appmod.App(core_link.FakeLink(), st, offline_feeds(tmp + '/cache'), rt,
                       inputs.Input(evdev=False, tty=False), lambda m: None)
        uc = 'UC' + 'e' * 22
        a.feeds.channels[uc] = {'name': 'C', 'at': time.time(), 'ok': True, 'videos': [
            {'id': 'live' + 'a' * 7, 'title': 'LIVE', 'channel': 'C', 'published': 3, 'short': False},
            {'id': 'live' + 'b' * 7, 'title': 'LIVE2', 'channel': 'C', 'published': 2, 'short': False}]}
        Path(st.p_subs).write_text(uc + '\n')
        saved = (pipeline.resolve, pipeline.benchmark)
        info, fm = fixture('landscape')
        tried = []

        def fake_resolve(rt, prof, vid, cancel, log, note=None, quality='Auto', cal=None, mult=1.0):
            tried.append(vid)
            return resolver(vid, info, fm, prof, quality)
        pipeline.resolve = fake_resolve
        pipeline.benchmark = lambda rt, prof, media, speed, seconds=10, cancel=None: {'Fast': 0.7, 'Fastest': 0.9}[speed]
        done = []
        threading.Thread(target=lambda: (a.run_benchmark(), done.append(1)), daemon=True).start()
        end = time.time() + 15
        try:
            while not done and time.time() < end:
                a.pump(0.01)
        finally:
            pipeline.resolve, pipeline.benchmark = saved
        return a, st, tried

    def test_livestreams_are_skipped_and_a_good_clip_is_used(self):
        def resolver(vid, info, fm, prof, quality):
            if vid.startswith('live'):
                raise ValueError(pipeline.LIVE_MESSAGE)
            return pipeline.media_from(info, fm, prof, quality)
        a, st, tried = self.run_test(resolver)
        self.assertEqual(tried[-1], 'M7kB0lis3xg')                     # fell through to the known good clip
        self.assertEqual(len([t for t in tried if t.startswith('live')]), 2)
        self.assertEqual(a.screen.title, 'Speed test')                  # success, not "Speed test failed"
        self.assertEqual(st.settings['speed_big'], 'Fastest')
        self.assertEqual(st.settings['cal']['cost'], round(1 / 0.9, 4))

    def test_when_nothing_works_it_says_so(self):
        def resolver(vid, info, fm, prof, quality):
            raise ValueError(pipeline.LIVE_MESSAGE)
        a, st, tried = self.run_test(resolver)
        self.assertEqual(a.screen.title, 'Speed test failed')
        self.assertIn('test clip', ' '.join(a.screen.lines))


def vid(i, **kw):
    return dict({'id': 'v%010d' % i, 'title': 'Video %d' % i, 'channel': 'C', 'published': 0, 'approx': True,
                 'short': False, 'views': 0}, **kw)


class Paging(unittest.TestCase):
    UC = 'UC' + 'p' * 22
    PL = 'PL' + 'q' * 32

    def make(self):
        f = feeds.Feeds(tempfile.mkdtemp())
        f.log = lambda m: None
        calls = []

        def page_fetch(pid, start, count):
            calls.append((pid, start, count))
            total = 140                                           # the "channel" has 140 uploads in all
            return 'T', [vid(i) for i in range(start - 1, min(total, start - 1 + count))]
        f.page_fetch = page_fetch
        return f, calls

    def seed_channel(self, f, n=15):
        f.channels[self.UC] = {'name': 'C', 'at': time.time(), 'ok': True,
                               'videos': [vid(i, published=1000 - i, approx=False) for i in range(n)]}

    def test_a_channel_grows_a_page_at_a_time_without_duplicates(self):
        f, calls = self.make()
        self.seed_channel(f)
        self.assertTrue(f.can_load_more('channel', self.UC))
        f.load_more('channel', self.UC).join(10)
        self.assertEqual(calls[0], ('UU' + 'p' * 22, 16, feeds.PAGE))              # the channel's uploads playlist, from #16
        vids = f.channel_videos(self.UC)
        self.assertEqual(len(vids), 15 + feeds.PAGE)
        self.assertEqual(len({v['id'] for v in vids}), len(vids))                  # no duplicates
        self.assertEqual([v['title'] for v in vids[:3]], ['Video 0', 'Video 1', 'Video 2'])
        self.assertEqual(vids[15]['title'], 'Video 15')                            # in order
        f.load_more('channel', self.UC).join(10)
        self.assertEqual(len(f.channel_videos(self.UC)), 15 + 2 * feeds.PAGE)       # 115
        f.load_more('channel', self.UC).join(10)                                   # the last, short page: 25 left
        self.assertEqual(len(f.channel_videos(self.UC)), 140)
        self.assertFalse(f.can_load_more('channel', self.UC))                      # nothing further to ask for

    def test_the_overlap_between_rss_and_the_first_page_is_removed(self):
        f, calls = self.make()
        self.seed_channel(f, 15)
        f.page_fetch = lambda pid, start, count: ('T', [vid(i) for i in range(10, 10 + count)])    # starts too early
        f.load_more('channel', self.UC).join(10)
        ids = [v['id'] for v in f.channel_videos(self.UC)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_playlists_page_too(self):
        f, calls = self.make()
        f.playlists[self.PL] = {'title': 'P', 'at': time.time(), 'videos': [vid(i) for i in range(feeds.PAGE)]}
        self.assertTrue(f.can_load_more('playlist', self.PL))                      # a full first page: likely more
        f.load_more('playlist', self.PL).join(10)
        self.assertEqual(calls[0], (self.PL, feeds.PAGE + 1, feeds.PAGE))
        self.assertEqual(len(f.playlist_videos(self.PL)), 100)
        short = 'PL' + 'r' * 32
        f.playlists[short] = {'title': 'Short', 'at': time.time(), 'videos': [vid(i) for i in range(7)]}
        self.assertFalse(f.can_load_more('playlist', short))                       # a short list has no next page

    def test_only_one_load_at_a_time_and_failures_are_survivable(self):
        import threading
        f, calls = self.make()
        self.seed_channel(f)
        gate = threading.Event()
        real = f.page_fetch

        def slow(pid, start, count):
            gate.wait(5)
            return real(pid, start, count)
        f.page_fetch = slow
        t = f.load_more('channel', self.UC)
        self.assertTrue(f.is_loading('channel', self.UC))
        self.assertIsNone(f.load_more('channel', self.UC))                         # a second press does nothing
        gate.set()
        t.join(10)
        self.assertFalse(f.is_loading('channel', self.UC))
        f.page_fetch = lambda *a: (_ for _ in ()).throw(RuntimeError('ERROR: [youtube:tab] x: Unable to download'))
        n = len(f.channel_videos(self.UC))
        f.load_more('channel', self.UC).join(10)
        self.assertEqual(len(f.channel_videos(self.UC)), n)                        # nothing lost
        self.assertTrue(f.can_load_more('channel', self.UC))                       # and it can be tried again
        self.assertFalse(f.is_loading('channel', self.UC))

    def test_later_pages_do_not_flood_the_merged_subscriptions_feed(self):
        f, calls = self.make()
        self.seed_channel(f)
        before = len(f.feed([(None, self.UC)], True, 500))
        f.load_more('channel', self.UC).join(10)
        self.assertEqual(len(f.feed([(None, self.UC)], True, 500)), before)        # still just the dated 15

    def test_the_load_more_row_in_the_list(self):
        f, calls = self.make()
        self.seed_channel(f, 3)
        asked = []
        a = type('A', (), {'store': store.Store(tempfile.mkdtemp()), 'feeds': f})()
        lst = ui.VideoList('C', lambda app: f.channel_videos(self.UC), more=lambda app: asked.append(1),
                           can_more=lambda app: f.can_load_more('channel', self.UC),
                           loading=lambda app: f.is_loading('channel', self.UC))
        rows = lst.items(a)
        self.assertEqual(len(rows), 4)
        self.assertEqual(lst.label(a, rows[-1]), 'Load more videos...')
        self.assertIsNone(lst.info(a, rows[-1]))                                   # no detail panel for it
        self.assertEqual(lst.right(a, rows[-1]), '')
        self.assertFalse(lst.extra(a, rows[-1], 'alt'))                            # Y does not "save" the row
        lst.activate(a, rows[-1])
        self.assertEqual(asked, [1])                                               # A on it asks for the next page
        f.loading.add(('channel', self.UC))
        self.assertTrue(lst.label(a, rows[-1]).startswith('Loading more'))   # the dots animate
        lst.activate(a, rows[-1])
        self.assertEqual(asked, [1])                                               # but not while one is in flight
        f.loading.clear()
        f.channels[self.UC]['more'] = False
        self.assertEqual(len(lst.items(a)), 3)                                     # no row once the end is reached
        self.assertEqual(len(ui.VideoList('x', lambda app: []).items(a)), 0)       # lists without paging are unchanged

    def test_dropped_live_entries_do_not_end_paging_and_the_cap_holds(self):
        f, calls = self.make()
        self.seed_channel(f)

        def hole(pid, start, count):                              # 5 of every 50 are live/unusable: dropped, but sent
            calls.append((pid, start, count))
            page = pipeline.Page(vid(i) for i in range(start - 1, start - 1 + count - 5))
            page.raw = count
            return 'T', page
        f.page_fetch = hole
        f.load_more('channel', self.UC).join(10)
        self.assertTrue(f.can_load_more('channel', self.UC))                       # 45 usable of 50 sent: not the end
        f.load_more('channel', self.UC).join(10)
        self.assertEqual(calls[1][1], 16 + 50)                                      # continues after what YouTube sent
        f.channels[self.UC]['videos'] = [vid(i) for i in range(feeds.MAX_LOADED)]
        self.assertFalse(f.can_load_more('channel', self.UC))                       # capped

    def test_playlist_window_command(self):
        rt = type('R', (), {'resolver': Path('yt-dlp'), 'qjs': Path('qjs'), 'ca': Path('ca'), 'ffdir': Path('.')})()
        seen = []
        saved = pipeline.capture
        pipeline.capture = lambda cmd, env, cancel, timeout=300, on_line=None, background=False: (seen.append([str(c) for c in cmd]), b'')[1]
        try:
            pipeline.playlist_entries(rt, 'PL' + 'q' * 32, limit=50, start=51)
        finally:
            pipeline.capture = saved
        cmd = seen[0]
        self.assertEqual(cmd[cmd.index('--playlist-start') + 1], '51')
        self.assertEqual(cmd[cmd.index('--playlist-end') + 1], '100')


class AudioDropout(unittest.TestCase):
    """A long video's audio connection can end quietly part-way: reopen it there instead of playing silence."""
    VB, AB = 16, 192000 // 30            # tiny "frames" are enough to exercise the pairing

    def setUp(self):
        self.real_select = pipeline.select.select
        if sys.platform == 'win32':          # select() on pipes is Linux-only: poll the pipes instead
            pipeline.select.select = self.win_select
            if not hasattr(os, 'killpg'):
                os.killpg = lambda pid, sig: os.kill(pid, signal.SIGTERM)
                self.addCleanup(lambda: delattr(os, 'killpg'))

    @staticmethod
    def win_select(r, w, x, t=None):
        import ctypes, msvcrt
        end = time.time() + (t or 0)
        while True:
            ready = []
            for fd in r:
                avail = ctypes.c_ulong(0)
                ok = ctypes.windll.kernel32.PeekNamedPipe(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(avail), None)
                if not ok or avail.value:                       # data waiting, or the pipe is closed (a read then returns EOF)
                    ready.append(fd)
            if ready or time.time() >= end:
                return ready, [], []
            time.sleep(.005)

    def tearDown(self):
        pipeline.select.select = self.real_select

    @staticmethod
    def feeder(nbytes, byte, exit_code=0):
        return [sys.executable, '-c',
                'import sys;sys.stdout.buffer.write(bytes([%d])*%d);sys.stdout.flush();sys.exit(%d)' % (byte, nbytes, exit_code)]

    def run_all(self, audio_cmds, frames=40, duration=100):
        video = self.feeder(self.VB * frames, 7)
        it = iter(audio_cmds[1:])
        starts = []

        def again(at):
            starts.append(round(at, 2))
            return next(it)
        d = pipeline.Decoders({'video': video, 'audio': audio_cmds[0]}, self.VB, self.AB, again, 0.0, duration)
        saw = []
        try:
            while True:
                rec = d.next_record(lambda: None)
                if rec is None:
                    break
                saw.append(rec[self.VB:])
        finally:
            d.close()
        return saw, starts

    def test_audio_that_stops_early_is_reopened_where_it_stopped(self):
        half = self.AB * 10                                 # the first connection delivers 10 frames of sound, then ends
        saw, starts = self.run_all([self.feeder(half, 1), self.feeder(self.AB * 30, 2)])
        self.assertEqual(len(saw), 40)
        self.assertTrue(all(set(a) == {1} for a in saw[:10]))
        self.assertTrue(all(set(a) == {2} for a in saw[10:]))                 # sound continues: not silence
        self.assertEqual(len(starts), 1)
        self.assertAlmostEqual(starts[0], 10 * self.AB / 192000.0, places=2)   # reopened at the 10-frame mark

    def test_audio_that_ends_with_the_video_is_not_restarted(self):
        saw, starts = self.run_all([self.feeder(self.AB * 40, 1)], duration=40 * self.AB / 192000.0)
        self.assertEqual(starts, [])
        self.assertEqual(len(saw), 40)

    def test_a_failing_audio_stream_still_reports_its_error(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_all([self.feeder(0, 0, 1)])
        self.assertIn('audio decoder failed', str(cm.exception))


class BackgroundHold(unittest.TestCase):
    """While a video is starting or playing, nothing in the background may compete for the ARM."""

    def test_background_work_waits_until_the_video_is_over(self):
        import threading
        f = feeds.Feeds(tempfile.mkdtemp())
        f.log = lambda m: None
        started = []
        f.playlist_fetch = lambda pid: (started.append(pid), ('T', [vid(1)]))[1]
        f.hold.set()                                              # a video is starting
        t = f.refresh_lists([(None, 'PL' + 'a' * 32)], [])
        time.sleep(1.2)
        self.assertEqual(started, [])                             # nothing began
        f.hold.clear()                                            # playback over
        t.join(10)
        self.assertEqual(len(started), 1)                         # and now it runs

    def test_playing_holds_and_always_releases(self):
        a = type('A', (), {})()
        a.feeds = feeds.Feeds(tempfile.mkdtemp())
        seen = []

        def inner(v, start):
            seen.append(a.feeds.hold.is_set())
            raise RuntimeError('boom')
        a._play = inner
        with self.assertRaises(RuntimeError):
            __import__('ytcrt.app', fromlist=['App']).App.play(a, {'id': 'x'}, 0)
        self.assertEqual(seen, [True])
        self.assertFalse(a.feeds.hold.is_set())                   # released even when playback blows up


class ResolveCache(unittest.TestCase):
    def test_a_recent_resolve_is_reused_and_forgotten_on_demand(self):
        d = Path(tempfile.mkdtemp())
        rt = type('R', (), {'resolve_cache': d, 'ca': Path('ca'), 'resolver': Path('y'), 'qjs': Path('q'), 'ffdir': Path('.')})()
        raw = (Path(__file__).parent / 'fixtures' / 'landscape.json').read_bytes()      # the two JSON lines yt-dlp prints
        calls = []
        saved = pipeline.capture
        pipeline.capture = lambda cmd, env, cancel, timeout=300, on_line=None, background=False: (calls.append(1), raw)[1]
        try:
            vid = 'abcdefghijk'
            pipeline.resolve(rt, profiles.PROFILE, vid, lambda: None, lambda m: None)
            pipeline.resolve(rt, profiles.PROFILE, vid, lambda: None, lambda m: None)
            self.assertEqual(len(calls), 1)                      # the second play did not ask YouTube again
            pipeline.forget(rt, vid)                             # URLs refused: next time is a fresh resolve
            pipeline.resolve(rt, profiles.PROFILE, vid, lambda: None, lambda m: None)
            self.assertEqual(len(calls), 2)
            os.utime(str(d / (vid + '.v2.out')), (time.time() - 4 * 3600,) * 2)    # too old
            pipeline.resolve(rt, profiles.PROFILE, vid, lambda: None, lambda m: None)
            self.assertEqual(len(calls), 3)
        finally:
            pipeline.capture = saved


class FourByThree(unittest.TestCase):
    def test_360p_means_360_lines_for_4_by_3_video_too(self):
        info = {'id': 'x', 'title': 't', 'duration': 60}
        def f(i, w, h, tbr):
            return {'format_id': i, 'protocol': 'https', 'vcodec': 'avc1.4d401e', 'acodec': 'none', 'width': w, 'height': h,
                    'fps': 30, 'tbr': tbr, 'url': 'https://example.invalid/v?itag=' + i}
        audio = {'format_id': '140', 'protocol': 'https', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'tbr': 130,
                 'url': 'https://example.invalid/a?itag=140'}
        fmts = [f('133', 320, 240, 150), f('134', 480, 360, 378), f('135', 640, 480, 683), audio]
        for q, want in (('240p', '133'), ('360p', '134'), ('480p', '135')):
            v = pipeline.choose_streams(info, fmts, profiles.PROFILE, q)[0]
            self.assertIn('itag=' + want, v['url'], q)


class AudioLanguage(unittest.TestCase):
    def test_the_original_language_is_chosen_not_an_auto_dub(self):
        info = {'id': 'x', 'title': 't', 'duration': 60}
        video = {'format_id': '134', 'protocol': 'https', 'vcodec': 'avc1.4d401e', 'acodec': 'none', 'width': 480, 'height': 360,
                 'fps': 30, 'tbr': 378, 'url': 'https://example.invalid/v?itag=134'}

        def audio(fid, pref):
            return {'format_id': fid, 'protocol': 'https', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'ext': 'm4a', 'tbr': 130,
                    'language_preference': pref, 'url': 'https://example.invalid/a?itag=' + fid}
        fmts = [video, audio('140-0', -1), audio('140-1', -1), audio('140-7', 10)]      # German first in the list, English is the original
        a = pipeline.choose_streams(info, fmts, profiles.PROFILE, '360p')[1]
        self.assertIn('itag=140-7', a['url'])
        a = pipeline.choose_streams(info, [video, dict(audio('140', None))], profiles.PROFILE, '360p')[1]   # no language data: unchanged
        self.assertIn('itag=140', a['url'])


nl = chr(10)


class RemoveEntries(unittest.TestCase):
    def make(self, text):
        d = tempfile.mkdtemp()
        st = store.Store(d)
        Path(st.p_subs).write_text(text, encoding='utf-8')
        return st

    def test_remove_a_typo_and_keep_everything_else(self):
        st = self.make(nl.join(['# my list', '@GameSAC', 'Jawed | @jawed', 'https://youtu.be/jNQXAC9IVRw', '  # note', '']))
        self.assertEqual(st.remove_entry('channel', ['@GameSAC']), 1)
        self.assertEqual(Path(st.p_subs).read_text(), nl.join(['# my list', 'Jawed | @jawed', 'https://youtu.be/jNQXAC9IVRw', '  # note', '']))
        self.assertEqual(st.remove_entry('channel', ['@GameSAC']), 0)             # already gone
        self.assertEqual(st.remove_entry('video', ['jNQXAC9IVRw']), 1)            # a video, by id
        self.assertEqual(st.remove_entry('channel', ['@jawed']), 1)               # a named line
        self.assertEqual(Path(st.p_subs).read_text(), nl.join(['# my list', '  # note', '']))

    def test_gui_removes_a_channel_listed_by_handle_even_after_it_resolved(self):
        a, st = ListsInTheGui.make_app(self)
        st.add_entry('channel', '@GameSAC')
        a.feeds.handles['@GameSAC'] = 'UC' + 'g' * 22                              # the handle was resolved to an id
        a.confirm_remove_channel('UC' + 'g' * 22, 'GameSAC')
        self.assertIsInstance(a.screen, ui.Choice)
        a.screen.handle(a, 'ok')                                                  # "Remove"
        self.assertEqual(st.subscriptions(), [])


if __name__ == '__main__':
    unittest.main()
