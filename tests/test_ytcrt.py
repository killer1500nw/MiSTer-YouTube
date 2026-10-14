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
        self.assertEqual(rows, ['quality', 'crop', 'resume', 'autoplay', 'shorts', 'thumbnails', 'decode_speed', 'bench', 'diag',
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
        self.assertEqual(profiles.speed_for(854 * 480, 'Fastest', 'Fast'), 'Fastest')    # a manual Fastest is never overridden by the Speed test
        self.assertEqual(profiles.speed_for(854 * 480, 'Fast', 'Fastest'), 'Fastest')    # (the learned value still applies to Fast/Normal)


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
        ref = {'width': 640, 'height': 360, 'tbr': cal['tbr'], 'fps': 30}
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
        v24 = self.pick([stream(426, 240, 126, 24), stream(640, 360, 360, 24)])
        v30 = self.pick([stream(426, 240, 126, 30), stream(640, 360, 360, 30)])
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
        self.t0 = time.monotonic()

        def fake_open(rt, prof, media, start_s, speed, cancel, crop='Off', target=None):
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

    def test_a_clip_that_starves_on_fast_at_480p_gets_fastest_and_is_remembered(self):
        st = self.make(quality='480p', cost=0.3)
        st.settings['decode_speed'] = 'Normal'
        seen = []

        def loop(v, media, prof, level, pos, can_step, refresh):
            seen.append((media.tier, level))
            return ('downgrade', 5000) if len(seen) == 1 else ('stop', 6000)
        self.app._play_loop = loop
        self.app.play({'id': 'v' * 11, 'title': 't'}, 0)
        self.assertEqual(seen[0][1], 'Fast')
        self.assertEqual(seen[1], (seen[0][0], 'Fastest'))               # same picture, stronger setting
        self.assertEqual(st.settings['fastest_ids'], ['v' * 11])
        self.app.play({'id': 'v' * 11, 'title': 't'}, 0)                 # next time it starts on Fastest
        self.assertEqual(seen[2][1], 'Fastest')

    def test_a_starve_while_ffmpeg_is_not_busy_is_ignored_once(self):
        from ytcrt import cpu as cpumod
        st = self.make(quality='480p', cost=0.3)
        st.settings['decode_speed'] = 'Normal'
        seen = []
        real = cpumod.CpuWatch

        class Idle(real):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.values = {'video ffmpeg': 40.0}          # the decoder is nowhere near a full core

            def sample(self):
                return ''
        cpumod.CpuWatch = Idle
        quick_before = self.appmod.CPU_QUICK_SECONDS
        self.appmod.CPU_QUICK_SECONDS = 0.1
        self.addCleanup(setattr, self.appmod, 'CPU_QUICK_SECONDS', quick_before)
        self.addCleanup(setattr, cpumod, 'CpuWatch', real)
        orig = self.app._play_loop
        self.app._play_loop = lambda v, media, prof, speed, *a: (seen.append(speed), orig(v, media, prof, speed, *a))[1]
        lines = []
        real_log = self.app.log
        self.app.log = lambda m, *a: (lines.append(m % a if a else m), real_log(m, *a))[1]
        self.run_play([FakePipe(rate=4), FakePipe()], lambda: len(self.opened) >= 2)
        self.assertEqual(seen[0], 'Fast')
        self.assertTrue(any('ignoring it once' in l for l in lines))           # the first starve was put down to a hiccup
        self.assertEqual(seen[1], 'Fastest')                                    # it happened again: now the stronger setting is tried

    def test_a_video_that_needed_a_lighter_stream_starts_on_it_next_time(self):
        st = self.make(cost=0.7)
        self.run_play([FakePipe(rate=4), FakePipe()], lambda: len(self.opened) >= 2)
        self.assertEqual([t for t, _ in self.opened], ['360p', '240p'])
        self.assertIn('v' * 11, st.settings['cap_ids'])
        self.opened.clear()
        self.run_play([FakePipe()], lambda: len(self.opened) >= 1)
        self.assertEqual(self.opened[0][0], '240p')                     # straight to the stream that worked: no second interruption

    def test_crop_defaults_to_fill_and_a_videos_own_choice_is_remembered(self):
        import threading
        st = self.make(quality='480p', cost=0.3)
        crops = []
        orig = pipeline.open_pipeline

        def fake_open(rt, prof, media, start_s, speed, cancel, crop='Off', target=None):
            crops.append(crop)
            return FakePipe()
        pipeline.open_pipeline = fake_open
        self.addCleanup(setattr, pipeline, 'open_pipeline', orig)
        self.assertEqual(st.settings['crop'], 'Fill')
        st.settings['crops'] = {'a' * 11: '14:9'}
        for vid in ('a' * 11, 'b' * 11):
            threading.Thread(target=lambda: (time.sleep(1.0), self.inp.push('back')), daemon=True).start()
            self.app.play({'id': vid, 'title': 't'}, 0)
        self.assertEqual(crops[0], '14:9')                              # this video was set to 14:9 before
        self.assertEqual(crops[-1], 'Fill')                             # any other video: the default

    def test_the_next_video_of_a_list_plays_when_one_finishes(self):
        self.make()
        played = []

        def fake_play(v, start_ms):
            played.append(v['id'])
            self.app.finished = len(played) < 3                         # the third video is stopped by the user
        self.app._play = fake_play
        vids = [{'id': c * 11, 'title': c} for c in 'abcd']
        self.app.play(vids[0], 0, lambda: vids)
        self.assertEqual(played, ['a' * 11, 'b' * 11, 'c' * 11])        # a and b ended, c was stopped: no d
        played.clear()
        self.app.store.settings['autoplay'] = 'Off'
        self.app.play(vids[0], 0, lambda: vids)
        self.assertEqual(played, ['a' * 11])

    def test_the_old_default_crop_becomes_fill_once(self):
        import json
        self.make()                                                     # (the class's tearDown expects it)
        for old, new in (('Off', 'Fill'), ('14:9', '14:9')):
            d = tempfile.mkdtemp()
            Path(d, 'settings.json').write_text(json.dumps({'crop': old}))
            self.assertEqual(store.Store(d).settings['crop'], new)
        d = tempfile.mkdtemp()
        st = store.Store(d)
        st.set('crop', 'Off')                                           # chosen on purpose afterwards: kept
        self.assertEqual(store.Store(d).settings['crop'], 'Off')

    def test_a_fast_arm_gets_360p_and_a_slow_one_steps_down_and_learns(self):
        st = self.make(cost=0.7)                         # calibration says 360p@452k needs ~62% of the ARM
        self.run_play([FakePipe(rate=4), FakePipe()], lambda: len(self.opened) >= 2)
        self.assertEqual([t for t, _ in self.opened], ['360p', '240p'])   # it really starved, so it stepped down
        self.assertEqual(len(self.resolved), 1)          # the catalogue was reused: no second yt-dlp run
        self.assertGreater(st.settings['cost_mult'], 1.0)                 # and the estimate was corrected upwards

    def test_an_empty_queue_while_audio_is_being_reopened_is_not_starvation(self):
        class Reopening(FakePipe):
            @property
            def restarts(self):                          # the audio stream is reopened over and over: the picture waits for it
                return int(time.monotonic() * 20)
        st = self.make(cost=0.7)
        self.run_play([Reopening(rate=4)], lambda: self.app.link.sent > 5 and time.monotonic() > self.t0 + 3)
        self.assertEqual([t for t, _ in self.opened], ['360p'])           # no step down
        self.assertEqual(st.settings['cost_mult'], 1.0)                   # and nothing learned from it

    def test_repeated_short_dropouts_step_down_even_when_the_total_time_is_small(self):
        st = self.make(cost=0.7)
        # the queue is empty for 0.1 s out of every 0.3 s: short dropouts, each a gap in the audio
        flaky = type('Flaky', (core_link.FakeLink,), {'queued': property(lambda s: 0 if (time.monotonic() * 1000) % 300 < 100 else 3)})
        self.app.link.__class__ = flaky
        saved = (self.appmod.DRY_LIMIT, self.appmod.STARVE_LIMIT)
        self.appmod.DRY_LIMIT, self.appmod.STARVE_LIMIT = 2, 50.0       # only the dropout count can trigger it
        try:
            self.run_play([FakePipe(), FakePipe()], lambda: len(self.opened) >= 2)
        finally:
            self.appmod.DRY_LIMIT, self.appmod.STARVE_LIMIT = saved
        self.assertEqual([t for t, _ in self.opened], ['360p', '240p'])

    def test_quality_can_be_changed_from_the_more_panel_while_playing(self):
        st = self.make(cost=0.3)
        saved = self.appmod.QUALITY_SETTLE
        self.appmod.QUALITY_SETTLE = 0.2

        def drive():
            time.sleep(1.0)
            for act in ('menu', 'down', 'right'):                     # X, down to Quality, right: Auto -> 240p
                self.inp.push(act)
                time.sleep(0.1)
        import threading
        threading.Thread(target=drive, daemon=True).start()
        try:
            self.run_play([FakePipe(), FakePipe()], lambda: len(self.opened) >= 2)
        finally:
            self.appmod.QUALITY_SETTLE = saved
        self.assertNotEqual(self.opened[0][0], '240p')                 # Auto chose something bigger first
        self.assertEqual(self.opened[1][0], '240p')                    # same video, new stream
        self.assertEqual(len(self.resolved), 1)                        # from the catalogue: no second yt-dlp run
        self.assertEqual(st.settings['quality'], 'Auto')               # for this video only: the setting is unchanged

    def test_clean_playback_trusts_the_estimate_a_little_more(self):
        st = self.make(cost=0.78)                        # 360p@452k predicted ~0.69: allowed, and close to the limit
        self.appmod.SMOOTH_AFTER = 0.5
        self.run_play([FakePipe()], lambda: st.settings['cost_mult'] < 1.0)
        self.assertEqual(self.opened[0][0], '360p')
        self.assertLess(st.settings['cost_mult'], 1.0)

    def test_the_default_calibration_allows_a_moderate_360p_stream(self):
        self.make(cost=profiles.CAL_DEFAULT['cost'])
        self.run_play([FakePipe()], lambda: self.app.link.sent > 20)
        self.assertEqual(self.opened[0][0], '360p')

    def test_a_slow_measured_arm_sends_a_busy_stream_to_240p(self):
        self.make(cost=1.2)                              # what a Speed test on a much slower ARM would say
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


    def test_a_decoder_crash_reconnects_at_the_same_place(self):
        self.make(quality='240p')
        crash = 'video decoder failed (-11): '
        self.run_play([FakePipe(failures=[crash]), FakePipe()], lambda: self.app.link.sent > 20)
        self.assertEqual(len(self.opened), 2)            # opened, crashed, reopened: playback went on
        self.assertEqual(len(self.resolved), 1)          # without asking yt-dlp again

    def test_repeated_decoder_crashes_give_up_after_the_nudges(self):
        self.make(quality='240p')
        always = FakePipe(failures=['video decoder failed (-11): '] * 40)
        self.run_play([always], lambda: type(self.app.screen).__name__ == 'TextView')
        self.assertEqual(len(self.opened), 5)            # the first open + 4 tries at nearby positions, then a clear error screen
        self.assertEqual(type(self.app.screen).__name__, 'TextView')
        self.assertIn('Try a lower quality', ' '.join(self.app.screen.lines))

    def test_a_crash_at_one_position_is_tried_again_a_little_later(self):
        self.make(quality='240p')
        crash = 'audio decoder failed (-11): '
        self.run_play([FakePipe(failures=[crash] * 5), FakePipe(failures=[crash] * 5), FakePipe()], lambda: self.app.link.sent > 15)
        self.assertEqual(len(self.opened), 3)
        self.assertEqual(self.opened[0][1], 0)           # the first try, at the resume position
        self.assertEqual(self.opened[1][1], 1)           # then 1.2 s later
        self.assertEqual(self.opened[2][1], 3)           # then 3 s later, where it works

    def test_auto_steps_down_when_every_try_crashes(self):
        st = self.make(cost=0.3)                          # Auto picks something big first
        crash = 'audio decoder failed (-11): '
        self.run_play([FakePipe(failures=[crash] * 5)] * 0 + [FakePipe(failures=[crash] * 5) for _ in range(5)] + [FakePipe()],
                      lambda: len(self.opened) >= 6)
        self.assertEqual(len(self.opened), 6)
        self.assertEqual(self.opened[0][0], self.opened[4][0])
        self.assertNotEqual(self.opened[4][0], self.opened[5][0])        # a lighter stream after the nudges all failed
        self.assertEqual(st.settings['cost_mult'], 1.0)                  # and nothing learned about the ARM's speed from a crash

    def test_a_starting_screen_shows_until_the_first_picture(self):
        self.make(quality='240p')
        seen = []
        orig = self.app.loading_frame

        def spy():
            seen.append(type(self.app.screen).__name__)
            orig()
        self.app.loading_frame = spy
        self.run_play([FakePipe(rate=5)], lambda: self.app.link.sent > 8)
        self.assertIn('Message', seen)                                   # the spinner screen was drawn while waiting
        self.assertNotEqual(type(self.app.screen).__name__, 'Message')   # and it is gone again


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
        self.assertEqual(self.f.status[self.uc], 'yt-dlp: Yt-dlp broke')          # what really failed last, not the RSS error
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
    def run_test(self, resolver, bench=None):
        import threading
        from ytcrt import app as appmod
        tmp = tempfile.mkdtemp()
        st = store.Store(tmp)
        rt = type('Rt', (), {'ffmpeg': Path(tmp) / 'nope', 'resolver': Path(tmp) / 'nope', 'missing': lambda self: []})()
        a = appmod.App(core_link.FakeLink(), st, offline_feeds(tmp + '/cache'), rt,
                       inputs.Input(evdev=False, tty=False), lambda m: None)
        a.link.measure_fast_copy = lambda n=12: {'memmove': (1.2, 2.0, True), 'slice': (3.0, 4.0, True)}   # a MiSTer's answer
        uc = 'UC' + 'e' * 22
        a.feeds.channels[uc] = {'name': 'C', 'at': time.time(), 'ok': True, 'videos': [
            {'id': 'live' + 'a' * 7, 'title': 'LIVE', 'channel': 'C', 'published': 3, 'short': False},
            {'id': 'live' + 'b' * 7, 'title': 'LIVE2', 'channel': 'C', 'published': 2, 'short': False}]}
        Path(st.p_subs).write_text(uc + '\n')
        saved = (pipeline.resolve, pipeline.benchmark, pipeline.pipe_benchmark)
        pipeline.pipe_benchmark = lambda *a, **k: 1.2
        info, fm = fixture('landscape')
        tried = []

        def fake_resolve(rt, prof, vid, cancel, log, note=None, quality='Auto', cal=None, mult=1.0):
            tried.append(vid)
            return resolver(vid, info, fm, prof, quality)
        pipeline.resolve = fake_resolve
        pipeline.benchmark = bench or (lambda rt, prof, media, speed, seconds=10, cancel=None, decode_only=False, target=None: (
            1.2 if decode_only else {'Normal': 0.6, 'Fast': 0.7, 'Fastest': 0.9}[speed]))
        done = []
        threading.Thread(target=lambda: (a.run_benchmark(), done.append(1)), daemon=True).start()
        end = time.time() + 15
        try:
            while not done and time.time() < end:
                a.pump(0.01)
        finally:
            pipeline.resolve, pipeline.benchmark, pipeline.pipe_benchmark = saved
        return a, st, tried

    def test_livestreams_are_skipped_and_a_good_clip_is_used(self):
        def resolver(vid, info, fm, prof, quality):
            if vid.startswith('live'):
                raise ValueError(pipeline.LIVE_MESSAGE)
            return pipeline.media_from(info, fm, prof, quality)
        a, st, tried = self.run_test(resolver)
        self.assertIn('M7kB0lis3xg', tried)                            # fell through to the known good clip
        self.assertEqual(len([t for t in tried if t.startswith('live')]), 2)
        self.assertEqual(a.screen.title, 'Speed test')                  # success, not "Speed test failed"
        self.assertIn('Bulk copy: 1.2 ms (memmove)', a.screen.lines)    # the copy measurements are shown
        self.assertEqual(st.settings['speed_big'], 'Fast')                # Auto is sized for Fast: Fastest (no deblocking) is never relied on
        self.assertEqual(st.settings['cal']['cost'], round(1 / 0.7, 4))
        self.assertEqual(st.settings['speed_clip'], 'EafWURqb2vc')       # the clip used (the fixture's id): the next test reuses it
        self.assertFalse(a.feeds.hold.is_set())                          # and background work is released again

    def test_a_refused_stream_is_retried_with_fresh_details(self):
        calls = []

        def bench(rt, prof, media, speed, seconds=10, cancel=None, decode_only=False, target=None):
            calls.append(speed)
            if len(calls) == 1:
                raise RuntimeError('ffmpeg failed: Server returned 403 Forbidden (access denied)')
            return 1.2 if decode_only else {'Normal': 0.6, 'Fast': 0.7, 'Fastest': 0.9}[speed]
        forgot = []
        saved = pipeline.forget
        pipeline.forget = lambda rt, vid: forgot.append(vid)
        try:
            a, st, tried = self.run_test(lambda vid, info, fm, prof, quality: pipeline.media_from(info, fm, prof, quality), bench)
        finally:
            pipeline.forget = saved
        self.assertEqual(a.screen.title, 'Speed test')                  # it recovered instead of failing
        self.assertEqual(forgot, ['EafWURqb2vc'])

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


class SearchTests(unittest.TestCase):
    def make_app(self):
        a, st = ListsInTheGui.make_app(self)
        a.rt.resolver, a.rt.ca = Path('yt-dlp'), Path('ca')
        return a, st

    def page(self, start, n, total=45):
        rows = pipeline.Page(vid(i, title='Result %d' % i) for i in range(start - 1, min(total, start - 1 + n)))
        rows.raw = len(rows)
        return rows

    def test_search_is_first_on_home_and_the_keyboard_types_spaces(self):
        self.assertEqual(ui.Home.ENTRIES[0], 'Search')
        a, st = self.make_app()
        a.open_home_entry('Search')
        s = a.screen
        for ch in 'power':
            s.handle(a, 'ch:' + ch)
        s.row, s.col = 4, 9                                          # the space key
        s.handle(a, 'ok')
        self.assertEqual(s.text, 'power ')
        a.open_home_entry('Add to Lists')                            # elsewhere the space key still types nothing
        t = a.screen
        t.row, t.col = 4, 9
        t.handle(a, 'ok')
        self.assertEqual(t.text, '')

    def test_results_list_and_load_more(self):
        a, st = self.make_app()
        calls = []

        def fake(rt, query, cancel=lambda: None, log=lambda m: None, limit=20, start=1):
            calls.append((query, start, limit))
            return '', self.page(start, limit)
        saved = pipeline.search_entries
        pipeline.search_entries = fake
        try:
            self.assertEqual(a._search_typed(a, 'x'), 'Type something to search for.')      # too short: stays on the screen
            a.open_home_entry('Search')
            a.screen.text = '  power   rangers '
            a.screen.handle(a, 'submit')
            self.assertEqual(calls[0], ('power rangers', 1, 20))                             # spaces tidied
            lst = a.screen
            self.assertIsInstance(lst, ui.VideoList)
            rows = lst.items(a)
            self.assertEqual(len(rows), 21)                                                  # 20 results + the "Load more" row
            self.assertEqual(lst.label(a, rows[-1]).startswith('Load more'), True)
            lst.activate(a, rows[-1])
            for _ in range(200):
                if not a.search['loading']:
                    break
                time.sleep(0.02)
            self.assertEqual(calls[1], ('power rangers', 21, 20))                            # continues after what was sent
            self.assertEqual(len(a.search['videos']), 40)
            a.search_more(a.search)
            for _ in range(200):
                if not a.search['loading']:
                    break
                time.sleep(0.02)
            self.assertEqual(len(a.search['videos']), 45)                                    # the last, short page
            self.assertFalse(a.search['more'])
        finally:
            pipeline.search_entries = saved

    def test_a_failed_search_says_why(self):
        a, st = self.make_app()

        def boom(*args, **kw):
            raise RuntimeError('YouTube resolver failed (1): no network')
        saved = pipeline.search_entries
        pipeline.search_entries = boom
        try:
            a._search_typed(a, 'anything')
            self.assertIsInstance(a.screen, ui.TextView)
            self.assertEqual(a.screen.title, 'Search failed')
        finally:
            pipeline.search_entries = saved

    def test_search_command_is_safe_and_windowed(self):
        rt = type('R', (), {'resolver': Path('yt-dlp'), 'ca': Path('ca')})()
        seen = []
        saved = pipeline.capture
        pipeline.capture = lambda cmd, env, cancel, timeout=300, on_line=None, background=False: (seen.append([str(c) for c in cmd]), b'')[1]
        try:
            pipeline.search_entries(rt, '--exec rm -rf /\n evil', start=21, limit=20)
        finally:
            pipeline.capture = saved
        cmd = seen[0]
        self.assertEqual(cmd[cmd.index('--playlist-start') + 1], '21')
        self.assertEqual(cmd[cmd.index('--playlist-end') + 1], '40')
        self.assertEqual(cmd[-2], '--')                                                       # the query can never be an option
        self.assertTrue(cmd[-1].startswith('ytsearch40:--exec'))
        self.assertNotIn('\n', cmd[-1])


class NodeRuntime(unittest.TestCase):
    def tree(self):
        d = Path(tempfile.mkdtemp())
        res = d / 'resolver-2026.08.19-qjs-0.17.0'
        (res / 'bundle').mkdir(parents=True)
        (res / 'qjs').write_bytes(b'q')
        return d

    def test_runtime_finds_node_and_the_command_prefers_it_with_quickjs_as_fallback(self):
        d = self.tree()
        rt = pipeline.Runtime(str(d), str(d))
        self.assertIsNone(rt.node)
        cmd = [str(c) for c in pipeline.resolve_command(rt, profiles.PROFILE, 'https://www.youtube.com/watch?v=x')]
        self.assertEqual([c for c in cmd if c.startswith(('node:', 'quickjs:'))], ['quickjs:' + str(rt.qjs)])
        (d / 'node-test').mkdir()
        (d / 'node-test' / 'node').write_bytes(b'n')                    # the copy Test_Node_JS.sh leaves
        (d / 'node-22.23.3-armhf').mkdir()
        (d / 'node-22.23.3-armhf' / 'node').write_bytes(b'n')           # the installed one wins
        rt = pipeline.Runtime(str(d), str(d))
        self.assertEqual(rt.node, d / 'node-22.23.3-armhf' / 'node')
        cmd = [str(c) for c in pipeline.resolve_command(rt, profiles.PROFILE, 'https://www.youtube.com/watch?v=x')]
        self.assertEqual([c for c in cmd if c.startswith(('node:', 'quickjs:'))],
                         ['node:' + str(rt.node), 'quickjs:' + str(rt.qjs)])
        self.assertEqual(rt.missing().count(str(rt.qjs)), 0)             # node is optional: never "missing"

    def test_setup_installs_only_a_verified_runnable_node(self):
        import hashlib, io, tarfile
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'YouTubeCRT'))
        import ytcrt_setup as su
        root, dl = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as t:
            data = b'#!fake'
            info = tarfile.TarInfo('node-v22.23.3-linux-armv7l/bin/node')
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        blob = raw.getvalue()
        saved = (su.fetch, su.NODE_SHA256, su.node_version)
        su.fetch = lambda url, dest=None, label='': Path(dest).write_bytes(blob)
        try:
            su.NODE_SHA256 = hashlib.sha256(b'something else').hexdigest()            # wrong checksum: refuse
            with self.assertRaises(RuntimeError):
                su.setup_node(root, dl)
            self.assertFalse(list(root.glob('node-*-armhf')))
            su.NODE_SHA256 = hashlib.sha256(blob).hexdigest()
            su.node_version = lambda path: ''                                         # unpacked but does not run: refuse
            with self.assertRaises(RuntimeError):
                su.setup_node(root, dl)
            self.assertFalse(list(root.glob('node-*-armhf')))
            su.node_version = lambda path: su.NODE_VER
            su.setup_node(root, dl)
            self.assertTrue((root / 'node-22.23.3-armhf' / 'node').is_file())
            su.setup_node(root, dl)                                                   # a second run changes nothing
        finally:
            su.fetch, su.NODE_SHA256, su.node_version = saved


class RssBreaker(unittest.TestCase):
    def test_a_dead_rss_endpoint_is_given_up_on_after_two_channels(self):
        import urllib.error
        f = feeds.Feeds(tempfile.mkdtemp())
        f.log = lambda m: None
        feeds.RSS_BACKOFF_SAVED = feeds.RSS_BACKOFF
        feeds.RSS_BACKOFF = 0
        asked = []

        def dead(url, timeout=15, headers=None):
            asked.append(url)
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        f._get = dead
        f.fallback = lambda cid: [vid(1, channel='C')]
        try:
            ids = ['UC' + c * 22 for c in 'abcd']
            for cid in ids[:2]:
                f._refresh_one(cid, None, True)
            tried = len(asked)
            self.assertEqual(tried, 2 * feeds.RSS_TRIES)                  # the first two channels tried every attempt
            self.assertGreater(f.rss_down_until, time.time())            # then RSS was declared down
            for cid in ids[2:]:
                f._refresh_one(cid, None, True)
            self.assertEqual(len(asked), tried)                           # the rest went straight to yt-dlp
            self.assertTrue(all(f.channels[c]['videos'] for c in ids))    # and every channel still got its videos
            f.rss_down_until = 0                                          # RSS comes back
            f._get = lambda url, timeout=15, headers=None: (
                b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
                b'<title>T</title></feed>')
            f._refresh_one('UC' + 'e' * 22, None, True)
            self.assertEqual(f.rss_failed, 0)
        finally:
            feeds.RSS_BACKOFF = feeds.RSS_BACKOFF_SAVED


class DecodeOnlyCommand(unittest.TestCase):
    def test_decode_only_has_no_scaling_or_rgb_conversion(self):
        rt = type('R', (), {'ffmpeg': Path('ffmpeg'), 'ca': Path('ca')})()
        src = {'url': 'https://example.invalid/v', 'headers': {}}
        full = [str(x) for x in pipeline.decoder_command(rt, profiles.PROFILE, src, 'video', to_null=True)]
        bare = [str(x) for x in pipeline.decoder_command(rt, profiles.PROFILE, src, 'video', to_null=True, decode_only=True)]
        self.assertIn('rgb565le', full)
        self.assertNotIn('rgb565le', bare)
        self.assertEqual(bare[bare.index('-vf') + 1], 'null')
        self.assertEqual(bare[bare.index('-pix_fmt') + 1], 'yuv420p')


class BenchmarkSlope(unittest.TestCase):
    def test_start_up_time_is_cancelled_out(self):
        saved = pipeline._bench_wall
        try:
            # 3 s of start-up overhead plus 1/1.5 s of decoding per video second
            pipeline._bench_wall = lambda rt, prof, media, speed, seconds, cancel, decode_only, target=None, threads=None: 3.0 + seconds / 1.5
            self.assertAlmostEqual(pipeline.benchmark(None, None, None, 'Fast', 12), 1.5, places=2)   # the plain figure would be 12/11 = 1.09
            pipeline._bench_wall = lambda rt, prof, media, speed, seconds, cancel, decode_only, target=None, threads=None: 5.0     # no difference: falls back
            self.assertAlmostEqual(pipeline.benchmark(None, None, None, 'Fast', 12), 12 / 5.0, places=2)
        finally:
            pipeline._bench_wall = saved


class YuvFormat(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        import yuv_ref
        self.ref = yuv_ref
        from ytcrt import yuv
        self.yuv = yuv

    def test_header_round_trips_and_rejects_nonsense(self):
        yuv = self.yuv
        h = yuv.pack_header(640, 360, 640, 320, 0, 640, 60, 360, 65536, 65536, 0, 0, yuv.FLAG_601 | yuv.FLAG_DITHER)
        self.assertEqual(len(h), 64)
        p = yuv.parse_header(h)
        self.assertEqual((p['src_w'], p['src_h'], p['ystride'], p['cstride'], p['dy0'], p['dh'], p['flags']),
                         (640, 360, 640, 320, 60, 360, 5))
        with self.assertRaises(ValueError):
            yuv.pack_header(641, 360, 648, 328, 0, 640, 60, 360, 65536, 65536, 0, 0)       # odd width
        with self.assertRaises(ValueError):
            yuv.pack_header(640, 360, 636, 320, 0, 640, 60, 360, 65536, 65536, 0, 0)       # stride not a multiple of 8
        with self.assertRaises(ValueError):
            yuv.pack_header(640, 360, 640, 320, 0, 640, 200, 360, 65536, 65536, 0, 0)      # rectangle leaves the frame
        with self.assertRaises(ValueError):
            yuv.parse_header(bytes(64))

    def test_slots_and_the_four_writes_of_a_record(self):
        yuv = self.yuv
        self.assertEqual(yuv.slot_offset(0), 0x1000)
        self.assertEqual(yuv.slot_offset(9), 0x1000 + 0x80000)
        self.assertLessEqual(yuv.AUDIO_OFF + yuv.AUDIO_BYTES, yuv.SLOT_BYTES)
        self.assertLessEqual(yuv.Y_OFF + yuv.MAX_W * yuv.MAX_H, yuv.U_OFF)
        self.assertLessEqual(yuv.U_OFF + yuv.MAX_W * yuv.MAX_H // 4, yuv.V_OFF)
        self.assertLessEqual(yuv.V_OFF + yuv.MAX_W * yuv.MAX_H // 4, yuv.AUDIO_OFF)
        w = yuv.pieces(yuv.header_for(640, 360), bytes(640 * 360), bytes(320 * 180), bytes(320 * 180), bytes(6400), 3)
        self.assertEqual([o - yuv.slot_offset(3) for o, _ in w], [0, yuv.U_OFF, yuv.V_OFF, yuv.AUDIO_OFF])
        self.assertEqual(len(w[0][1]), yuv.Y_OFF + 640 * 360)

    def test_geometry_of_common_pictures(self):
        yuv = self.yuv
        g = yuv.layout(640, 360)                                           # 16:9 letterboxed in the 4:3 frame
        self.assertEqual((g['dx0'], g['dw'], g['dy0'], g['dh']), (0, 640, 60, 360))
        self.assertEqual((g['hstep'], g['vstep'], g['sx0'], g['sy0']), (65536, 65536, 0, 0))
        g = yuv.layout(480, 360)                                           # 4:3 fills the frame, magnified 1.333x
        self.assertEqual((g['dx0'], g['dw'], g['dy0'], g['dh']), (0, 640, 0, 480))
        self.assertAlmostEqual(g['hstep'] / 65536.0, 0.75, places=4)
        g = yuv.layout(640, 360, 'Fill')                                   # trims the sides to 4:3
        self.assertEqual((g['dw'], g['dh']), (640, 480))
        self.assertAlmostEqual(g['hstep'] / 65536.0, 0.75, places=4)
        self.assertAlmostEqual(g['sx0'] / 65536.0, 80 + 0.375 - 0.5, places=3)  # 80 source pixels off the left edge
        g = yuv.layout(640, 360, '14:9')
        self.assertEqual((g['dw'], g['dh']), (640, 410))
        g = yuv.layout(360, 640)                                           # a vertical short: pillarboxed
        self.assertEqual((g['dh'], g['dw']), (480, 270))
        self.assertIsNone(g['prescale'])

    def test_pictures_too_tall_for_the_line_step_are_prescaled(self):
        yuv = self.yuv
        self.assertIsNone(yuv.layout(848, 480, 'Off', True)['prescale'])   # 480 rows into 362 lines: 1.33 rows per frame line, fine
        self.assertIsNone(yuv.layout(848, 480, 'Off', False)['prescale'])  # and in 240p too: the core's eight row buffers cope with up to 2
        p = yuv.layout(400, 1000, 'Off', True)['prescale']                 # a very tall picture steps more than 2 rows per line
        self.assertIsNotNone(p)
        self.assertEqual(p[1], int(480 * 2.0) & ~1)                        # shrunk to twice the destination lines, no more
        self.assertEqual(p[0] % 16, 0)
        self.assertEqual(yuv.aligned_size(426, 240), (416, 240))
        self.assertEqual(yuv.aligned_size(854, 480), (848, 480))

    def test_model_identity_scale_is_just_the_colour_conversion(self):
        yuv, ref = self.yuv, self.ref
        w, h = 640, 480
        y, u, v = ref.planes_from_function(w, h, lambda i, j: (16 + (i * 219) // w, 128 + (j % 5), 128 - (j % 7)))
        hdr = yuv.parse_header(yuv.header_for(w, h))
        self.assertEqual((hdr['hstep'], hdr['vstep']), (65536, 65536))
        frame = ref.render_frame(y, u, v, hdr)
        for d, x in ((0, 0), (0, 639), (240, 320), (479, 100), (17, 5)):
            yy, uu, vv = y[d * w + x], u[(d // 2) * 320 + x // 2], v[(d // 2) * 320 + x // 2]
            # with a 1:1 mapping luma is exact; chroma is interpolated half a sample, so allow its small difference
            r, g, b = frame[d][x]
            er, eg, eb = ref.colour(yy, uu, vv, 0)
            self.assertLessEqual(max(abs(r - er), abs(g - eg), abs(b - eb)), 4)

    def test_model_black_bars_white_black_and_primaries(self):
        yuv, ref = self.yuv, self.ref
        w, h = 640, 360
        y, u, v = ref.planes_from_function(w, h, lambda i, j: (235, 128, 128) if i < 320 else (16, 128, 128))
        frame = ref.render_frame(y, u, v, yuv.parse_header(yuv.header_for(w, h)))
        self.assertEqual(frame[0][100], (0, 0, 0))                          # inside the top letterbox bar
        self.assertEqual(frame[479][100], (0, 0, 0))
        self.assertEqual(frame[240][10], (255, 255, 255))                   # limited-range white
        self.assertEqual(frame[240][630], (0, 0, 0))                        # limited-range black
        self.assertEqual(ref.colour(81, 90, 240, 0)[0] > 200, True)         # the red bar is red
        r, g, b = ref.colour(145, 54, 34, 0)
        self.assertGreater(g, r)                                           # the green bar is green
        self.assertEqual(ref.colour(255, 128, 128, yuv.FLAG_FULL_RANGE), (255, 255, 255))

    def test_model_upscaling_interpolates_and_stays_in_range(self):
        yuv, ref = self.yuv, self.ref
        w, h = 480, 360                                                      # 4:3 magnified 1.333x
        y, u, v = ref.planes_from_function(w, h, lambda i, j: (16 + (i * 219) // w, 128, 128))
        frame = ref.render_frame(y, u, v, yuv.parse_header(yuv.header_for(w, h)))
        row = [px[0] for px in frame[240]]
        self.assertEqual(row, sorted(row))                                   # a ramp stays monotonic
        self.assertGreater(len(set(row)), 200)                               # and uses many levels, with no banding

    def test_model_dither_gives_six_bit_values(self):
        yuv, ref = self.yuv, self.ref
        w, h = 480, 360
        y, u, v = ref.planes_from_function(w, h, lambda i, j: (16 + (i * 219) // w, 128, 128))
        hdr = yuv.parse_header(yuv.header_for(w, h, flags=yuv.FLAG_DITHER))
        frame = ref.render_frame(y, u, v, hdr)
        self.assertTrue(all(c % 4 == 0 for px in frame[240] for c in px))


class FakeYuvPipe:
    """Stands in for RecordPipeline on the v0.15 core: yields (YUV frame, PCM) pairs."""

    class Dec:
        @staticmethod
        def colour_flags(height):
            return 0

    def __init__(self, w=640, h=360, rate=500):
        self.w, self.h, self.rate, self.t0, self.n = w, h, rate, time.monotonic(), 0
        self.decoder = self.Dec()

    def poll(self):
        if self.n >= (time.monotonic() - self.t0) * self.rate:
            return 'wait', None
        self.n += 1
        return 'record', (bytearray(self.w * self.h * 3 // 2), bytes(6400))

    def close(self, wait=True):
        pass


class YuvPlayer(unittest.TestCase):
    """The player talking to a v0.15 core (FakeYuvLink): the GUI is an overlay, pictures are YUV, crop is the FPGA's."""

    def make(self):
        from ytcrt import app as appmod
        from ytcrt import yuv
        self.appmod, self.yuv = appmod, yuv
        tmp = tempfile.mkdtemp()
        self.st = store.Store(tmp)
        self.link = core_link.FakeYuvLink()
        rt = type('Rt', (), {'ffmpeg': Path(tmp) / 'nope', 'missing': lambda self: []})()
        self.inp = inputs.Input(evdev=False, tty=False)
        self.app = appmod.App(self.link, self.st, offline_feeds(tmp + '/cache'), rt, self.inp, lambda m: None)
        self.saved = (pipeline.resolve, pipeline.open_pipeline)
        info, fm = fixture('landscape')
        pipeline.resolve = lambda rt, prof, vid, cancel, log, note=None, quality='Auto', cal=None, mult=1.0: \
            pipeline.media_from(info, fm, prof, quality, cal, mult)

    def tearDown(self):
        pipeline.resolve, pipeline.open_pipeline = self.saved

    def test_the_menu_is_an_overlay_over_a_black_picture(self):
        self.make()
        self.assertTrue(self.app.yuv)
        for _ in range(8):
            self.app.pump(0.001)
        self.assertGreaterEqual(self.link.sent, 4)                       # the core starts after four records
        w, h, crop, flags, overlay = self.link.pictures[-1][2:]
        self.assertEqual((w, h, overlay is not None), (16, 16, True))     # a tiny black picture carrying the menu
        y, u, v = self.link.overlays[overlay]
        self.assertEqual(len(y), 320 * 240)
        self.assertNotIn(0, y)                                           # the menu is opaque (luma 0 would be transparent)
        self.assertEqual(self.link.pictures[-1][1], bytes(6400))          # silence

    def test_a_changed_screen_goes_to_another_overlay_buffer(self):
        self.make()
        for _ in range(8):
            self.app.pump(0.001)
        first = self.link.pictures[-1][6]
        self.app.open_home_entry('Settings')
        self.app.dirty = True
        for _ in range(4):
            self.app.pump(0.001)
        second = self.link.pictures[-1][6]
        self.assertNotEqual(first, second)                               # never overwritten while queued records still show it

    def play(self, pipe_factory, stop_when):
        import threading
        opened = []

        def fake_open(rt, prof, media, start_s, speed, cancel, crop='Off', target=None):
            opened.append((target, crop))
            return pipe_factory(target)
        pipeline.open_pipeline = fake_open

        def stopper():
            end = time.monotonic() + 20
            while time.monotonic() < end and not stop_when():
                time.sleep(0.05)
            time.sleep(0.2)
            self.inp.push('back')                                       # closes the More panel if it is open ...
            time.sleep(0.2)
            self.inp.push('back')                                       # ... and this one stops the video
        threading.Thread(target=stopper, daemon=True).start()
        self.app.play({'id': 'v' * 11, 'title': 'A test video'}, 0)
        return opened

    def test_playback_sends_pictures_at_their_own_size_and_crop_needs_no_restart(self):
        self.make()
        st = self.st
        st.settings['quality'] = '360p'
        steps = {'n': 0}

        def pipe(target):
            return FakeYuvPipe(target['w'], target['h'])

        def done():
            steps['n'] += 1
            if steps['n'] == 5:
                self.inp.push('menu')                                    # the More panel
            if steps['n'] == 8:
                self.inp.push('ok')                                      # Crop: Off -> 14:9
            return self.link.sent > 60 and self.link.pictures[-1][4] == '14:9'
        opened = self.play(pipe, done)
        self.assertEqual(len(opened), 1)                                 # no decoder restart for the crop
        self.assertEqual((opened[0][0]['w'], opened[0][0]['h']), (640, 360))
        pics = self.link.pictures
        self.assertEqual((pics[-1][2], pics[-1][3]), (640, 360))
        self.assertEqual(len(pics[-1][0]), 640 * 360 * 3 // 2)
        self.assertEqual(pics[-1][4], '14:9')                            # the FPGA does the crop: only the header changed
        self.assertIsNotNone(pics[-1][6])                                # and the More panel is an overlay, not burned in

    def test_the_playback_bar_is_an_overlay_that_goes_away(self):
        self.make()
        self.st.settings['quality'] = '360p'
        self.appmod.OSD_SECONDS = 0.3
        try:
            self.play(lambda t: FakeYuvPipe(t['w'], t['h'], rate=60), lambda: self.link.sent > 70)
        finally:
            self.appmod.OSD_SECONDS = 4.0
        flags = [p[6] is not None for p in self.link.pictures]
        self.assertIn(False, flags)                                      # the bar disappeared after its few seconds
        shown = [p[6] for p in self.link.pictures if p[6] is not None]
        if shown:
            y = self.link.overlays[shown[-1]][0]
            self.assertIn(0, y)                                          # mostly transparent: only the bar is drawn
            self.assertGreater(sum(1 for b in y if b), 100)

    def test_thumbnails_are_requested_as_yuv(self):
        self.make()
        self.assertEqual(self.app.feeds.thumb_format, 'yuv420p')


class MailboxCopy(unittest.TestCase):
    """The real v0.15 copy path (libc memmove) against ordinary memory standing in for /dev/mem."""

    def test_picture_and_overlay_land_in_memory(self):
        from ytcrt import transport, yuv
        mem = bytearray(yuv.MAP_BYTES)
        box = transport.Mailbox(mem)
        w, h = 64, 32
        ysize = w * h
        frame = bytearray(range(256)) * (ysize * 3 // 2 // 256)
        audio = bytes(range(200)) * 32
        hdr = bytes(range(64))
        box.picture(2, hdr, frame, ysize, audio)
        base = yuv.slot_offset(2)
        self.assertEqual(bytes(mem[base:base + 64]), hdr)
        self.assertEqual(bytes(mem[base + yuv.Y_OFF:base + yuv.Y_OFF + ysize]), bytes(frame[:ysize]))
        self.assertEqual(bytes(mem[base + yuv.AUDIO_OFF:base + yuv.AUDIO_OFF + yuv.AUDIO_BYTES]), audio)
        ov = yuv.overlay_offset(1)
        box.overlay(1, bytes(range(256)) * 300, bytes(80), bytes(80))
        self.assertEqual(bytes(mem[ov + yuv.OV_Y_OFF:ov + yuv.OV_Y_OFF + 256]), bytes(range(256)))
        box.close()


class CopyMethods(unittest.TestCase):
    """The cached copy: write through a cacheable mapping, flush, and check the result through the uncached one."""

    def make(self):
        import mmap
        from ytcrt import transport, yuv
        plain = mmap.mmap(-1, yuv.MAP_BYTES)                 # stands in for the O_SYNC mapping
        cached = mmap.mmap(-1, yuv.MAP_BYTES)                # and for the cacheable one (separate memory here, so the "flush" copies)
        return transport, yuv, plain, cached, transport.Mailbox(plain, None, cached)

    def test_cached_copy_lands_in_memory_after_the_flush(self):
        transport, yuv, plain, cached, box = self.make()
        saved = transport.cacheflush

        def fake_flush(start, end):                          # what the real flush does: the cached data reaches memory
            off = start - box.cached_base
            plain[off:off + (end - start)] = cached[off:off + (end - start)]
            return True
        transport.cacheflush = fake_flush
        try:
            box.use_method('cached')
            self.assertEqual(box.method, 'cached')
            data = bytes(range(256)) * 1000
            where = yuv.slot_offset(1) + yuv.Y_OFF
            box.copy_in(where, data)
            self.assertEqual(box.method, 'cached')           # it worked, so it stays in use
        finally:
            transport.cacheflush = saved
        self.assertEqual(bytes(plain[where:where + len(data)]), data)
        box.close()

    def test_a_flush_that_does_nothing_is_caught_and_the_plain_copy_takes_over(self):
        transport, yuv, plain, cached, box = self.make()
        saved = transport.cacheflush
        transport.cacheflush = lambda start, end: True       # claims success but the data never reaches memory
        try:
            box.use_method('cached')
            data = bytes(range(256)) * 400
            where = yuv.slot_offset(2) + yuv.Y_OFF
            box.copy_in(where, data)
        finally:
            transport.cacheflush = saved
        self.assertEqual(box.method, 'memmove')              # never used again
        self.assertEqual(bytes(plain[where:where + len(data)]), data)      # and this copy still arrived
        box.close()

    def test_a_missing_flush_call_falls_back_too(self):
        transport, yuv, plain, cached, box = self.make()
        saved = transport.cacheflush
        transport.cacheflush = lambda start, end: False
        try:
            box.use_method('cached')
            box.copy_in(yuv.slot_offset(0) + yuv.Y_OFF, bytes(1000))
        finally:
            transport.cacheflush = saved
        self.assertEqual(box.method, 'memmove')
        box.close()

    def test_without_a_cached_mapping_the_cached_method_is_refused(self):
        from ytcrt import transport, yuv
        box = transport.Mailbox(bytearray(yuv.MAP_BYTES))
        box.use_method('cached')
        self.assertEqual(box.method, 'memmove')
        box.close()

    def test_measure_copy_reports_only_the_methods_that_work(self):
        from ytcrt import core_link, transport, yuv
        import mmap
        link = core_link.Link.__new__(core_link.Link)
        link.kind, link.sent = 'yuv', 0
        link.copy_bytes, link.copy_report = 0, {}
        plain = mmap.mmap(-1, yuv.MAP_BYTES)
        cached = mmap.mmap(-1, yuv.MAP_BYTES)
        link.box = transport.Mailbox(plain, None, cached)
        saved = transport.cacheflush
        transport.cacheflush = lambda start, end: False       # the flush is not available: only memmove can be measured
        try:
            link.measure_copy(2)
        finally:
            transport.cacheflush = saved
        self.assertEqual(sorted(link.copy_report), ['memmove', 'parallel'])      # cached is left out: its flush is not available
        self.assertIn(link.box.method, ('memmove', 'parallel'))
        link.box.close()

    def test_parallel_copy_lands_every_piece_exactly(self):
        import mmap
        from ytcrt import transport, yuv
        plain = mmap.mmap(-1, yuv.MAP_BYTES)
        box = transport.Mailbox(plain)
        box.use_method('parallel')
        self.assertEqual(box.method, 'parallel')
        base = yuv.slot_offset(3)
        y = bytes((i * 7) & 255 for i in range(230400))
        u = bytes((i * 3) & 255 for i in range(57600))
        v = bytes((i * 5) & 255 for i in range(57600))
        for _ in range(3):                                    # repeated, so the helper thread is reused
            box.copy_pieces([(base + yuv.Y_OFF, y), (base + yuv.U_OFF, u), (base + yuv.V_OFF, v)])
        self.assertEqual(bytes(plain[base + yuv.Y_OFF:base + yuv.Y_OFF + len(y)]), y)
        self.assertEqual(bytes(plain[base + yuv.U_OFF:base + yuv.U_OFF + len(u)]), u)
        self.assertEqual(bytes(plain[base + yuv.V_OFF:base + yuv.V_OFF + len(v)]), v)
        box.close()

    def test_picture_uses_the_parallel_copy_and_still_writes_header_last(self):
        import mmap
        from ytcrt import transport, yuv
        plain = mmap.mmap(-1, yuv.MAP_BYTES)
        box = transport.Mailbox(plain)
        box.use_method('parallel')
        w, h = 128, 96
        frame = bytes(range(256)) * (w * h * 3 // 2 // 256)
        header = yuv.header_for(w, h, 'Off', True)
        box.picture(2, header, frame, w * h, bytes(6400))
        base = yuv.slot_offset(2)
        self.assertEqual(bytes(plain[base:base + 64]), header)
        self.assertEqual(bytes(plain[base + yuv.Y_OFF:base + yuv.Y_OFF + w * h]), frame[:w * h])
        box.close()

    def test_the_split_is_balanced(self):
        from ytcrt import transport
        box = transport.Mailbox(bytearray(4096))
        mine, theirs = box._split([(0, 0, 230400), (300000, 300000, 57600), (400000, 400000, 57600)])
        a, b = sum(p[2] for p in mine), sum(p[2] for p in theirs)
        self.assertEqual(a + b, 345600)
        self.assertLess(abs(a - b), 60000)
        box.close()


class CopyInTheCostModel(unittest.TestCase):
    CAL = {'px': 640 * 360, 'tbr': 307.0, 'fps': 30.0, 'cost': 0.34}

    def test_copy_time_adds_to_the_prediction(self):
        s = stream(640, 360, 307)
        plain = profiles.predicted_cost(s, self.CAL)
        with_copy = profiles.predicted_cost(s, dict(self.CAL, copy_ms=10.0, copy_bytes=345600))
        self.assertAlmostEqual(with_copy - plain, 0.30, places=2)             # 10 ms x 30 fps

    def test_bigger_pictures_copy_more_but_stop_at_the_core_limit(self):
        cal = dict(self.CAL, copy_ms=10.0, copy_bytes=345600)
        c360 = profiles.copy_cost(stream(640, 360, 300), cal, 30)
        c480 = profiles.copy_cost(stream(854, 480, 500), cal, 30)       # scaled down to 640x360 before it is copied
        c43 = profiles.copy_cost(stream(640, 480, 500), cal, 30)
        self.assertAlmostEqual(c480, c360, places=2)
        self.assertAlmostEqual(c43 / c360, 0.5 + 0.5 * 640 * 480 / (640 * 360.0), places=2)     # half of the copy time is a fixed cost
        c240 = profiles.copy_cost(stream(426, 240, 200), cal, 30)
        self.assertGreater(c240 / c360, 0.6)                                                   # a small picture is not nearly free

    def test_the_rgb_core_copies_a_fixed_record(self):
        cal = dict(self.CAL, copy_ms=7.0, copy_bytes=160000, copy_fixed=True)
        self.assertAlmostEqual(profiles.copy_cost(stream(426, 240, 100), cal, 30), profiles.copy_cost(stream(640, 360, 300), cal, 30))

    def test_the_limit_is_higher_only_when_the_copy_is_counted(self):
        self.assertEqual(profiles.limit_for(self.CAL), profiles.LIMIT)
        self.assertEqual(profiles.limit_for(dict(self.CAL, copy_ms=9.0, copy_bytes=1)), profiles.LIMIT_TOTAL)


class Av1Probe(unittest.TestCase):
    def test_codec_option_picks_av1_or_says_there_is_none(self):
        fm = [stream(640, 360, 300, fid='a'), stream(640, 360, 220, fid='b', vcodec='av01.0.05M.08')]
        for f in fm:
            f['url'] = 'https://x/' + f['format_id']
        info = {'id': 'x' * 11, 'title': 't', 'duration': 60}
        v, a, label, cost, why = pipeline.choose_streams(info, fm, profiles.PROFILE, '360p', codec='av01')
        self.assertEqual(v['format_id'] if isinstance(v, dict) and 'format_id' in v else v.get('format_id'), 'b')
        with self.assertRaises(ValueError):
            pipeline.choose_streams(info, fm[:1], profiles.PROFILE, '360p', codec='av01')


class GentlestSetting(unittest.TestCase):
    CAL = {'px': 640 * 360, 'tbr': 307.0, 'fps': 30.0, 'cost': 1 / 2.77, 'copy_ms': 14.0, 'copy_bytes': 345600}
    X = {'Normal': 2.48, 'Fast': 2.6, 'Fastest': 2.77}

    def test_a_light_stream_keeps_the_full_deblocking_filter(self):
        self.assertEqual(profiles.setting_for(stream(640, 360, 200), self.CAL, 1.0, self.X), 'Normal')

    def test_a_heavy_stream_gets_the_faster_settings(self):
        heavy = stream(854, 480, 900)
        self.assertEqual(profiles.setting_for(heavy, self.CAL, 1.0, self.X), 'Fast')        # never Fastest on its own

    def test_nothing_known_means_no_opinion(self):
        self.assertIsNone(profiles.setting_for(stream(640, 360, 300), self.CAL, 1.0, None))
        self.assertIsNone(profiles.setting_for(stream(640, 360, 300), None, 1.0, self.X))

    def test_a_pessimistic_multiplier_pushes_toward_faster_settings(self):
        s = stream(640, 360, 300)
        gentle = profiles.setting_for(s, self.CAL, 1.0, self.X)
        cautious = profiles.setting_for(s, self.CAL, 1.4, self.X)
        self.assertLessEqual(profiles.SPEEDS.index(gentle), profiles.SPEEDS.index(cautious))


class RepeatedPictures(unittest.TestCase):
    def make_link(self):
        from ytcrt import core_link, transport, yuv
        link = core_link.Link.__new__(core_link.Link)
        link.kind, link.sent, link.consumed = 'yuv', 0, 0
        link.copy_total = link.copy_max = 0.0
        link.copy_count = 0
        link.full_total, link.full_count, link.repeats = 0.0, 0, 0
        link._last_frame, link._last_at, link._last_size = None, -1, None
        link.last_progress = 0
        link.ov_ref = [-1] * yuv.OVERLAYS
        mem = bytearray(yuv.MAP_BYTES)
        link.box = transport.Mailbox(mem)
        link.box.write = lambda off, value: None            # the mailbox counters are not needed here
        return link, mem

    def test_an_identical_picture_is_sent_as_a_repeat_once_then_in_full(self):
        from ytcrt import yuv
        link, mem = self.make_link()
        a = bytearray(bytes(range(256)) * 90)                # 640x... a 16x? small picture: use 128x120 samples
        w, h = 128, 120
        frame = bytearray(w * h * 3 // 2)
        frame[:256] = bytes(range(256))
        audio = bytes(6400)
        link.push_picture(frame, audio, w, h)
        link.push_picture(frame, audio, w, h)                # same picture: a repeat of slot 0
        link.push_picture(bytearray(frame), audio, w, h)     # equal contents, different object: but follows a repeat, so full
        link.push_picture(bytearray(frame), audio, w, h)     # and the next one may repeat it again
        self.assertEqual(link.repeats, 2)
        heads = [yuv.parse_header(mem[yuv.slot_offset(n):yuv.slot_offset(n) + 64]) for n in range(4)]
        self.assertEqual([x['ref'] for x in heads], [None, 0, None, 2])
        # a repeat does not copy the planes: slot 1's Y area is still untouched
        self.assertEqual(bytes(mem[yuv.slot_offset(1) + yuv.Y_OFF:yuv.slot_offset(1) + yuv.Y_OFF + 256]), bytes(256))
        self.assertEqual(bytes(mem[yuv.slot_offset(2) + yuv.Y_OFF:yuv.slot_offset(2) + yuv.Y_OFF + 256]), bytes(range(256)))
        link.box.close()

    def test_a_changed_picture_or_size_is_always_sent_in_full(self):
        from ytcrt import yuv
        link, mem = self.make_link()
        audio = bytes(6400)
        f1 = bytearray(128 * 120 * 3 // 2)
        f2 = bytearray(f1)
        f2[5] = 9
        link.push_picture(f1, audio, 128, 120)
        link.push_picture(f2, audio, 128, 120)
        link.push_picture(bytearray(64 * 60 * 3 // 2), audio, 64, 60)
        self.assertEqual(link.repeats, 0)
        link.box.close()


class Progressive240(unittest.TestCase):
    def test_240p_samples_the_centre_of_each_two_by_two_cell(self):
        from ytcrt import yuv
        i = yuv.layout(640, 360, 'Off', True)
        p = yuv.layout(640, 360, 'Off', False)
        self.assertEqual(p['hstep'], i['hstep'])
        self.assertEqual(p['sx0'] - i['sx0'], i['hstep'] // 2)           # half a step further right
        self.assertEqual(p['sy0'] - i['sy0'], round(i['vstep'] / 2))      # and half a step further down


class PlaylistSearch(unittest.TestCase):
    OUT = "\n".join([
        '{"id": "PLabcdefghijklmn", "title": "He-Man Cartoons", "channel": "Retro TV", "playlist_count": 65}',
        '{"id": "RDabcdefghijklmn12", "title": "Mix - something", "channel": "YouTube"}',
        '{"id": "PLabcdefghijklmn", "title": "duplicate", "channel": "x"}',
        '{"id": null, "title": "From the link", "url": "https://www.youtube.com/playlist?list=PLzyxwvutsrqponm", "uploader": "Someone"}',
        '{"id": "UCnotaplaylistxxxxxxxxxx", "title": "A channel"}',
        'not json'])

    def test_parsing_keeps_real_playlists_once_and_drops_mixes_and_channels(self):
        found = pipeline.parse_found_playlists(self.OUT)
        self.assertEqual([f['id'] for f in found], ['PLabcdefghijklmn', 'PLzyxwvutsrqponm'])
        self.assertEqual(found[0], {'id': 'PLabcdefghijklmn', 'title': 'He-Man Cartoons', 'channel': 'Retro TV', 'count': 65})
        self.assertEqual(found[1]['channel'], 'Someone')

    def test_second_search_filter_is_tried_when_the_first_finds_nothing(self):
        calls = []
        saved = pipeline.capture
        rt = type('Rt', (), {'resolver': Path('yt-dlp'), 'ca': Path('ca')})()

        def fake(cmd, env, cancel, timeout=0, **kw):
            calls.append(cmd[-1])
            return (b'' if len(calls) == 1 else self.OUT.encode())
        pipeline.capture = fake
        try:
            found = pipeline.search_playlists(rt, 'he man', limit=5)
        finally:
            pipeline.capture = saved
        self.assertEqual(len(calls), 2)
        self.assertIn('search_query=he+man', calls[0])
        self.assertIn('sp=EgIQAw%253D%253D', calls[0])
        self.assertIn('sp=EgIQAw%3D%3D', calls[1])
        self.assertEqual(len(found), 2)
        with self.assertRaises(ValueError):
            pipeline.search_playlists(rt, '   ')

    def test_a_later_page_asks_yt_dlp_for_later_results(self):
        seen = []
        saved = pipeline.capture
        rt = type('Rt', (), {'resolver': Path('yt-dlp'), 'ca': Path('ca')})()
        pipeline.capture = lambda cmd, env, cancel, timeout=0, **kw: (seen.append(cmd), self.OUT.encode())[1]
        try:
            page = pipeline.search_playlists(rt, 'he man', limit=20, start=41)
        finally:
            pipeline.capture = saved
        cmd = seen[0]
        self.assertEqual(cmd[cmd.index('--playlist-start') + 1], '41')
        self.assertEqual(cmd[cmd.index('--playlist-end') + 1], '80')        # over-fetches twice the page: mixes and channels are dropped
        self.assertEqual(page.raw, 5)                                       # the next page starts at 41 + 5

    def make_app(self):
        a, st = ListsInTheGui.make_app(self)
        a.rt.resolver, a.rt.ca = Path('yt-dlp'), Path('ca')
        return a, st

    def test_search_open_save_flow(self):
        self.assertEqual(ui.Home.ENTRIES[:2], ('Search', 'Search Playlists'))
        a, st = self.make_app()
        found = [{'id': 'PLabcdefghijklmn', 'title': 'He-Man Cartoons', 'channel': 'Retro TV', 'count': 65}]
        page = pipeline.Page(vid(i, title='Episode %d' % i) for i in range(3))
        page.raw = 3
        saved = (pipeline.search_playlists, pipeline.playlist_entries)
        pipeline.search_playlists = lambda rt, query, cancel=None, log=None, limit=20: found
        seen_bg = []

        def fake_entries(rt, pid, cancel=None, log=None, limit=200, start=1, background=True):
            seen_bg.append(background)
            return 'He-Man', page
        pipeline.playlist_entries = fake_entries
        try:
            self.assertEqual(a._playlist_search_typed(a, 'x'), 'Type something to search for.')
            a.open_home_entry('Search Playlists')
            a.screen.text = ' he   man '
            a.screen.handle(a, 'submit')
            lst = a.screen
            self.assertIsInstance(lst, ui.FoundPlaylists)
            self.assertEqual(lst.label(a, found[0]), 'He-Man Cartoons')
            self.assertEqual(lst.right(a, found[0]), '65')
            lst.handle(a, 'alt')                                              # Y: save it
            self.assertIn('PLabcdefghijklmn', open(st.p_subs, encoding='utf-8').read())
            lst.handle(a, 'ok')                                               # A: open it
            videos = a.screen
            self.assertIsInstance(videos, ui.VideoList)
            self.assertEqual([v['title'] for v in videos.items(a)], ['Episode 0', 'Episode 1', 'Episode 2'])
            self.assertEqual(seen_bg, [False])                              # a fetch the user waits for must not be frozen by pause_background()
        finally:
            pipeline.search_playlists, pipeline.playlist_entries = saved


    def test_load_more_playlists_pages_the_search(self):
        a, st = self.make_app()

        def rows(first, n, raw):
            page = pipeline.Page({'id': 'PL%011d' % i, 'title': 'List %d' % i, 'channel': 'c', 'count': 3} for i in range(first, first + n))
            page.raw = raw
            return page
        calls = []

        def fake_search(rt, query, cancel=None, log=None, limit=20, start=1):
            calls.append(start)
            return rows(0, 12, 14) if start == 1 else rows(10, 12, 14)         # the second page repeats two playlists
        saved = pipeline.search_playlists
        pipeline.search_playlists = fake_search
        try:
            a._playlist_search_typed(a, 'he man')
            lst = a.screen
            self.assertIsInstance(lst, ui.FoundPlaylists)
            self.assertTrue(lst.items(a)[-1].get('_more'))                       # a "Load more playlists..." row follows the 12
            self.assertEqual(lst.label(a, lst.items(a)[-1]), 'Load more playlists...')
            lst.activate(a, lst.items(a)[-1])
            end = time.monotonic() + 5
            while len(calls) < 2 or lst.is_loading(a):
                self.assertLess(time.monotonic(), end)
                time.sleep(0.02)
            self.assertEqual(calls, [1, 15])                                     # the next page starts after the 14 results YouTube sent
            self.assertEqual(len([r for r in lst.items(a) if not r.get('_more')]), 22)   # 12 + 12 minus the 2 repeats
        finally:
            pipeline.search_playlists = saved


class CpuLog(unittest.TestCase):
    def test_proc_stat_with_spaces_in_the_name(self):
        from ytcrt import cpu
        text = '123 (ff mpeg (x)) S 1 2 3 4 5 6 7 8 9 10 400 150 0 0 20 0 1 0 100 0 0'
        self.assertEqual(cpu.parse_proc_stat(text), 550)
        self.assertEqual(cpu.parse_cpu_line('cpu  100 0 50 800 50 0 0 0 0 0' + chr(10) + 'cpu0 1 2 3'), (150, 850))

    def test_watch_reports_percent_of_one_core(self):
        from ytcrt import cpu
        files = {}
        clock = [0.0]

        def read(path):
            return files.get(path)
        files['/proc/%d/stat' % os.getpid()] = '1 (python3) S 1 2 3 4 5 6 7 8 9 10 0 0 0 0 20 0 1 0 100 0 0'
        files['/proc/77/stat'] = '77 (ffmpeg) S 1 2 3 4 5 6 7 8 9 10 0 0 0 0 20 0 1 0 100 0 0'
        files['/proc/stat'] = 'cpu 0 0 0 0 0 0 0 0 0 0'
        w = cpu.CpuWatch({'video ffmpeg': 77}, read, lambda: clock[0])
        clock[0] = 10.0
        files['/proc/%d/stat' % os.getpid()] = '1 (python3) S 1 2 3 4 5 6 7 8 9 10 300 0 0 0 20 0 1 0 100 0 0'      # 300 jiffies in 10 s = 30%
        files['/proc/77/stat'] = '77 (ffmpeg) S 1 2 3 4 5 6 7 8 9 10 1500 0 0 0 20 0 1 0 100 0 0'                  # 150%
        files['/proc/stat'] = 'cpu 1500 0 300 200 0 0 0 0 0 0'
        text = w.sample()
        self.assertIn('player 30%', text)
        self.assertIn('video ffmpeg 150%', text)
        self.assertIn('idle 10% of all cores', text)


class PlaylistCooldown(unittest.TestCase):
    def test_a_failed_playlist_is_not_retried_straight_away(self):
        tmp = tempfile.mkdtemp()
        fd = offline_feeds(tmp + '/cache')
        calls = []

        def fetch(pid):
            calls.append(pid)
            raise RuntimeError('YouTube URL resolution timed out.')
        fd.playlist_fetch = fetch
        lists = [('Retro', 'PLabcdefghijklmn')]
        fd.refresh_lists(lists, []).join(10)
        fd.refresh_lists(lists, []).join(10)                    # a background refresh soon after: skipped
        self.assertEqual(len(calls), 1)
        fd.refresh_lists(lists, [], force=True).join(10)        # X on the Playlists screen: tries again
        self.assertEqual(len(calls), 2)


class ProbeScripts(unittest.TestCase):
    def test_probe_scripts_can_be_passed_as_arguments(self):
        from ytcrt import core_link
        for name in ('FB_PROBE', 'FAST_COPY_PROBE'):
            text = getattr(core_link.Link, name)
            self.assertNotIn(chr(0), text, name)                        # a NUL character cannot be an argument: it ended the session once
            compile(text, name, 'exec')                                 # and the script is valid Python

    def test_a_probe_that_cannot_start_returns_nothing(self):
        import subprocess
        from ytcrt import core_link
        link = core_link.Link.__new__(core_link.Link)
        link.kind = 'yuv'
        saved = subprocess.run

        def boom(*a, **k):
            raise ValueError('embedded null byte')
        subprocess.run = boom
        try:
            self.assertIsNone(link.measure_fb_copy())
        finally:
            subprocess.run = saved


class FramebufferPictures(unittest.TestCase):
    """The v0.16 core reads pictures from the Linux framebuffer: fast to write, found by the address in header word 6."""

    def make(self, alias=True):
        import mmap
        import tempfile
        from ytcrt import transport, yuv
        tmp = tempfile.TemporaryFile()
        tmp.truncate(yuv.FB_MIN_LENGTH)
        fb_map = mmap.mmap(tmp.fileno(), yuv.FB_MIN_LENGTH)
        # the uncached window the writes are checked through: the same memory as the framebuffer (or, for the failure test, not)
        view_map = (mmap.mmap(tmp.fileno(), 8 * yuv.FB_SLOT_STRIDE, offset=yuv.FB_SLOTS_OFFSET) if alias
                    else mmap.mmap(-1, 8 * yuv.FB_SLOT_STRIDE))
        fb = transport.Framebuffer(0x22001000, yuv.FB_MIN_LENGTH, fb_map, view_map)
        return tmp, fb

    def link(self, fb, logs):
        from ytcrt import core_link, transport, yuv
        link = core_link.Link.__new__(core_link.Link)
        link.kind, link.sent, link.consumed = 'yuv', 0, 0
        link.copy_total = link.copy_max = 0.0
        link.copy_count = 0
        link.full_total, link.full_count, link.repeats = 0.0, 0, 0
        link._last_frame, link._last_at, link._last_size, link._last_in_fb = None, -1, None, False
        link.last_progress = 0
        link.ov_ref = [-1] * yuv.OVERLAYS
        link.log = logs.append
        link.plain = bytearray(yuv.MAP_BYTES)
        link.box = transport.Mailbox(link.plain)
        link.box.write = lambda off, value: None
        link.box.fb = fb
        link.box.use_method('fb')
        return link

    def test_planes_land_in_the_framebuffer_slot_and_are_checked(self):
        from ytcrt import yuv
        tmp, fb = self.make()
        y, u, v = bytes(range(256)) * 90, bytes(range(200)) * 30, bytes(range(100)) * 60
        fb.write_planes(2, [(yuv.Y_OFF, y), (yuv.U_OFF, u), (yuv.V_OFF, v)])
        slot = yuv.FB_SLOTS_OFFSET + 2 * yuv.FB_SLOT_STRIDE
        self.assertEqual(bytes(fb.fb_map[slot + yuv.Y_OFF:slot + yuv.Y_OFF + len(y)]), y)
        self.assertEqual(bytes(fb.fb_map[slot + yuv.V_OFF:slot + yuv.V_OFF + len(v)]), v)
        self.assertEqual(fb.pic_base(2), 0x22001000 + slot)
        with self.assertRaises(ValueError):
            fb.write_planes(0, [(yuv.FB_SLOT_STRIDE - 10, bytes(100))])        # a piece that would run into the next slot
        fb.close()
        tmp.close()

    def test_a_write_that_cannot_be_confirmed_is_refused(self):
        from ytcrt import yuv
        tmp, fb = self.make(alias=False)
        with self.assertRaises(RuntimeError):
            fb.write_planes(1, [(yuv.Y_OFF, bytes(range(256)) * 20)])
        fb.close()
        tmp.close()

    def test_pictures_go_to_the_framebuffer_and_the_header_names_the_address(self):
        from ytcrt import yuv
        tmp, fb = self.make()
        logs = []
        link = self.link(fb, logs)
        w, h = 128, 96
        frame = bytearray((i * 7) & 255 for i in range(w * h * 3 // 2))
        audio = bytes(6400)
        link.push_picture(frame, audio, w, h)
        link.push_picture(frame, audio, w, h)                       # identical: a repeat naming the previous record's planes
        link.push_picture(bytearray(frame), audio, w, h)            # equal contents but the previous record was a repeat: in full
        heads = [yuv.parse_header(link.plain[yuv.slot_offset(n):yuv.slot_offset(n) + 64]) for n in range(3)]
        self.assertEqual([x['pic_base'] for x in heads], [fb.pic_base(0), fb.pic_base(0), fb.pic_base(2)])
        self.assertEqual(link.repeats, 1)
        slot0 = yuv.FB_SLOTS_OFFSET
        self.assertEqual(bytes(fb.fb_map[slot0 + yuv.Y_OFF:slot0 + yuv.Y_OFF + w * h]), bytes(frame[:w * h]))
        # the uncached slot got only the audio and the header: no planes
        self.assertEqual(bytes(link.plain[yuv.slot_offset(0) + yuv.Y_OFF:yuv.slot_offset(0) + yuv.Y_OFF + 64]), bytes(64))
        self.assertEqual(logs, [])
        link.box.close()
        tmp.close()

    def test_a_failed_framebuffer_write_falls_back_to_the_normal_copy_for_good(self):
        from ytcrt import yuv
        tmp, fb = self.make(alias=False)                            # every check fails
        logs = []
        link = self.link(fb, logs)
        w, h = 128, 96
        frame = bytearray((i * 5) & 255 for i in range(w * h * 3 // 2))
        link.push_picture(frame, bytes(6400), w, h)
        self.assertEqual(link.box.method, 'memmove')
        self.assertTrue(any('Framebuffer copy failed' in m for m in logs))
        head = yuv.parse_header(link.plain[yuv.slot_offset(0):yuv.slot_offset(0) + 64])
        self.assertIsNone(head['pic_base'])                          # this record carries its planes in its own slot
        base = yuv.slot_offset(0) + yuv.Y_OFF
        self.assertEqual(bytes(link.plain[base:base + w * h]), bytes(frame[:w * h]))
        link.box.close()
        tmp.close()

    def test_the_method_is_refused_without_a_framebuffer(self):
        from ytcrt import transport, yuv
        box = transport.Mailbox(bytearray(yuv.MAP_BYTES))
        box.use_method('fb')
        self.assertEqual(box.method, 'memmove')
        self.assertFalse(box.fb_active)
        box.close()

    def test_the_speed_test_compares_the_framebuffer_with_the_other_copies(self):
        from ytcrt import yuv
        tmp, fb = self.make()
        link = self.link(fb, [])
        link.copy_bytes, link.copy_report = 0, {}
        avg, worst = link.measure_copy(2)
        self.assertIn('fb', link.copy_report)
        self.assertIn('memmove', link.copy_report)
        link.box.close()
        tmp.close()


class KnownCores(unittest.TestCase):
    def test_every_identity_the_player_waits_for_is_a_known_core(self):
        from ytcrt import core_link, transport
        for ident in transport.IDENTS:
            self.assertIn(ident, core_link.KINDS, hex(ident))           # a core the player waits for but cannot adopt shows only its test pattern

    def test_adopting_the_v016_core_makes_it_a_yuv_link_that_can_use_the_framebuffer(self):
        import mmap
        from ytcrt import core_link, transport, yuv
        link = core_link.Link.__new__(core_link.Link)
        link.box = transport.Mailbox(bytearray(yuv.MAP_BYTES))
        link.fb_candidate = object()                                    # stands in for an opened framebuffer
        link._adopt(transport.IDENT_FB)
        self.assertEqual(link.kind, 'yuv')
        self.assertIsNotNone(link.box.fb)
        self.assertTrue(link.big_core)                                  # the v0.16b core takes pictures wider than 640
        link2 = core_link.Link.__new__(core_link.Link)
        link2.box = transport.Mailbox(bytearray(yuv.MAP_BYTES))
        link2.fb_candidate = object()
        link3 = core_link.Link.__new__(core_link.Link)
        link3.box = transport.Mailbox(bytearray(yuv.MAP_BYTES))
        link3.fb_candidate = object()
        link3._adopt(transport.IDENT_FB6)                               # the first v0.16 core: framebuffer yes, big pictures no
        self.assertIsNotNone(link3.box.fb)
        self.assertFalse(link3.big_core)
        link3.box.fb = None
        link3.box.close()
        link2._adopt(transport.IDENT_YUV)                               # the v0.15 core never gets the framebuffer
        self.assertIsNone(link2.box.fb)
        link.box.fb = link2.box.fb = None
        link.box.close()
        link2.box.close()


class PipeSize(unittest.TestCase):
    def test_enlarging_a_pipe_never_raises(self):
        proc = type('P', (), {'stdout': object()})()                  # no usable file descriptor, and no fcntl on some systems
        self.assertFalse(pipeline.enlarge_pipe(proc))

    def test_framebuffer_write_times_are_recorded(self):
        t = FramebufferPictures()
        tmp, fb = t.make()
        from ytcrt import yuv
        fb.write_planes(0, [(yuv.Y_OFF, bytes(range(256)) * 40)])
        fb.write_planes(1, [(yuv.Y_OFF, bytes(range(256)) * 40)])
        self.assertEqual(fb.stats[3], 2)
        self.assertTrue(all(x >= 0 for x in fb.stats[:3]))
        fb.close()
        tmp.close()


class DecoderThreads(unittest.TestCase):
    def test_the_thread_count_goes_into_the_video_decoder_command_only(self):
        rt = type('Rt', (), {'ffmpeg': 'ffmpeg', 'ca': 'ca'})()
        src = {'url': 'https://x/v', 'headers': {}}
        saved = pipeline.DECODE_THREADS
        try:
            pipeline.DECODE_THREADS = 2
            default = pipeline.decoder_command(rt, profiles.PROFILE, src, 'video')
            self.assertEqual(default[default.index('-threads') + 1], '2')
            three = pipeline.decoder_command(rt, profiles.PROFILE, src, 'video', threads=3)
            self.assertEqual(three[three.index('-threads') + 1], '3')
            pipeline.DECODE_THREADS = 4
            self.assertEqual(pipeline.decoder_command(rt, profiles.PROFILE, src, 'video')[4:8].count('4'), 1)
            audio = pipeline.decoder_command(rt, profiles.PROFILE, src, 'audio')
            self.assertEqual(audio[audio.index('-threads') + 1], '1')         # the audio decoder never needs more than one
        finally:
            pipeline.DECODE_THREADS = saved

    def test_the_framebuffer_check_is_sampled(self):
        from ytcrt import yuv
        t = FramebufferPictures()
        tmp, fb = t.make()
        for n in range(40):
            fb.write_planes(n, [(yuv.Y_OFF, bytes(range(256)) * 20)])
        self.assertEqual(fb.stats[3], 40)
        # a corrupted view is only noticed on the checked pictures: the first 16, then every 16th
        fb.view_map[0:16] = bytes(16)
        fb.write_planes(40, [(yuv.Y_OFF, bytes(range(256)) * 20)])           # picture 41 of 41: not a checked one (41 % 16 != 0)
        fb.close()
        tmp.close()


class BigPictures(unittest.TestCase):
    """v0.16 core with the framebuffer: pictures up to 864 x 480 go in as they are and the FPGA shrinks them."""

    def test_plane_offsets(self):
        from ytcrt import yuv
        self.assertEqual(yuv.plane_offsets(640, 480), (yuv.U_OFF, yuv.V_OFF))                # classic sizes keep the fixed positions
        u, v = yuv.plane_offsets(848, 480)
        self.assertEqual(u % 4096, 0)
        self.assertEqual(v % 4096, 0)
        self.assertGreaterEqual(u, yuv.Y_OFF + 848 * 480)                                   # no overlap with Y
        self.assertGreaterEqual(v, u + 424 * 240)                                           # nor between U and V
        self.assertLessEqual(v + 424 * 240, yuv.FB_SLOT_STRIDE)                             # and it fits a framebuffer slot
        with self.assertRaises(ValueError):
            yuv.plane_offsets(864, 486 * 2)

    def test_header_carries_the_offsets_only_for_big_pictures(self):
        from ytcrt import yuv
        big = yuv.parse_header(yuv.header_for(848, 480, 'Off', True, 0, None, None, 0x22101000))
        self.assertEqual((big['u_off'], big['v_off']), yuv.plane_offsets(848, 480))
        self.assertEqual(big['pic_base'], 0x22101000)
        small = yuv.header_for(640, 360, 'Off', True, 0, None, None, 0x22101000)
        self.assertEqual(yuv.parse_header(small)['u_off'], yuv.U_OFF)                       # word 7 stays 0: the core's defaults
        with self.assertRaises(ValueError):
            yuv.header_for(848, 480, 'Off', True)                                           # a big picture needs the framebuffer address

    def test_big_stream_goes_in_unscaled_with_the_framebuffer_and_scaled_without(self):
        media = type('M', (), {'video': {'width': 854, 'height': 480}})()
        self.assertEqual(pipeline.yuv_target(media, True, big=True), {'w': 848, 'h': 480, 'prescale': None})
        scaled = pipeline.yuv_target(media, True, big=False)
        self.assertLessEqual(scaled['w'], 640)
        self.assertIsNotNone(scaled['prescale'])
        wide = type('M', (), {'video': {'width': 1280, 'height': 720}})()
        self.assertLessEqual(pipeline.yuv_target(wide, True, big=True)['w'], 864)           # something bigger is still scaled down first

    def test_a_big_picture_is_written_where_its_header_says(self):
        from ytcrt import yuv
        t = FramebufferPictures()
        tmp, fb = t.make()
        import mmap
        from ytcrt import transport
        plain = mmap.mmap(-1, yuv.MAP_BYTES)
        box = transport.Mailbox(plain)
        box.fb = fb
        box.use_method('fb')
        w, h = 848, 480
        frame = bytes((i * 3) & 255 for i in range(w * h * 3 // 2))
        header = yuv.header_for(w, h, 'Off', True, 0, None, None, fb.pic_base(1))
        box.picture(1, header, frame, w * h, bytes(6400), in_fb=True)
        head = yuv.parse_header(header)
        slot = yuv.FB_SLOTS_OFFSET + 1 * yuv.FB_SLOT_STRIDE
        self.assertEqual(bytes(fb.fb_map[slot + yuv.Y_OFF:slot + yuv.Y_OFF + 100]), frame[:100])
        self.assertEqual(bytes(fb.fb_map[slot + head['u_off']:slot + head['u_off'] + 100]), frame[w * h:w * h + 100])
        self.assertEqual(bytes(fb.fb_map[slot + head['v_off']:slot + head['v_off'] + 100]), frame[w * h + w * h // 4:w * h + w * h // 4 + 100])
        box.fb = None
        box.close()
        fb.close()
        tmp.close()

    def test_copy_cost_follows_the_real_picture_size_in_big_mode(self):
        s = stream(854, 480, 500)
        cal = {'px': 640 * 360, 'tbr': 307.0, 'fps': 30.0, 'cost': 0.4, 'copy_ms': 8.0, 'copy_bytes': 345600}
        plain = profiles.copy_cost(s, cal, 30)
        big = profiles.copy_cost(s, dict(cal, big=True), 30)
        self.assertGreater(big, plain)                                                      # 848 x 480 is bigger than the 640 x 360 it was shrunk to


class ConsoleCursor(unittest.TestCase):
    def test_quiet_console_hides_the_cursor_and_stops_the_blink(self):
        import os
        import tempfile
        from ytcrt import transport
        d = tempfile.mkdtemp()
        tty, blink = os.path.join(d, 'tty1'), os.path.join(d, 'cursor_blink')
        open(tty, 'w').close()
        open(blink, 'w').write('1')
        self.assertEqual(transport.quiet_console((os.path.join(d, 'nodir', 'tty'), tty), blink), 2)     # skips a missing tty, uses the next
        self.assertEqual(open(tty).read(), chr(27) + '[?25l')
        self.assertEqual(open(blink).read(), '0')

    def test_quiet_console_is_harmless_where_there_is_no_console(self):
        from ytcrt import transport
        self.assertEqual(transport.quiet_console(('/nonexistent/tty',), '/nonexistent/blink'), 0)

    def test_pictures_live_at_the_far_end_of_the_framebuffer(self):
        import mmap
        import tempfile
        from ytcrt import transport, yuv
        length = 8294400                                                    # 1920 x 1080 x 4, what /dev/fb0 reports on the MiSTer
        slots_offset = max(yuv.FB_SLOTS_OFFSET, (length - 8 * yuv.FB_SLOT_STRIDE) & ~0xFFFF)
        self.assertGreater(slots_offset, 0x200000)                          # well below the top, where console text and its cursor are
        self.assertEqual(slots_offset % 0x10000, 0)
        self.assertLessEqual(slots_offset + 8 * yuv.FB_SLOT_STRIDE, length)  # and it all fits
        tmp = tempfile.TemporaryFile()
        tmp.truncate(slots_offset + 8 * yuv.FB_SLOT_STRIDE)
        fb_map = mmap.mmap(tmp.fileno(), slots_offset + 8 * yuv.FB_SLOT_STRIDE)
        view = mmap.mmap(tmp.fileno(), 8 * yuv.FB_SLOT_STRIDE, offset=slots_offset)
        fb = transport.Framebuffer(0x22001000, length, fb_map, view, slots_offset)
        self.assertEqual(fb.pic_base(3), 0x22001000 + slots_offset + 3 * yuv.FB_SLOT_STRIDE)
        fb.write_planes(3, [(yuv.Y_OFF, bytes(range(256)) * 10)])           # written and checked at the new place
        self.assertEqual(bytes(fb_map[slots_offset + 3 * yuv.FB_SLOT_STRIDE + yuv.Y_OFF:][:256]), bytes(range(256)))
        fb.close()
        tmp.close()


class FramebufferParallel(unittest.TestCase):
    def test_big_pictures_are_written_by_two_threads_exactly_and_small_ones_by_one(self):
        from ytcrt import yuv
        t = FramebufferPictures()
        for parallel in (True, False):
            tmp, fb = t.make()
            fb.parallel = parallel
            y = bytes((i * 11) & 255 for i in range(407040))
            u = bytes((i * 5) & 255 for i in range(101760))
            fb.write_planes(2, [(yuv.Y_OFF, y), (0x64000, u), (0x7D000, u)])
            slot = yuv.FB_SLOTS_OFFSET + 2 * yuv.FB_SLOT_STRIDE
            self.assertEqual(bytes(fb.fb_map[slot + yuv.Y_OFF:slot + yuv.Y_OFF + len(y)]), y)
            self.assertEqual(bytes(fb.fb_map[slot + 0x64000:slot + 0x64000 + len(u)]), u)
            self.assertEqual(bytes(fb.fb_map[slot + 0x7D000:slot + 0x7D000 + len(u)]), u)
            self.assertEqual(fb.worker is not None, parallel)               # the helper thread only exists when it is used
            fb.write_planes(3, [(yuv.Y_OFF, bytes(1000))])                  # small: written by the caller alone
            fb.close()
            tmp.close()


class ConsoleDetach(unittest.TestCase):
    def make_root(self):
        import os
        import tempfile
        root = tempfile.mkdtemp()
        for name, label, bind in (('vtcon0', '(S) dummy device', '1'), ('vtcon1', '(M) frame buffer device', '1')):
            os.makedirs(os.path.join(root, name))
            open(os.path.join(root, name, 'name'), 'w').write(label)
            open(os.path.join(root, name, 'bind'), 'w').write(bind)
        return root

    def test_only_the_framebuffer_console_is_switched(self):
        import os
        from ytcrt import transport
        root = self.make_root()
        self.assertEqual(transport.console_bind(False, root), ['vtcon1'])
        self.assertEqual(open(os.path.join(root, 'vtcon1', 'bind')).read(), '0')
        self.assertEqual(open(os.path.join(root, 'vtcon0', 'bind')).read(), '1')        # the dummy console is left alone
        self.assertEqual(transport.console_bind(True, root), ['vtcon1'])
        self.assertEqual(open(os.path.join(root, 'vtcon1', 'bind')).read(), '1')

    def test_nothing_happens_without_a_vtconsole(self):
        from ytcrt import transport
        self.assertEqual(transport.console_bind(False, '/nonexistent/vtconsole'), [])

    def test_the_link_detaches_for_a_framebuffer_core_and_attaches_again_when_the_session_ends(self):
        from ytcrt import core_link, transport, yuv
        calls = []
        saved = (transport.console_bind, transport.console_graphics)
        transport.console_bind = lambda on, root=None: (calls.append(on), ['vtcon1'])[1]
        transport.console_graphics = lambda on, paths=None: True
        try:
            link = core_link.Link.__new__(core_link.Link)
            link.log = lambda m: None
            link.box = transport.Mailbox(bytearray(yuv.MAP_BYTES))
            link.box.fb = object()
            link.session = None
            link._adopt(transport.IDENT_FB)
            link._adopt(transport.IDENT_FB)                                          # polled again: not detached twice
            self.assertEqual(calls, [False])
            link.end_session()
            self.assertEqual(calls, [False, True])
            link._adopt(transport.IDENT_FB)                                          # a new session detaches again
            self.assertEqual(calls, [False, True, False])
            old = core_link.Link.__new__(core_link.Link)
            old.log = lambda m: None
            old.box = transport.Mailbox(bytearray(yuv.MAP_BYTES))
            old._adopt(transport.IDENT_YUV)                                          # the v0.15 core never touches the console
            self.assertEqual(calls, [False, True, False])
            link.box.fb = None
            link.box.close()
            old.box.close()
        finally:
            transport.console_bind, transport.console_graphics = saved


class ConsoleGraphics(unittest.TestCase):
    def test_graphics_mode_is_harmless_where_there_is_no_console(self):
        from ytcrt import transport
        self.assertFalse(transport.console_graphics(True, ('/nonexistent/tty0',)))


if __name__ == '__main__':
    unittest.main()
