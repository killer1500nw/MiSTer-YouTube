"""The on-screen app: menus and playback share one streaming session, so the
GUI is simply more 320x240 frames pushed through the same DDR mailbox."""
import threading
import re
import time

from . import feeds as feedsmod, pipeline, profiles, ui, urls
from .core_link import SLOTS
from .gfx import Canvas
from .store import RESUME_MIN_MS

TEST_CLIP = 'M7kB0lis3xg'
# The core starts consuming only once 4 records are queued (PROTOCOL.md prefill),
# so the menu must queue at least 4 or it never appears. ~130 ms of latency.
MENU_DEPTH = 4
SEEK_SMALL, SEEK_BIG = 10, 60  # seconds
SMOOTH_AFTER = 90.0            # seconds of clean playback of a demanding stream before Auto trusts its estimate more
STARVE_GRACE = 8.0             # seconds after (re)starting before starvation counts
STARVE_WINDOW = 12.0           # judge starvation over windows of this length
STARVE_LIMIT = 2.5             # seconds of an empty queue within a window that means 'too slow'
SEEK_SETTLE = 0.6              # seconds of no seek keys before the seek is actually performed
OSD_SECONDS = 4.0


class Quit(Exception):
    pass


class Job:
    """Run a blocking function on a thread while the UI keeps streaming."""

    def __init__(self, fn):
        self.cancelled = threading.Event()
        self.result = self.error = None
        self.done = False
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def check(self):
        if self.cancelled.is_set():
            raise RuntimeError('Cancelled')

    def _run(self, fn):
        try:
            self.result = fn(self.check)
        except Exception as exc:
            self.error = exc
        self.done = True


class App:
    def __init__(self, link, store, feeds, rt, inputs, log=print):
        self.link, self.store, self.feeds, self.rt, self.inputs, self.log = link, store, feeds, rt, inputs, log
        self.canvas = Canvas(320, 240)
        self.stack = []
        self.dirty = True
        self.record = None
        self.silence = bytes(self.profile().audio_bytes)
        self.toast_text, self.toast_until = '', 0.0
        self.refresher = None
        self.lists_refresher = None
        self.last_gen = -1
        self.last_anim = 0.0
        self.home = ui.Home()
        self.stack.append(self.home)
        self.feeds.ffmpeg = str(rt.ffmpeg) if rt.ffmpeg.is_file() else None
        self.feeds.log = self.log
        pipeline.hook_log = self.log
        if self.feeds.playlist_fetch is None and getattr(rt, 'resolver', None) is not None and rt.resolver.is_file():
            self.feeds.playlist_fetch = lambda pid: pipeline.playlist_entries(
                rt, pid, lambda: None, self.log, limit=feedsmod.PAGE)
            self.feeds.page_fetch = lambda pid, start, count: pipeline.playlist_entries(
                rt, pid, lambda: None, self.log, limit=count, start=start)
        if self.feeds.fallback is None and getattr(rt, 'resolver', None) is not None and rt.resolver.is_file():
            self.feeds.fallback = lambda cid: pipeline.channel_uploads(rt, cid, lambda: None, self.log)

    # helpers ----------------------------------------------------------
    def profile(self):
        return profiles.PROFILE

    def input_debug(self):
        """Two short lines of raw input words from the core, for diagnosing controls."""
        from . import __version__
        try:
            b = self.link.box
            return ['pad %08x %08x' % (b.read(40), b.read(44)),
                    'key %08x %08x  v%s' % (b.read(48), b.read(52), __version__)]
        except Exception:
            return ['input words: n/a', 'v' + __version__]

    def profile_summary(self):
        s = self.store.settings
        return 'Quality %s  Crop %s' % (s['quality'], s['crop'])

    @property
    def screen(self):
        return self.stack[-1]

    def push(self, s):
        self.stack.append(s)
        self.dirty = True

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()
        self.dirty = True

    def go_home(self):
        del self.stack[1:]
        self.dirty = True

    def toast(self, text, seconds=2.0):
        self.toast_text, self.toast_until = text, time.monotonic() + seconds
        self.dirty = True

    def feeds_busy(self):
        return bool((self.refresher and self.refresher.is_alive()) or
                    (self.lists_refresher and self.lists_refresher.is_alive()))

    def refresh_feeds(self, force=False):
        if not (self.refresher and self.refresher.is_alive()):
            subs = self.store.subscriptions()
            self.refresher = self.feeds.refresh(subs, force)
            if subs:
                self.toast('Updating feeds...')
            elif not (self.store.playlists() or self.store.video_list()):
                self.toast('Nothing in your list yet')

    def refresh_lists(self, force=False):
        """Playlist contents and single-video titles (from subscriptions.txt)."""
        if not (self.lists_refresher and self.lists_refresher.is_alive()):
            self.lists_refresher = self.feeds.refresh_lists(self.store.playlists(), self.store.video_list(), force)
            if force:
                self.toast('Updating lists...')

    def load_more(self, kind, ident):
        """The "Load more videos" row: fetch the next page of a channel or playlist."""
        if self.feeds.load_more(kind, ident):
            self.toast('Loading more videos...')

    def no_lists_text(self, kind):
        return ['Nothing here yet.', 'Add %s links or IDs to:' % kind, self.store.p_subs,
                'or use Add to Lists on Home.']

    def no_subscriptions_text(self):
        return ['No channels yet.', 'Add channel IDs or @handles to:', self.store.p_subs,
                'then press X (Tab) to refresh.']

    # rendering --------------------------------------------------------
    def render(self):
        s = self.screen
        s.draw(self, self.canvas)
        if self.toast_text and time.monotonic() < self.toast_until:
            c = self.canvas
            w = len(self.toast_text) * 8 + 16
            x = (320 - w) // 2
            c.rect(x, 188, w, 22, ui.PANEL)
            c.frame(x, 188, w, 22, ui.RED, 2)
            c.text(x + 8, 195, self.toast_text, ui.WHITE, ui.PANEL)
        self.record = bytes(self.canvas.buf) + self.silence
        self.dirty = False

    def tick_ui(self):
        now = time.monotonic()
        if self.toast_text and now >= self.toast_until:
            self.toast_text = ''
            self.dirty = True
        if self.feeds.generation != self.last_gen:
            self.last_gen = self.feeds.generation
            self.dirty = True
        if (self.screen.animated() or self.screen.busy(self)) and now - self.last_anim > 0.12:
            self.last_anim = now
            self.dirty = True

    def pump(self, wait=0.01):
        """One iteration of the menu loop: health, input, redraw, keep the core fed."""
        self.link.health()
        self.inputs.text_mode = getattr(self.screen, 'wants_text', False)
        act = self.inputs.get(wait)
        if act:
            self.screen.handle(self, act)
            self.dirty = True
        self.tick_ui()
        if self.dirty or self.record is None:
            self.render()
        if self.link.queued < MENU_DEPTH:
            self.link.push(self.record)
        return act

    def run(self):
        self.refresh_feeds()
        self.refresh_lists()
        try:
            while True:
                self.pump()
        except Quit:
            pass

    # jobs -------------------------------------------------------------
    def run_job(self, title, lines, fn):
        """Show a spinner while `fn(check)` runs. B cancels. -> (result, error)."""
        job = Job(fn)
        self.push(ui.Message(title, lines, spinner=True, on_back=job.cancelled.set))   # lines may be a callable
        try:
            while not job.done:
                self.pump()
        finally:
            job.cancelled.set()
            self.pop()
        return job.result, job.error

    # actions ----------------------------------------------------------
    def open_home_entry(self, name):
        st = self.store
        shorts = lambda a: a.store.settings['shorts'] == 'On'
        if name == 'Subscriptions':
            self.push(ui.VideoList('Subscriptions', lambda a: a.feeds.feed(a.store.subscriptions(), shorts(a)),
                                   empty=lambda a: a.no_subscriptions_text() if not a.store.subscriptions()
                                   else ['Fetching videos...' if a.feeds_busy() else 'No videos found.'],
                                   status=lambda a: 'updating' if a.feeds_busy() else ''))
        elif name == 'Channels':
            self.push(ui.ChannelList())
        elif name == 'Watch Later':
            self.push(ui.VideoList('Watch Later', lambda a: a.store.later,
                                   hints='A:Play  Y:Remove  B:Back',
                                   empty=lambda a: ['Nothing saved yet.', 'Press Y on any video to save it.']))
        elif name == 'History':
            self.push(ui.VideoList('History', lambda a: a.store.history_list(),
                                   empty=lambda a: ['No watch history yet.']))
        elif name == 'Playlists':
            self.push(ui.PlaylistList())
        elif name == 'My Videos':
            self.push(ui.VideoList('My Videos', lambda a: a.feeds.listed_videos(a.store.video_list()),
                                   hints='A:Play  Y:Later  X:Refresh  B:Back', empty=lambda a: a.no_lists_text('video'),
                                   refresh=lambda a: a.refresh_lists(True)))
        elif name == 'Play URL / ID':
            self.push(ui.TextInput('Play URL or ID', 'YouTube video link or 11-character ID', self._play_typed))
        elif name == 'Add Channel via @handle':
            self.push(ui.TextInput('Add Channel', 'YouTube handle, e.g. @youtuberuser', self._add_handle, initial='@'))
        elif name == 'Add to Lists':
            self.push(ui.TextInput('Add to Lists', 'Channel, playlist or video link / ID', self._add_typed))
        elif name == 'Settings':
            self.push(ui.Settings())
        elif name == 'Quit':
            self.push(ui.Choice('Quit', 'Stop streaming and return to MiSTer?', ['Quit', 'Cancel'],
                                lambda i: self._quit() if i == 0 else None))

    def _play_typed(self, app, text):
        """TextInput callback: play a typed link/ID. Returns an error message, or None when handled."""
        try:
            _, vid = urls.normalise_url(text)
        except ValueError as exc:
            return str(exc)
        self.pop()
        self.log('Typed video id ' + vid)
        self.start_video({'id': vid, 'title': '', 'channel': '', 'typed': True})
        return None

    def _add_handle(self, app, text):
        """TextInput callback for Add Channel: a bare @handle (the @ is optional). Returns an error message, or None."""
        handle = text.strip()
        if handle and not handle.startswith('@') and '/' not in handle and ':' not in handle:
            handle = '@' + handle
        if not re.fullmatch(r'@[A-Za-z0-9._-]{3,30}', handle):
            return 'Type a handle such as @youtuberuser'
        added = self.store.add_entry('channel', handle)
        self.pop()
        self.log('Typed channel %s (%s)' % (handle, 'added' if added else 'already listed'))
        self.toast('%s added' % handle if added else 'Already in your list')
        if added:
            self.refresh_feeds(force=True)
        return None

    def _add_typed(self, app, text):
        """TextInput callback: file whatever was typed under channels, playlists or videos."""
        kind, ref = self.store.classify(text)
        if kind is None:
            return ref                                    # the message
        added = self.store.add_entry(kind, ref)
        self.pop()
        self.log('Typed %s %s (%s)' % (kind, ref, 'added' if added else 'already listed'))
        self.toast(('%s added' % kind.capitalize()) if added else 'Already in your list')
        if added:
            if kind == 'channel':
                self.refresh_feeds(force=True)
            else:
                self.refresh_lists(force=True)
        return None

    def _quit(self):
        raise Quit()

    def settings_action(self, key):
        if key == 'clear_later':
            self.push(ui.Choice('Watch Later', 'Clear the whole list?', ['Clear', 'Cancel'],
                                lambda i: (self.store.clear_later(), self.toast('Watch Later cleared')) if i == 0 else None))
        elif key == 'clear_history':
            self.push(ui.Choice('History', 'Clear history and resume points?', ['Clear', 'Cancel'],
                                lambda i: (self.store.clear_history(), self.toast('History cleared')) if i == 0 else None))
        elif key == 'diag':
            self.push(ui.TextView('Diagnostics', self.diagnostics(), self.diagnostics))
        elif key == 'bench':
            self.run_benchmark()
    LOG_PATH = '/media/fat/YouTubeNative/YouTube-crt-log.txt'

    def diagnostics(self):
        """Text for the Diagnostics screen: versions, tool check, tail of the player log."""
        from . import __version__
        rt = self.rt
        out = ['Player v%s  core ident %08x' % (__version__, self.link.ident),
               'Quality: %s  Crop: %s' % (self.store.settings['quality'], self.store.settings['crop'])]
        for name, p in (('yt-dlp', rt.resolver), ('qjs', rt.qjs), ('ffmpeg', rt.ffmpeg)):
            out.append(('%s: OK' % name) if p.is_file() else ('!%s: MISSING' % name))
        out += ['Input: ' + ' '.join(self.input_debug()[:1]), '--- player log ---']
        try:
            with open(self.LOG_PATH, 'rb') as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 6000))
                raw = f.read().decode('utf-8', 'replace').splitlines()[-30:]
        except OSError:
            raw = ['(log not readable)']
        for line in raw:
            out += ui.wrap(line, 38, 6) or ['']
        return out

    def missing_tools(self):
        miss = self.rt.missing()
        if miss:
            self.push(ui.Message('Setup needed', ['Missing: ' + miss[0], 'Copy the YouTubeNative folder from the package to the SD card.'], ui.AMBER))
            return True
        return False

    def run_benchmark(self):
        """Measure decoding on this MiSTer with both cheap decoder settings and calibrate the Auto cost model."""
        if self.missing_tools():
            return
        prof = self.profile()
        # Test clips: your newest videos first (a realistic load), then a known good one. Livestreams and
        # other videos that cannot be decoded are skipped instead of failing the whole test.
        candidates = [v['id'] for v in self.feeds.feed(self.store.subscriptions(), False, 3)] + [TEST_CLIP]
        candidates = list(dict.fromkeys(candidates))

        def work(check):
            media = None
            for vid in candidates:
                try:
                    media = pipeline.resolve(self.rt, prof, vid, check, self.log, quality='360p')   # a mid-size reference
                    break
                except ValueError as exc:               # live, past livestream, no usable stream...: try the next one
                    self.log('Speed test: skipping %s (%s)' % (vid, exc))
            if media is None:
                raise RuntimeError('No video in your lists could be used as a test clip.')
            return [(sp, pipeline.benchmark(self.rt, prof, media, sp, 8, check)) for sp in ('Fast', 'Fastest')], media
        res, err = self.run_job('Speed test', ['Decoding a test stream (twice)...', 'B: cancel'], work)
        if err:
            self.push(ui.Message('Speed test failed', [str(err)[:140]], ui.AMBER))
            return
        results, media = res
        best_speed, best = max(results, key=lambda r: r[1])
        v = media.video
        lines = ['%s decoder: %.2fx real time' % (sp, f) for sp, f in results]
        lines.append('Test stream: %s, %d kbps, %d fps' % (media.label, round(media.tbr), round(v.get('fps') or 30)))
        self.store.set('cal', {'px': v['width'] * v['height'], 'tbr': float(v.get('tbr') or 300),
                               'fps': float(v.get('fps') or 30), 'cost': round(1.0 / best, 4)})
        self.store.set('speed_big', best_speed)
        self.store.set('cost_mult', 1.0)
        lines.append('Auto now picks streams by estimated load')
        self.log('Speed test %s: %s; calibration %s, speed_big=%s' % (
            media.label, ', '.join('%s %.2fx' % r for r in results), self.store.settings['cal'], best_speed))
        self.push(ui.Message('Speed test', lines, ui.GREEN if best >= 1.15 else ui.AMBER))

    # playback ---------------------------------------------------------
    def calibration(self):
        """(cal, mult): the measured reference stream and the learned correction."""
        cal = self.store.settings.get('cal')
        if not (isinstance(cal, dict) and cal.get('cost') and cal.get('px')):
            cal = profiles.CAL_DEFAULT
        return cal, float(self.store.settings.get('cost_mult', 1.0))

    def effective_speed(self, media, speed):
        """Decoder setting for this stream: big pictures use what the Speed test learned."""
        v = media.video or {}
        return profiles.speed_for((v.get('width') or 0) * (v.get('height') or 0), speed,
                                  self.store.settings.get('speed_big'))

    def start_video(self, v):
        pos = self.store.saved_pos(v['id'])
        if pos >= RESUME_MIN_MS and self.store.settings['resume'] == 'Ask':
            self.push(ui.Choice('Resume', v.get('title', ''),
                                ['Resume at ' + ui.mmss(pos), 'Start over'],
                                lambda i: self.play(v, pos if i == 0 else 0)))
        else:
            self.play(v, pos if pos >= RESUME_MIN_MS else 0)

    def _note_smooth(self, media):
        """A demanding stream ran for a while without starving: trust the cost estimate a little more."""
        if media.cost >= 0.8 * profiles.LIMIT:
            mult = max(0.7, float(self.store.settings.get('cost_mult', 1.0)) * 0.97)
            self.store.set('cost_mult', round(mult, 3))
            self.log('%s at estimated load %d%% played cleanly; cost multiplier now %.3f' %
                     (media.label, round(media.cost * 100), mult))

    def _resolve(self, v, prof, quality):
        """Ask YouTube (via yt-dlp) for the video, showing progress. -> Media, or None if it failed."""
        started, last = time.monotonic(), ['starting yt-dlp']
        cal, mult = self.calibration()
        res, err = self.run_job(
            'Loading',
            lambda: [ui.clip(v.get('title', ''), 33),
                     'Contacting YouTube... %ds' % (time.monotonic() - started), ui.clip(last[0], 33),
                     ('Security check: can take a few minutes. Please be patient.' if 'Solving JS' in last[0] else
                      'This can take a minute or two. Please be patient.')],
            lambda check: pipeline.resolve(self.rt, prof, v['id'], check, self.log,
                                           note=lambda t: last.__setitem__(0, t), quality=quality, cal=cal, mult=mult))
        if err:
            if str(err) != 'Cancelled':
                self.log('Cannot play: ' + ' | '.join(str(err).splitlines())[:600])
                self.push(ui.TextView('Cannot play', ['!' + l for l in ui.wrap(str(err), 37, 14)]))
            return None
        return res

    def play(self, v, start_ms):
        """Play a video. Background refreshing is paused from here until playback is over."""
        self.feeds.hold.set()
        pipeline.pause_background()                       # freeze any lookup already running
        try:
            self._play(v, start_ms)
        finally:
            self.feeds.hold.clear()
            pipeline.resume_background()

    def _play(self, v, start_ms):
        if self.missing_tools():
            return
        prof, speed = self.profile(), self.store.settings['decode_speed']
        q = self.store.settings.get('quality', 'Auto')
        self.log('Play %s "%s" profile=%s quality=%s speed=%s resume=%dms' %
                 (v['id'], v.get('title', '')[:60], prof.key, q, speed, start_ms))
        media = self._resolve(v, prof, q)
        if media is None:
            return
        if not v.get('title'):
            v['title'] = media.title                      # a typed link has no title until YouTube tells us
        refresh_left, pos_ms, outcome = 1, start_ms, 'stop'
        while True:
            cal, mult = self.calibration()
            can_step = q == 'Auto' and pipeline.step_down(media, prof, cal, mult) is not None
            try:
                outcome, pos_ms = self._play_loop(v, media, prof, self.effective_speed(media, speed), pos_ms,
                                                  can_step, refresh_left > 0)
            except Quit:
                raise
            except pipeline.PlaybackError as exc:
                self.log('Playback error: ' + ' | '.join(str(exc).splitlines())[:600])
                self.push(ui.TextView('Playback stopped', ['!' + l for l in ui.wrap(str(exc), 37, 14)]))
                return
            if outcome == 'downgrade':                    # the ARM could not keep up: learn from it, use a lighter stream
                mult = min(3.0, mult * 1.25)
                self.store.set('cost_mult', round(mult, 3))
                smaller = pipeline.step_down(media, prof, cal, mult, self.log)
                self.log('%s (estimated load %d%%) could not keep up; cost multiplier now %.3f; %s' % (
                    media.label, round(media.cost * 100), mult,
                    ('switching to %s' % smaller.label) if smaller else 'nothing lighter to switch to'))
                if smaller is not None:
                    self.toast('Too heavy for %s - using %s' % (media.label, smaller.label))
                    media = smaller
                continue
            if outcome == 'refresh':                      # YouTube kept refusing these URLs: ask for fresh ones once
                refresh_left -= 1
                self.toast('Refreshing the stream...')
                pipeline.forget(self.rt, v['id'])             # the saved URLs are what YouTube refused
                fresh = self._resolve(v, prof, q)
                if fresh is None:
                    return
                media = fresh
                continue
            break
        self.log('Playback %s at %s of %s' % ('finished' if outcome == 'eof' else 'stopped', ui.mmss(pos_ms), ui.mmss(media.duration * 1000)))
        self.store.record_play(v, pos_ms, media.duration * 1000, finished=(outcome == 'eof'))
        self.dirty = True

    def _play_loop(self, v, media, prof, speed, start_ms, may_fall_back=False, can_refresh=False):
        link, fps = self.link, prof.fps
        state = {'start': start_ms, 'base': link.sent, 'paused': False, 'pause_pushed': False,
                 'osd_until': time.monotonic() + OSD_SECONDS, 'note': '', 'last': None,
                 'pending': None, 'pending_at': 0.0, 'seeking': False,
                 'crop': self.store.settings.get('crop', 'Off'), 'more': None, 'crop_change': False,
                 'opened_at': time.monotonic(), 'starved': 0.0, 'win_start': time.monotonic(),
                 'tick': time.monotonic(), 'tier': media.tier, 'retries': 0, 'smooth_noted': False}
        dur_ms = media.duration * 1000

        def pos_ms():
            return min(dur_ms or 1 << 60, state['start'] + max(0, link.consumed - state['base']) * 1000 // fps)

        def open_pipe(at_ms):
            state['start'], state['base'] = at_ms, link.sent
            state['opened_at'] = time.monotonic()
            state['first_logged'] = False
            state['starved'], state['win_start'] = 0.0, time.monotonic()
            try:
                return pipeline.open_pipeline(self.rt, prof, media, at_ms / 1000.0, speed, lambda: None,
                                              crop=state['crop'])
            except Exception as exc:
                raise pipeline.PlaybackError(str(exc))

        def set_crop(mode, now):
            """Change the crop for this video. The picture filter lives in ffmpeg, so the decoders
            restart at the current position once changes settle (same path as a seek)."""
            if mode == state['crop']:
                return
            state['crop'] = mode
            if state['pending'] is None:
                state['pending'] = pos_ms()
            state.update(pending_at=now, crop_change=True, note='Crop: ' + mode)

        def more_key(act, now):
            """Keys while the More panel is open: up/down choose, left/right/ok change, back closes."""
            m = state['more']
            if act in ('back', 'menu'):
                state['more'] = None
            elif act == 'down':
                m['sel'] = (m['sel'] + 1) % 3
            elif act == 'up':
                m['sel'] = (m['sel'] - 1) % 3
            elif m['sel'] == 0 and act in ('left', 'right', 'ok'):
                step = -1 if act == 'left' else 1
                set_crop(profiles.CROPS[(profiles.CROPS.index(state['crop']) + step) % len(profiles.CROPS)], now)
            elif m['sel'] == 1 and act in ('left', 'right', 'ok'):
                state['note'] = 'Saved' if self.store.toggle_later(v) else 'Removed'
            elif m['sel'] == 2 and act == 'ok':
                state['more'] = None
            state['osd_until'] = now + OSD_SECONDS
            state['pause_pushed'] = False                 # redraw a paused frame so the panel updates
        pipe = open_pipe(start_ms)
        next_save = time.monotonic() + 10
        try:
            while True:
                link.health()
                now = time.monotonic()
                act = self.inputs.get(0.003)
                if act and state['more'] is not None:
                    more_key(act, now)                    # the panel owns the keys while it is open
                    act = None
                if act == 'back':
                    return 'stop', pos_ms()
                if act in ('left', 'right', 'pgup', 'pgdn'):
                    # Taps accumulate; one real seek happens after SEEK_SETTLE of no further presses.
                    d = {'left': -SEEK_SMALL, 'right': SEEK_SMALL, 'pgup': -SEEK_BIG, 'pgdn': SEEK_BIG}[act]
                    basepos = state['pending'] if state['pending'] is not None else pos_ms()
                    target = max(0, basepos + d * 1000)
                    if dur_ms:
                        target = min(target, max(0, dur_ms - 2000))
                    state.update(pending=target, pending_at=now, osd_until=now + OSD_SECONDS,
                                 note=('>> ' if d > 0 else '<< ') + ui.mmss(target))
                elif act == 'ok':
                    state['paused'] = not state['paused']
                    state['pause_pushed'] = False
                    state['osd_until'] = now + OSD_SECONDS
                elif act == 'alt':
                    added = self.store.toggle_later(v)
                    state['note'] = 'Saved' if added else 'Removed'
                    state['osd_until'] = now + OSD_SECONDS
                elif act == 'menu':                       # X / Tab: the More panel
                    state.update(more={'sel': 0}, osd_until=now + OSD_SECONDS, pause_pushed=False)
                elif act:
                    state['osd_until'] = now + OSD_SECONDS
                if state['pending'] is not None and now - state['pending_at'] > SEEK_SETTLE:
                    target, state['pending'] = state['pending'], None
                    pipe.close(wait=False)                  # do not wait for the old ffmpeg to exit
                    pipe = open_pipe(target)
                    state.update(paused=False, pause_pushed=False, seeking=True,
                                 note='Applying crop...' if state['crop_change'] else 'Seeking...',
                                 crop_change=False, osd_until=now + OSD_SECONDS + 3)
                    if state['last'] is not None and link.queued < SLOTS:   # immediate on-screen feedback
                        link.push(self._osd(state['last'], v, target, dur_ms, False, state, prof, silent=True))
                # Is the ARM keeping up? The FPGA queue only runs dry when the decoder falls behind.
                dt, state['tick'] = now - state['tick'], now
                if (may_fall_back and not state['paused'] and not state['seeking'] and state['pending'] is None
                        and state['last'] is not None and now - state['opened_at'] > STARVE_GRACE
                        and link.queued == 0):
                    state['starved'] += dt
                if (may_fall_back and not state['smooth_noted'] and state['starved'] == 0.0
                        and now - state['opened_at'] > SMOOTH_AFTER):
                    state['smooth_noted'] = True
                    self._note_smooth(media)
                if now - state['win_start'] > STARVE_WINDOW:
                    if may_fall_back and state['starved'] > STARVE_LIMIT:
                        self.log('Starved %.1fs of the last %.0fs at %s: stepping down' %
                                 (state['starved'], now - state['win_start'], media.tier))
                        return 'downgrade', pos_ms()
                    state['starved'], state['win_start'] = 0.0, now
                if now >= next_save:
                    self.store.record_play(v, pos_ms(), dur_ms, force=False)
                    next_save = now + 10
                if link.queued >= SLOTS:
                    time.sleep(.002)
                    continue
                if state['paused']:
                    if not state['pause_pushed'] and state['last'] is not None:
                        link.push(self._osd(state['last'], v, pos_ms(), dur_ms, True, state, prof, silent=True))
                        state['pause_pushed'] = True
                    else:
                        time.sleep(.01)
                    continue
                try:
                    kind, rec = pipe.poll()
                except Exception as exc:
                    msg = str(exc)
                    if '403' in msg and state['retries'] < 3:      # YouTube sometimes refuses at random: try again
                        state['retries'] += 1
                        self.log('YouTube refused the stream (403); retry %d of 3' % state['retries'])
                        pipe.close(wait=False)
                        time.sleep(0.6)
                        pipe = open_pipe(pos_ms())
                        state.update(seeking=True, note='Retrying...', osd_until=now + OSD_SECONDS + 3)
                        continue
                    if '403' in msg and can_refresh:
                        self.log('403 persists; asking YouTube for fresh stream URLs')
                        return 'refresh', pos_ms()
                    raise pipeline.PlaybackError(msg)
                if kind == 'wait':
                    time.sleep(.002)
                    continue
                if kind == 'eof':
                    return 'eof', dur_ms
                state['last'] = rec
                state['retries'] = 0
                if state.get('opened_at') and not state.get('first_logged'):
                    state['first_logged'] = True
                    self.log('First picture %.1f s after starting the decoders' % (time.monotonic() - state['opened_at']))
                if state['seeking']:                         # first picture from the new position
                    state.update(seeking=False, note='', osd_until=now + 1.5)
                if now < state['osd_until'] or state['more'] is not None:
                    shown = state['pending'] if state['pending'] is not None else pos_ms()
                    rec = self._osd(rec, v, shown, dur_ms, False, state, prof)
                elif state['note']:
                    state['note'] = ''
                link.push(rec)
        finally:
            pipe.close()

    def _osd(self, rec, v, pos, dur, paused, state, prof, silent=False):
        """Return `rec` with a title/progress bar burned into its picture."""
        buf = bytearray(rec)
        if silent:
            buf[prof.video_bytes:] = bytes(prof.audio_bytes)
        c = Canvas(320, 240, buf, 0)
        x, y, w = 12, 182, 296
        c.rect(x, y, w, 44, ui.BLACK)
        c.frame(x, y, w, 44, ui.RED, 2)
        c.text(x + 8, y + 6, ui.clip(v.get('title', ''), 35), ui.WHITE, ui.BLACK)
        bx, bw = x + 8, w - 16
        c.rect(bx, y + 19, bw, 6, ui.PANEL)
        if dur:
            c.rect(bx, y + 19, max(2, bw * min(pos, dur) // dur), 6, ui.RED)
        if paused:
            c.rect(bx, y + 29, 4, 10, ui.WHITE)
            c.rect(bx + 8, y + 29, 4, 10, ui.WHITE)
        else:
            for i in range(5):
                c.rect(bx + i * 2, y + 29 + i, 2, 10 - 2 * i, ui.WHITE)
        label = '%s / %s  %s' % (ui.mmss(pos), ui.mmss(dur) if dur else '--:--', state.get('tier', ''))
        c.text(bx + 20, y + 30, label, ui.TEXT, ui.BLACK)
        right = state['note'] or 'X:More'
        c.text(x + w - 8 - len(right) * 8, y + 30, right, ui.AMBER if state['note'] else ui.DIM, ui.BLACK)
        m = state.get('more')
        if m is not None:                                   # the More panel, over the picture
            px, py, pw, ph = 56, 44, 208, 86
            c.rect(px, py, pw, ph, ui.BLACK)
            c.frame(px, py, pw, ph, ui.RED, 2)
            c.text(px + 10, py + 7, 'More', ui.WHITE, ui.BLACK)
            rows = ['Crop: ' + state['crop'],
                    'Watch Later: ' + ('Saved' if self.store.in_later(v['id']) else 'Add'),
                    'Close']
            for i, text in enumerate(rows):
                bg = ui.SEL if i == m['sel'] else ui.BLACK
                yy = py + 22 + i * 16
                c.rect(px + 4, yy, pw - 8, 14, bg)
                c.text(px + 10, yy + 3, text, ui.WHITE if i == m['sel'] else ui.TEXT, bg)
            c.text(px + 10, py + 74, 'Left/Right   B:Back', ui.DIM, ui.BLACK)
        return bytes(buf)
