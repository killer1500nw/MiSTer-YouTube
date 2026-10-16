"""The on-screen app: menus and playback share one streaming session, so the
GUI is simply more 320x240 frames pushed through the same DDR mailbox."""
import subprocess
import threading
import re
import time

from . import cpu, feeds as feedsmod, pipeline, profiles, ui, urls, yuv
from .core_link import SLOTS
from .gfx import Canvas, YuvCanvas
from .store import RESUME_MIN_MS

TEST_CLIP = 'M7kB0lis3xg'
CPU_QUICK_SECONDS = 4.0        # how often the decoder's CPU use is read for the 'is the decoder the limit' decision (cheap: /proc)
CPU_LOG_SECONDS = 30.0         # how often playback logs the CPU split between the player and the decoders
MULT_STEP = 1.08                # how much more pessimistic every stream's estimate gets after one that starved (each video also
                               # remembers its own lighter stream, so the general correction can be small)
STALL_RESTARTS = 3             # how often a data stall reopens the decoders before Auto gives in and steps down
BUSY_PUSH = 0.85               # share of a starve window the loop spent copying pictures that means 'the copy is the limit'
BOOST_MIN_CPU = 85.0           # percent of one core ffmpeg must be using for a starve on Fast to mean 'the decoder is the limit'
EARLY_NUDGES_MS = (1200, 3000, -2000, 6000)   # where to try again, relative to the start, when the decoder crashes before the first picture
QUALITY_SETTLE = 1.2        # seconds after the last Quality press in the More panel before the picture restarts
COPY_BUSY_FB = 2.5            # the framebuffer copy: 0.75 ms idle; under load it slows down too, but far less than the uncached region
COPY_BUSY_PARALLEL = 2.2      # the two-core copy slows down less under load than the single-core one, but still does
COPY_BUSY_CACHED = 1.5        # the same for the cached copy, which does not stall the CPU on every store
COPY_BUSY = 3.2            # copying while ffmpeg decodes took about 23 ms against 7 ms on an idle MiSTer (corrected by _learn_copy)
# The core starts consuming only once 4 records are queued (PROTOCOL.md prefill),
# so the menu must queue at least 4 or it never appears. ~130 ms of latency.
MENU_DEPTH = 4
SEEK_SMALL, SEEK_BIG = 10, 60  # seconds
SMOOTH_AFTER = 90.0            # seconds of clean playback of a demanding stream before Auto trusts its estimate more
STARVE_GRACE = 8.0             # seconds after (re)starting before starvation counts
STARVE_WINDOW = 12.0           # judge starvation over windows of this length
DRY_LIMIT = 4                  # dropouts (the queue running empty) within a window that also mean 'too slow': each one is a gap in the audio
STARVE_LIMIT = 1.2             # seconds of an empty queue within a window that means 'too slow' (visible glitching starts well below 2.5)
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
        getattr(link, 'use_copy_method', lambda m: None)(store.settings.get('copy_method'))      # the way the Speed test found fastest
        if store.settings.get('decode_threads'):
            pipeline.DECODE_THREADS = int(store.settings['decode_threads'])        # and the decoder thread count
        self.play_quality = 'Auto'                                    # the quality of the video being played (the More panel can change it)
        self.h264 = getattr(link, 'kind', 'rgb') == 'h264'            # the H264 core: the FPGA decodes, the GUI is a framebuffer overlay
        self.yuv = getattr(link, 'kind', 'rgb') in ('yuv', 'h264')    # an overlay core: the GUI is a YUV overlay (the H264 link converts it)
        self.canvas = YuvCanvas(320, 240) if self.yuv else Canvas(320, 240)
        self.stack = []
        self.dirty = True
        self.record = None
        self.silence = bytes(self.profile().audio_bytes)
        self.menu_ov = None                                           # overlay buffer holding the current menu screen
        self.osd_ov, self.osd_sig, self.osd_canvas = None, None, None  # and the one holding the playback bar
        self.black = bytearray(b'\x10' * 256 + b'\x80' * 128)         # a 16x16 black picture: the menu's video
        if self.yuv:
            self.feeds.thumb_format = 'yuv420p'
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
        if self.yuv:
            idx = self.link.free_overlay(keep=self.menu_ov)
            if idx is None:                                # every overlay buffer is still on its way to the screen: retry
                self.dirty = True
                return
            self.link.upload_overlay(idx, self.canvas.y, self.canvas.u, self.canvas.v)
            self.menu_ov, self.record = idx, True
        else:
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
            if self.yuv:
                self.link.push_picture(self.black, self.silence, 16, 16, 'Off', 0, self.menu_ov)
            else:
                self.link.push(self.record)
        return act

    def loading_frame(self):
        """Keep the screen alive while a video starts: the menu loop's redraw and feeding, without its input or health checks."""
        self.tick_ui()
        if self.dirty or self.record is None:
            self.render()
        if self.link.queued < MENU_DEPTH:
            if self.yuv:
                self.link.push_picture(self.black, self.silence, 16, 16, 'Off', 0, self.menu_ov)
            else:
                self.link.push(self.record)

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
        if name == 'Search':
            self.push(ui.TextInput('Search', 'Search YouTube for...', self._search_typed, spaces=True))
        elif name == 'Search Playlists':
            self.push(ui.TextInput('Search Playlists', 'Search YouTube playlists for...', self._playlist_search_typed, spaces=True))
        elif name == 'Subscriptions':
            self.push(ui.VideoList('Subscriptions', lambda a: a.feeds.feed(a.store.subscriptions(), shorts(a)), autoplay=True,
                                   empty=lambda a: a.no_subscriptions_text() if not a.store.subscriptions()
                                   else ['Fetching videos...' if a.feeds_busy() else 'No videos found.'],
                                   status=lambda a: 'updating' if a.feeds_busy() else ''))
        elif name == 'Channels':
            self.push(ui.ChannelList())
        elif name == 'Watch Later':
            self.push(ui.VideoList('Watch Later', lambda a: a.store.later, autoplay=True,
                                   hints='A:Play  Y:Remove  B:Back',
                                   empty=lambda a: ['Nothing saved yet.', 'Press Y on any video to save it.']))
        elif name == 'History':
            self.push(ui.VideoList('History', lambda a: a.store.history_list(),
                                   empty=lambda a: ['No watch history yet.']))
        elif name == 'Playlists':
            self.push(ui.PlaylistList())
        elif name == 'My Videos':
            self.push(ui.VideoList('My Videos', lambda a: a.feeds.listed_videos(a.store.video_list()),
                                   hints='A:Play X:Refresh Y:Remove B:Back', empty=lambda a: a.no_lists_text('video'),
                                   refresh=lambda a: a.refresh_lists(True),
                                   remove=lambda a, v: a.confirm_remove('video', v.get('title') or v['id'], [v['id']])))
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

    # search -----------------------------------------------------------
    SEARCH_PAGE = 20
    SEARCH_MAX = 200

    def _search_typed(self, app, text):
        """TextInput callback: search YouTube. Returns an error message to show, or None when handled."""
        query = ' '.join(text.split())
        if len(query) < 2:
            return 'Type something to search for.'
        self.pop()
        self.start_search(query)
        return None

    def start_search(self, query):
        self.feeds.hold.set()                                    # the ARM is the search's until it is done
        pipeline.pause_background()
        started = time.monotonic()
        try:
            res, err = self.run_job(
                'Searching',
                lambda: [ui.clip(query, 33), 'Asking YouTube... %ds' % (time.monotonic() - started),
                         'This can take a minute. Please be patient.'],
                lambda check: pipeline.search_entries(self.rt, query, check, self.log, limit=self.SEARCH_PAGE))
        finally:
            self.feeds.hold.clear()
            pipeline.resume_background()
        if err:
            if str(err) != 'Cancelled':
                self.log('Search failed: ' + str(err)[:300])
                self.push(ui.TextView('Search failed', ['!' + l for l in ui.wrap(str(err), 37, 10)]))
            return
        state = {'query': query, 'videos': list(res[1]), 'next': 1 + getattr(res[1], 'raw', len(res[1])),
                 'more': getattr(res[1], 'raw', len(res[1])) >= self.SEARCH_PAGE, 'loading': False}
        self.search = state
        shorts = lambda a: a.store.settings['shorts'] == 'On'
        self.push(ui.VideoList('Search: ' + ui.clip(query, 22),
                               lambda a: [v for v in state['videos'] if shorts(a) or not v.get('short')],
                               empty=lambda a: ['No results for "%s".' % ui.clip(query, 20)],
                               more=lambda a: self.search_more(state),
                               can_more=lambda a: state['more'] and len(state['videos']) < self.SEARCH_MAX,
                               loading=lambda a: state['loading']))

    # playlist search ---------------------------------------------------
    def _playlist_search_typed(self, app, text):
        query = ' '.join(text.split())
        if len(query) < 2:
            return 'Type something to search for.'
        self.pop()
        self.start_playlist_search(query)
        return None

    def start_playlist_search(self, query):
        self.feeds.hold.set()
        pipeline.pause_background()
        started = time.monotonic()
        try:
            res, err = self.run_job(
                'Searching playlists',
                lambda: [ui.clip(query, 33), 'Asking YouTube... %ds' % (time.monotonic() - started),
                         'This can take a minute. Please be patient.'],
                lambda check: pipeline.search_playlists(self.rt, query, check, self.log, limit=self.SEARCH_PAGE))
        finally:
            self.feeds.hold.clear()
            pipeline.resume_background()
        if err:
            if str(err) != 'Cancelled':
                self.log('Playlist search failed: ' + str(err)[:300])
                self.push(ui.TextView('Search failed', ['!' + l for l in ui.wrap(str(err), 37, 10)]))
            return
        self.log('Playlist search "%s": %d playlists' % (query[:40], len(res)))
        raw = getattr(res, 'raw', len(res))
        state = {'query': query, 'results': list(res), 'next': 1 + raw, 'more': len(res) >= self.SEARCH_PAGE // 2, 'loading': False}
        self.push(ui.FoundPlaylists(query, state['results'], more=lambda a: self._playlist_search_more(state),
                                    can_more=lambda a: state['more'] and len(state['results']) < self.SEARCH_MAX,
                                    loading=lambda a: state['loading']))

    def _playlist_search_more(self, state):
        """"Load more playlists": the next page of this playlist search, in the background."""
        if state['loading']:
            return
        state['loading'] = True
        self.toast('Loading more playlists...')

        def run():
            try:
                self.feeds.wait_turn()
                page = pipeline.search_playlists(self.rt, state['query'], lambda: None, self.log,
                                                 limit=self.SEARCH_PAGE, start=state['next'])
                seen = {r['id'] for r in state['results']}
                new = [r for r in page if r['id'] not in seen]
                state['results'].extend(new)
                state['next'] += getattr(page, 'raw', len(page))
                state['more'] = bool(new) and getattr(page, 'raw', len(page)) >= self.SEARCH_PAGE // 2
            except Exception as exc:
                self.log('Playlist search: could not load more: ' + str(exc)[:200])
            finally:
                state['loading'] = False
                self.dirty = True
        threading.Thread(target=run, daemon=True).start()

    def open_found_playlist(self, row):
        """Load the first videos of a found playlist and show them like any other list (paged, up to a few hundred)."""
        pid, name = row['id'], row['title']
        started = time.monotonic()
        self.feeds.hold.set()
        pipeline.pause_background()
        try:
            res, err = self.run_job(
                'Opening playlist',
                lambda: [ui.clip(name, 33), 'Asking YouTube... %ds' % (time.monotonic() - started)],
                lambda check: pipeline.playlist_entries(self.rt, pid, check, self.log, limit=self.SEARCH_PAGE, background=False))
        finally:
            self.feeds.hold.clear()
            pipeline.resume_background()
        if err:
            if str(err) != 'Cancelled':
                self.log('Playlist %s failed: %s' % (pid, str(err)[:300]))
                self.push(ui.TextView('Could not open playlist', ['!' + l for l in ui.wrap(str(err), 37, 10)]))
            return
        vids = res[1]
        raw = getattr(vids, 'raw', len(vids))
        state = {'id': pid, 'videos': list(vids), 'next': 1 + raw, 'more': raw >= self.SEARCH_PAGE, 'loading': False}
        shorts = lambda a: a.store.settings['shorts'] == 'On'
        self.push(ui.VideoList(ui.clip(name, 30), lambda a: [v for v in state['videos'] if shorts(a) or not v.get('short')], autoplay=True,
                               hints='A:Play  Y:Later  B:Back', empty=lambda a: ['This playlist has no videos.'],
                               more=lambda a: self._found_playlist_more(state),
                               can_more=lambda a: state['more'] and len(state['videos']) < self.SEARCH_MAX,
                               loading=lambda a: state['loading']))

    def _found_playlist_more(self, state):
        if state['loading']:
            return
        state['loading'] = True
        self.toast('Loading more videos...')

        def run():
            try:
                self.feeds.wait_turn()
                _, vids = pipeline.playlist_entries(self.rt, state['id'], lambda: None, self.log,
                                                    limit=self.SEARCH_PAGE, start=state['next'], background=False)
                raw = getattr(vids, 'raw', len(vids))
                seen = {v['id'] for v in state['videos']}
                state['videos'].extend(v for v in vids if v['id'] not in seen)
                state['next'] += raw
                state['more'] = raw >= self.SEARCH_PAGE
            except Exception as exc:
                self.log('Playlist: could not load more: ' + str(exc)[:200])
            finally:
                state['loading'] = False
                self.dirty = True
        threading.Thread(target=run, daemon=True).start()

    def save_found_playlist(self, row):
        added = self.store.add_entry('playlist', row['id'])
        self.log('Playlist %s %s' % (row['id'], 'saved' if added else 'already saved'))
        self.toast('Saved to Playlists' if added else 'Already in Playlists')
        if added:
            self.refresh_lists(force=True)

    def search_more(self, state):
        """"Load more videos": the next page of this search, in the background (frozen while a video plays)."""
        if state['loading']:
            return
        state['loading'] = True
        self.toast('Loading more results...')

        def run():
            try:
                self.feeds.wait_turn()
                _, vids = pipeline.search_entries(self.rt, state['query'], lambda: None, self.log,
                                                  limit=self.SEARCH_PAGE, start=state['next'])
                raw = getattr(vids, 'raw', len(vids))
                seen = {v['id'] for v in state['videos']}
                state['videos'].extend(v for v in vids if v['id'] not in seen)
                state['next'] += raw
                state['more'] = raw >= self.SEARCH_PAGE
            except Exception as exc:
                self.log('Search: could not load more: ' + str(exc)[:200])
            finally:
                state['loading'] = False
                self.dirty = True
        threading.Thread(target=run, daemon=True).start()

    def confirm_remove_channel(self, row_id, name):
        """Y on Channels: the file may list the channel by @handle or by ID; remove whichever line(s) name it."""
        refs = [r for _, r in self.store.subscriptions() if r == row_id or self.feeds.handles.get(r) == row_id]
        self.confirm_remove('channel', name, refs or [row_id])

    def confirm_remove(self, kind, name, refs):
        self.push(ui.Choice('Remove', 'Remove "%s" from your lists?' % ui.clip(str(name), 26), ['Remove', 'Cancel'],
                            lambda i: self._remove(kind, name, refs) if i == 0 else None))

    def _remove(self, kind, name, refs):
        n = self.store.remove_entry(kind, refs)
        self.log('Removed %s %s (%d line%s)' % (kind, ','.join(refs), n, '' if n == 1 else 's'))
        self.toast('Removed' if n else 'Not found in the file')
        self.dirty = True

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
        """The Speed test. Background lookups are frozen meanwhile: they would steal the ARM and spoil the numbers."""
        self.feeds.hold.set()
        pipeline.pause_background()
        try:
            self._run_benchmark()
        finally:
            self.feeds.hold.clear()
            pipeline.resume_background()

    def _run_benchmark(self):
        """Measure decoding on this MiSTer with both cheap decoder settings and calibrate the Auto cost model."""
        if self.missing_tools():
            return
        prof = self.profile()
        # Test clips: your newest videos first (a realistic load), then a known good one. Livestreams and
        # other videos that cannot be decoded are skipped instead of failing the whole test.
        again = self.store.settings.get('speed_clip')
        candidates = ([again] if again else []) + [v['id'] for v in self.feeds.feed(self.store.subscriptions(), False, 3)] + [TEST_CLIP]
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
            for attempt in range(3):
                try:
                    tgt = pipeline.yuv_target(media, self.link.interlaced, self.big_pictures()) if self.yuv else None    # the pipeline the core really gets
                    runs = [(sp, pipeline.benchmark(self.rt, prof, media, sp, 12, check, target=tgt)) for sp in profiles.SPEEDS]
                    best_sp, best_x = 'Fast', dict(runs)['Fast']      # Auto is sized for Fast: Fastest drops the deblocking filter completely (blocky), so it is never relied on
                    # The same setting again with only the decoder (no scaling, no RGB565 conversion): the difference is the
                    # share of the ARM's time that an FPGA scaler could take over.
                    bare = pipeline.benchmark(self.rt, prof, media, best_sp, 12, check, decode_only=True)
                    break
                except RuntimeError as exc:
                    # YouTube sometimes refuses the saved stream URLs (403) on the first try: ask for fresh ones, like playback does.
                    if attempt == 2 or not re.search(r'403|forbidden|access denied|ffmpeg failed', str(exc), re.I):
                        raise
                    self.log('Speed test: the stream was refused (%s); getting fresh stream details' % ' '.join(str(exc).split())[:100])
                    pipeline.forget(self.rt, media.id)
                    media = pipeline.resolve(self.rt, prof, media.id, check, self.log, quality='360p')
            # How far the decoder alone gets with the next size up (the core now does the scaling): tells whether a
            # bigger source is worth offering. Optional: a video without a bigger H.264 stream just skips it.
            def limited():
                """The optional probes get about a minute each: a slow decoder must not hold up the whole test."""
                check()
                if time.monotonic() > deadline[0]:
                    raise RuntimeError('probe took too long')
            big = None
            deadline = [time.monotonic() + 75]
            try:
                bigger = pipeline.resolve(self.rt, prof, media.id, check, self.log, quality='480p')
                bv, mv = bigger.video, media.video
                if bv['width'] * bv['height'] > mv['width'] * mv['height'] * 1.2:
                    big = (bigger, pipeline.benchmark(self.rt, prof, bigger, best_sp, 8, limited, decode_only=True))
            except (ValueError, RuntimeError, OSError) as exc:
                self.log('Speed test: no larger stream measured (%s)' % exc)
            # VP9 at the same size (it looks cleaner than H.264 at the same bitrate; can the ARM decode it in time?). AV1 was
            # tried and is far too slow on this ARM, so it is no longer measured.
            threads = {}
            if big:                                                  # does a different number of decoder threads help the bigger stream?
                for n in (1, 3, 4):
                    deadline[0] = time.monotonic() + 60
                    try:
                        threads[n] = pipeline.benchmark(self.rt, prof, big[0], best_sp, 8, limited, decode_only=True, threads=n)
                    except (ValueError, RuntimeError, OSError, TypeError) as exc:
                        self.log('Speed test: %d decoder threads not measured (%s)' % (n, exc))
                        break
                threads[2] = big[1]
                # The same stream through the pipeline playback really uses (fps filter, the pre-scale to 640 wide when the stream is
                # wider, the pipe format): if that is much slower than decode only, the filter chain is the limit, not the decoder.
                try:
                    deadline[0] = time.monotonic() + 60
                    tgt_big = pipeline.yuv_target(big[0], self.link.interlaced, self.big_pictures())
                    full_big = pipeline.benchmark(self.rt, prof, big[0], best_sp, 8, limited, target=tgt_big)
                    self.log('Full playback pipeline on the bigger stream (%dx%d, pre-scale %s): %.2fx against %.2fx decode only' % (
                        big[0].video['width'], big[0].video['height'], tgt_big.get('prescale') or 'none', full_big, big[1]))
                    deadline[0] = time.monotonic() + 60
                    via_pipe = pipeline.pipe_benchmark(self.rt, prof, big[0], best_sp, 8, limited, target=tgt_big)
                    self.log('Through the pipe to the player (no core): %s against %.2fx for ffmpeg alone' % (
                        ('%.2fx' % via_pipe) if via_pipe else 'too short to tell', full_big))
                except (ValueError, RuntimeError, OSError, TypeError, KeyError) as exc:
                    self.log('Speed test: full pipeline on the bigger stream not measured (%s)' % exc)
            av1 = None
            deadline[0] = time.monotonic() + 75
            try:
                a = pipeline.media_from(media.catalog[0], media.catalog[1], prof, '360p', log=self.log, codec='vp09')
                av1 = (a, pipeline.benchmark(self.rt, prof, a, best_sp, 8, limited, decode_only=True))
            except (ValueError, RuntimeError, OSError, TypeError) as exc:
                self.log('Speed test: VP9 not measured (%s)' % exc)
            return runs, media, bare, big, av1, threads
        res, err = self.run_job('Speed test', ['Decoding a test stream (three runs)...', 'B: cancel'], work)
        if err:
            self.push(ui.Message('Speed test failed', [str(err)[:140]], ui.AMBER))
            return
        results, media, bare, big, av1, threads = res
        copy_avg, copy_max = self.link.measure_copy()       # on this thread: nothing else is writing the next slot
        fast = self.link.measure_fast_copy()
        try:
            fb = self.link.measure_fb_copy() if hasattr(self.link, 'measure_fb_copy') else None
        except Exception:                                    # an experiment: whatever goes wrong, the Speed test carries on
            fb = None
        best_speed, best = 'Fast', dict(results)['Fast']
        v = media.video
        lines = ['  '.join('%s %.2fx' % (sp, f) for sp, f in results)]
        t_full, t_bare = 1.0 / best, 1.0 / bare
        share = max(0.0, (t_full - t_bare) / t_full)           # fraction of the ARM's time spent scaling and converting colour
        lines.append('Decode only: %.2fx' % bare)
        lines.append('Scale+colour: %d%% of the time' % round(share * 100))
        if big:
            lines.append('Decode only at %s (%d kbps): %.2fx' % (big[0].label, round(big[0].tbr), big[1]))
        if av1:
            lines.append('VP9 %s (%d kbps), decode only: %.2fx (H.264 %.2fx)' % (av1[0].label, round(av1[0].tbr), av1[1], bare))
        kb = max(1, round(self.link.copy_bytes / 1000.0))
        lines.append('Copy %d KB: %.1f ms (max %.1f)%s' % (kb, copy_avg, copy_max, (', ' + ', '.join(
            '%s %.1f ms' % (m, r[0]) for m, r in sorted(self.link.copy_report.items()))) if len(self.link.copy_report) > 1 else ''))
        if fast:
            method, (bulk_ms, _, intact) = min(fast.items(), key=lambda kv: kv[1][0])
            lines.append('Bulk copy: %.1f ms (%s%s)' % (bulk_ms, method, '' if intact else ' BAD DATA'))
        else:
            lines.append('Bulk copy: not possible')
        if fb:
            lines.append('Framebuffer: ' + fb[:34])
        lines.append('Test: %s, %d kbps, %d fps' % (media.label, round(media.tbr), round(v.get('fps') or 30)))
        cal = {'px': v['width'] * v['height'], 'tbr': float(v.get('tbr') or 300),
               'fps': float(v.get('fps') or 30), 'cost': round(1.0 / best, 4)}
        if self.link.copy_bytes and copy_avg > 0:
            # Copying while ffmpeg is decoding is slower than on an idle MiSTer; start with twice the idle time and let
            # the real figures from playback correct it (see _learn_copy).
            busy = {'memmove': COPY_BUSY, 'parallel': COPY_BUSY_PARALLEL, 'fb': COPY_BUSY_FB}.get(getattr(self.link.box, 'method', 'memmove'), COPY_BUSY_CACHED)
            cal.update({'copy_ms': round(copy_avg * busy, 2), 'copy_bytes': self.link.copy_bytes,
                        'copy_fixed': self.link.kind != 'yuv'})
        self.store.set('cal', cal)
        self.store.set('copy_method', getattr(self.link.box, 'method', None) if self.link.kind == 'yuv' else None)
        self.store.set('speed_big', best_speed)
        self.store.set('speed_x', {sp: round(x, 3) for sp, x in results})
        self.store.set('cap_ids', {})                       # what was remembered about single videos belongs to the old measurement
        self.store.set('fastest_ids', [])
        if len(threads) > 1:
            best_n = max(threads, key=lambda n: threads[n])
            if threads[best_n] > 1.08 * threads[2]:                  # only a clear win changes it
                self.store.set('decode_threads', best_n)
                pipeline.DECODE_THREADS = best_n
            elif self.store.settings.get('decode_threads'):
                self.store.set('decode_threads', None)
                pipeline.DECODE_THREADS = 2
            self.log('Decoder threads on the bigger stream (decode only): ' + ', '.join(
                '%d: %.2fx' % (n, threads[n]) for n in sorted(threads)) + '; using %d' % pipeline.DECODE_THREADS)
        self.store.set('speed_clip', media.id)
        self.store.set('cost_mult', 1.0)
        extra = ''
        if big:
            extra += '; %s %dx%d %d kbps %d fps decode only %.2fx' % (
                big[0].label, big[0].video['width'], big[0].video['height'], round(big[0].tbr), round(big[0].video.get('fps') or 30), big[1])
        if av1:
            extra += '; VP9 %dx%d %d kbps %d fps decode only %.2fx (H.264 %.2fx at %d kbps)' % (
                av1[0].video['width'], av1[0].video['height'], round(av1[0].tbr), round(av1[0].video.get('fps') or 30), av1[1], bare, round(media.tbr))
        if fb:
            self.log('Framebuffer probe: ' + fb)
        self.log('Speed test %s: %s; decode only %.2fx (scaling+colour = %d%% of the time); copy %d KB %.1f ms (worst %.1f) %s; bulk copy %s; calibration %s, speed_big=%s%s' % (
            media.label, ', '.join('%s %.2fx' % r for r in results), bare, round(share * 100), kb, copy_avg, copy_max,
            {m: round(r[0], 1) for m, r in self.link.copy_report.items()}, fast, self.store.settings['cal'], best_speed, extra))
        self.push(ui.Message('Speed test', lines, ui.GREEN if best >= 1.15 else ui.AMBER))

    # playback ---------------------------------------------------------
    def big_pictures(self):
        """Can pictures wider than 640 go to the core as they are (v0.16 core reading from the framebuffer)? The FPGA then shrinks them."""
        return bool(getattr(getattr(self.link, 'box', None), 'fb_active', False) and getattr(self.link, 'big_core', False))

    def calibration(self):
        """(cal, mult): the measured reference stream and the learned correction."""
        cal = self.store.settings.get('cal')
        if not (isinstance(cal, dict) and cal.get('cost') and cal.get('px')):
            cal = profiles.CAL_DEFAULT
        if self.big_pictures():
            cal = dict(cal, big=True)                  # copy costs follow the real picture size, and no pre-scale is needed
        if self.yuv:
            cal = dict(cal, crop=self.store.settings.get('crop', 'Off'))     # ffmpeg trims the sides, so the pictures are smaller
        return cal, float(self.store.settings.get('cost_mult', 1.0))

    def effective_speed(self, media, speed):
        """Decoder setting for this stream: big pictures use what the Speed test learned."""
        v = media.video or {}
        if speed == 'Normal':                          # automatic: the gentlest setting that keeps up (best picture)
            cal, mult = self.calibration()
            pick = profiles.setting_for(v, cal, mult, self.store.settings.get('speed_x')) if v else None
            if pick:
                return pick
        return profiles.speed_for((v.get('width') or 0) * (v.get('height') or 0), speed,
                                  self.store.settings.get('speed_big'))

    def start_video(self, v, queue=None):
        """`queue`: a function returning the videos of the list this one was chosen from; the next one plays when this one ends."""
        pos = self.store.saved_pos(v['id'])
        if pos >= RESUME_MIN_MS and self.store.settings['resume'] == 'Ask':
            self.push(ui.Choice('Resume', v.get('title', ''),
                                ['Resume at ' + ui.mmss(pos), 'Start over'],
                                lambda i: self.play(v, pos if i == 0 else 0, queue)))
        else:
            self.play(v, pos if pos >= RESUME_MIN_MS else 0, queue)

    def _probe_audio(self, media, prof, at_ms):
        """After the audio decoder crashed at start: run it once more, verbosely, for two seconds and log what it says. A refused or
        reset connection normally prints a line before the crash that the normal run (errors only) does not show."""
        try:
            if not media.audio:
                return
            cmd = pipeline.decoder_command(self.rt, prof, media.audio, 'audio', start_s=at_ms / 1000.0, limit_s=2, to_null=True)
            cmd[cmd.index('-loglevel') + 1] = 'verbose'
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            tail = pipeline.safe_text(done.stderr).splitlines()[-14:]
            self.log('Audio probe (exit %s): %s' % (done.returncode, ' | '.join(l.strip()[:110] for l in tail if l.strip())))
        except Exception as exc:                          # a diagnostic must never take playback down with it
            self.log('Audio probe failed: %s' % exc)

    def _log_crash_clues(self):
        """After a decoder crash at start: what the kernel says about it (a segfault line or an out-of-memory kill) and free memory."""
        try:
            out = subprocess.run(['dmesg'], capture_output=True, text=True, timeout=4).stdout.splitlines()
            hits = [l for l in out[-60:] if re.search(r'segfault|ffmpeg|out of memory|oom|killed process|Killed', l, re.I)]
            self.log('Kernel notes after the crash: %s' % (' | '.join(l[-110:] for l in hits[-4:]) or 'none'))
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            with open('/proc/meminfo', encoding='utf-8') as f:
                mem = dict(l.split(':', 1) for l in f.read().splitlines()[:5])
            self.log('Memory: ' + ', '.join('%s %s' % (k, v.strip()) for k, v in mem.items()))
        except OSError:
            pass

    def _learn_copy(self, media, avg_ms, n):
        """Fold the copy time seen during real playback into the Auto cost model (an idle MiSTer copies faster)."""
        cal = self.store.settings.get('cal')
        if n < 90 or not (isinstance(cal, dict) and cal.get('copy_ms') and cal.get('copy_bytes')) or not media.video:
            return
        per_base = avg_ms if cal.get('copy_fixed') else avg_ms / profiles.copy_scale(media.video, dict(cal, big=self.big_pictures(), crop=self.store.settings.get('crop', 'Off')))
        new = round(0.7 * cal['copy_ms'] + 0.3 * min(per_base, 4.0 * cal['copy_ms']), 2)
        if abs(new - cal['copy_ms']) >= 0.1:
            self.store.set('cal', dict(cal, copy_ms=new))
            self.log('Copy time per picture now estimated at %.1f ms (%d KB)' % (new, cal['copy_bytes'] // 1000))

    def _note_smooth(self, media):
        """A demanding stream ran for a while without starving: trust the cost estimate a little more."""
        if media.cost >= 0.8 * profiles.limit_for(self.calibration()[0]):
            mult = max(0.9, float(self.store.settings.get('cost_mult', 1.0)) * 0.97)      # never more optimistic than 0.9: an empty queue is not the only way to stutter
            self.store.set('cost_mult', round(mult, 3))
            self.log('%s at estimated load %d%% played cleanly; cost multiplier now %.3f' %
                     (media.label, round(media.cost * 100), mult))

    def _resolve(self, v, prof, quality):
        """Ask YouTube (via yt-dlp) for the video, showing progress. -> Media, or None if it failed."""
        started, last = time.monotonic(), ['starting yt-dlp']
        slow = [self.store.is_slow(v['id'])]               # known from an earlier play, or seen during this lookup
        cal, mult = self.calibration()

        def note(text):
            last[0] = text
            if re.search(r'UNPLAYABLE|web embedded|Solving JS', text):
                slow[0] = True
        res, err = self.run_job(
            'Loading',
            lambda: [ui.clip(v.get('title', ''), 33),
                     'Contacting YouTube... %ds' % (time.monotonic() - started), ui.clip(last[0], 33),
                     (('This video is age restricted (child or adult) and will take longer to start. Please be patient.' if getattr(self.rt, 'node', None)
                       else 'This video is age restricted (child or adult) and will take a few minutes to start. Please be patient.') if slow[0] else
                      'This can take a minute or two. Please be patient.')],
            lambda check: pipeline.resolve(self.rt, prof, v['id'], check, self.log,
                                           note=note, quality=quality, cal=cal, mult=mult))
        if err:
            if str(err) != 'Cancelled':
                self.log('Cannot play: ' + ' | '.join(str(err).splitlines())[:600])
                self.push(ui.TextView('Cannot play', ['!' + l for l in ui.wrap(str(err), 37, 14)]))
            return None
        if slow[0] and res is not None:
            self.store.mark_slow(v['id'])                  # warn about it straight away next time
        return res

    def next_in_queue(self, v, queue):
        """The video after `v` in the list it was chosen from, or None."""
        try:
            vids = queue()
        except Exception:
            return None
        for i, x in enumerate(vids):
            if x.get('id') == v['id']:
                return vids[i + 1] if i + 1 < len(vids) else None
        return None

    def play(self, v, start_ms, queue=None):
        """Play a video (and, from a playlist or channel, the ones after it). Background refreshing is paused from here until playback is over."""
        self.feeds.hold.set()
        pipeline.pause_background()                       # freeze any lookup already running
        try:
            while True:
                self.finished = False
                self._play(v, start_ms)
                nxt = self.next_in_queue(v, queue) if (queue and self.finished and self.store.settings.get('autoplay', 'On') == 'On') else None
                if nxt is None:
                    break
                self.log('Playing the next video: %s' % (nxt.get('title') or nxt['id']))
                self.toast('Next: ' + ui.clip(nxt.get('title') or '', 30))
                v, start_ms = nxt, 0
        finally:
            self.feeds.hold.clear()
            pipeline.resume_background()

    def _play(self, v, start_ms):
        if self.missing_tools():
            return
        if self.h264:
            return self._play_h264(v, start_ms)
        prof, speed = self.profile(), self.store.settings['decode_speed']
        q = self.store.settings.get('quality', 'Auto')
        self.play_quality = q
        self.log('Play %s "%s" profile=%s quality=%s speed=%s resume=%dms' %
                 (v['id'], v.get('title', '')[:60], prof.key, q, speed, start_ms))
        media = self._resolve(v, prof, q)
        if media is None:
            return
        if not v.get('title'):
            v['title'] = media.title                      # a typed link has no title until YouTube tells us
        cap = self.store.settings.get('cap_ids', {}).get(v['id'])
        cal0, mult0 = self.calibration()
        if q == 'Auto' and cap and media.catalog and media.cost > cap * mult0 * 1.05:
            # Auto had to step this video down before: start at the stream that worked, so the music does not stop again.
            # (`cap` is that stream's load without the learned multiplier, so the multiplier drifting later does not push it lower.)
            media = pipeline.media_from(media.catalog[0], media.catalog[1], prof, 'Auto', cal0, mult0, self.log, max_cost=cap * mult0 * 1.05)
            self.log('Starting at %s: this video needed a lighter stream before' % media.label)
        refresh_left, pos_ms, outcome = 1, start_ms, 'stop'
        copy0 = (self.link.copy_total, self.link.copy_count, getattr(self.link, 'full_total', 0.0),
                 getattr(self.link, 'full_count', 0), getattr(self.link, 'repeats', 0))
        self.link.copy_max = 0.0
        for parts in (getattr(getattr(self.link, 'box', None), 'parts', None), getattr(self.link, 'parts2', None)):
            if parts:
                parts[:] = [0.0] * len(parts)             # the menu's own pictures must not count towards this video's averages
        boosted = speed == 'Normal' and v['id'] in self.store.settings.get('fastest_ids', [])
        stall_restarts = 0
        while True:
            cal, mult = self.calibration()
            can_step = q == 'Auto' and pipeline.step_down(media, prof, cal, mult) is not None
            level = self.effective_speed(media, speed)
            if boosted and level == 'Fast':
                level = 'Fastest'                         # this clip starved on Fast before: start on the stronger setting
            self.log('Decoder setting %s for %s (estimated load %d%%, copy %.1f ms per picture, copy method %s)' % (
                level, media.label, round(media.cost * 100), (cal.get('copy_ms') or 0.0),
                getattr(getattr(self.link, 'box', None), 'method', '-')))
            try:
                can_boost = speed == 'Normal' and level == 'Fast' and not boosted and (media.video or {}).get('height', 0) >= 400
                outcome, pos_ms = self._play_loop(v, media, prof, level, pos_ms,
                                                  can_step or can_boost, refresh_left > 0)
            except Quit:
                raise
            except pipeline.PlaybackError as exc:
                self.log('Playback error: ' + ' | '.join(str(exc).splitlines())[:600])
                self.push(ui.TextView('Playback stopped', ['!' + l for l in ui.wrap(str(exc), 37, 14)]))
                return
            if outcome == 'downgrade' and can_boost:
                boosted = True                            # first try the stronger decoder setting on the same 480p stream (looks better than 360p)
                ids = [i for i in self.store.settings.get('fastest_ids', []) if i != v['id']] + [v['id']]
                self.store.set('fastest_ids', ids[-60:])
                self.log('Starved on Fast at %s: trying Fastest before a smaller picture' % media.label)
                self.toast('Heavy clip - using the stronger decoder setting')
                continue
            if outcome == 'stall' and stall_restarts < STALL_RESTARTS:
                # The data stopped arriving (twice in a row) while the ARM was not busy. A smaller picture would not bring it
                # back, so open a fresh connection at the same quality and position.
                stall_restarts += 1
                self.log('Reconnecting the decoders at the same quality (%d of %d)' % (stall_restarts, STALL_RESTARTS))
                self.toast('Reconnecting...')
                continue
            if outcome == 'stall' and not can_step:
                continue
            if outcome in ('downgrade', 'crashstep', 'stall'):     # the ARM could not keep up (or the decoder crashes at start): use a lighter stream
                if boosted and outcome == 'downgrade':
                    ids = [i for i in self.store.settings.get('fastest_ids', []) if i != v['id']]
                    self.store.set('fastest_ids', ids)     # even Fastest was not enough: forget it, the smaller picture takes over
                    boosted = False
                if outcome == 'downgrade':                # ...and learn from it; a crash says nothing about the ARM's speed
                    mult = min(3.0, mult * MULT_STEP)
                    self.store.set('cost_mult', round(mult, 3))
                smaller = pipeline.step_down(media, prof, cal, mult, self.log)
                self.log('%s (estimated load %d%%) %s; cost multiplier now %.3f; %s' % (
                    media.label, round(media.cost * 100), {'downgrade': 'could not keep up', 'stall': 'kept waiting for data'}.get(outcome, 'crashed at start'), mult,
                    ('switching to %s' % smaller.label) if smaller else 'nothing lighter to switch to'))
                if smaller is not None:
                    self.toast('Too heavy for %s - using %s' % (media.label, smaller.label))
                    media = smaller
                    if q == 'Auto' and outcome in ('downgrade', 'stall'):
                        caps = dict(self.store.settings.get('cap_ids', {}))
                        caps.pop(v['id'], None)
                        caps[v['id']] = round(smaller.cost / max(mult, 0.1), 4)
                        self.store.set('cap_ids', dict(list(caps.items())[-100:]))
                continue
            if outcome == 'quality':                      # the user changed the quality from the More panel
                q = self.play_quality
                cal, mult = self.calibration()
                try:
                    media = pipeline.media_from(media.catalog[0], media.catalog[1], prof, q, cal, mult, self.log)
                except (ValueError, TypeError, IndexError):
                    fresh = self._resolve(v, prof, q)
                    if fresh is None:
                        return
                    media = fresh
                self.log('Quality set to %s for this video: %s' % (q, media.label))
                self.toast('Quality: %s (%s)' % (q, media.label))
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
        if self.link.copy_count > copy0[1]:
            n = self.link.copy_count - copy0[1]
            avg_ms = 1000.0 * (self.link.copy_total - copy0[0]) / n
            nfull = getattr(self.link, 'full_count', n) - copy0[3]
            full_ms = 1000.0 * (getattr(self.link, 'full_total', self.link.copy_total) - copy0[2]) / nfull if nfull else avg_ms
            again = getattr(self.link, 'repeats', 0) - copy0[4]
            self.log('Record copy to the core: average %.1f ms, worst %.1f ms (%d records%s)' % (
                avg_ms, 1000.0 * self.link.copy_max, n,
                '; %d repeated pictures not copied, %.1f ms per full picture' % (again, full_ms) if again else ''))
            fb = getattr(getattr(self.link, 'box', None), 'fb', None)
            if fb is not None and fb.stats[3] and getattr(self.link.box, 'fb_active', False):
                n_fb = fb.stats[3]
                self.log('Framebuffer write per picture (this session): copy %.1f ms, barrier %.1f ms, read-back check %.1f ms' % (
                    1000 * fb.stats[0] / n_fb, 1000 * fb.stats[1] / n_fb, 1000 * fb.stats[2] / n_fb))
            parts = getattr(getattr(self.link, 'box', None), 'parts', None)
            if parts and parts[3]:
                self.log('A full picture takes %.1f ms: planes %.1f, audio %.1f, header %.1f, everything else %.1f' % (
                    full_ms, 1000 * parts[0] / parts[3], 1000 * parts[1] / parts[3], 1000 * parts[2] / parts[3],
                    max(0.0, full_ms - 1000 * sum(parts[:3]) / parts[3])))
                parts[:] = [0.0, 0.0, 0.0, 0]
                p2 = getattr(self.link, 'parts2', None)
                if p2 and p2[2]:
                    self.log('  ...of "everything else": %.1f ms before the writes (repeat check, header), %.1f ms after them (publishing)' % (
                        1000 * p2[0] / p2[2], 1000 * p2[1] / p2[2]))
                    p2[:] = [0.0, 0.0, 0]
            self._learn_copy(media, full_ms, nfull)
        self.log('Playback %s at %s of %s' % ('finished' if outcome == 'eof' else 'stopped', ui.mmss(pos_ms), ui.mmss(media.duration * 1000)))
        self.store.record_play(v, pos_ms, media.duration * 1000, finished=(outcome == 'eof'))
        self.finished = outcome == 'eof'
        self.dirty = True

    def _play_h264(self, v, start_ms):
        """Play a video on the H264 core: ffmpeg only repackages the H.264 and decodes the audio; the FPGA does the rest."""
        prof = self.profile()
        q = self.store.settings.get('quality', 'Auto')
        self.play_quality = q
        self.log('Play %s "%s" (H264 core) quality=%s resume=%dms' % (v['id'], v.get('title', '')[:60], q, start_ms))
        media = self._resolve(v, prof, q)
        if media is None:
            return
        if not v.get('title'):
            v['title'] = media.title
        self.finished = False
        pos_ms, outcome = start_ms, 'stop'
        refresh_left = 1
        while True:
            try:
                media = pipeline.h264_media(media, q)
            except ValueError as exc:
                self.log('Cannot play on the H264 core: %s' % exc)
                self.push(ui.TextView('Cannot play', ['!' + l for l in ui.wrap(str(exc), 37, 14)]))
                return
            self.log('H264 core: %dx%d %s fps %s kbps (%s)' % (media.video['width'], media.video['height'], media.video.get('fps'),
                                                             round(media.tbr or 0), media.video['format_id']))
            try:
                outcome, pos_ms = self._h264_loop(v, media, pos_ms)
            except Quit:
                raise
            except pipeline.PlaybackError as exc:
                self.log('Playback error: ' + ' | '.join(str(exc).splitlines())[:600])
                self.push(ui.TextView('Playback stopped', ['!' + l for l in ui.wrap(str(exc), 37, 14)]))
                return
            if outcome == 'quality':
                q = self.play_quality
                continue
            if outcome == 'refresh' and refresh_left > 0:
                refresh_left -= 1
                pipeline.forget(self.rt, v['id'])
                fresh = self._resolve(v, prof, q)
                if fresh is None:
                    return
                media = fresh
                continue
            break
        self.log('Playback %s at %s of %s' % ('finished' if outcome == 'eof' else 'stopped', ui.mmss(pos_ms), ui.mmss(media.duration * 1000)))
        self.store.record_play(v, pos_ms, media.duration * 1000, finished=(outcome == 'eof'))
        self.finished = outcome == 'eof'
        self.dirty = True

    def _h264_crop(self, mode):
        """Fill / 14:9 / Off -> the core's Analog Aspect (3 Crop, 2 Letterbox). The core has no 14:9: it shows Fill instead."""
        try:
            self.link.ring.request_settings(analog_aspect=2 if mode == 'Off' else 3)
        except Exception as exc:
            self.log('Could not change the core\'s aspect: %s' % exc)

    def _h264_loop(self, v, media, start_ms):
        """The play loop for the H264 core. -> (outcome, position in ms)."""
        from . import h264feed
        link, ring = self.link, self.link.ring
        dur_ms = media.duration * 1000
        now = time.monotonic()
        state = {'paused': False, 'pending': None, 'pending_at': 0.0, 'seeking': False, 'note': '', 'osd_until': now + OSD_SECONDS,
                 'more': None, 'crop': self.store.settings.get('crops', {}).get(v['id']) or self.store.settings.get('crop', 'Fill'),
                 'quality': self.play_quality, 'tier': media.tier, 'flags': 0, 'quality_req': None, 'quality_at': 0.0}
        self._h264_crop(state['crop'])
        feed = {'feeder': None, 'thread': None, 'stop': None, 'result': None, 'error': None}
        position = {'ms': start_ms}

        def stop_feed():
            if feed['feeder'] is not None:
                feed['stop'].set()
                feed['feeder'].close()
                feed['thread'].join(timeout=5)
                feed['feeder'] = None

        def start_feed(at_ms):
            stop_feed()
            ring.new_session()                                     # drops whatever the core had not read and resets its display
            ring.set_pause(False)
            f = h264feed.Feeder(ring, lambda s: h264feed.video_copy_command(self.rt, media.video, s),
                                (lambda s: h264feed.audio_pcm_command(self.rt, media.audio, s)) if media.audio else None, log=self.log)
            stop = threading.Event()
            feed.update(feeder=f, stop=stop, result=None, error=None)

            def work():
                try:
                    feed['result'] = f.run(stop=stop.is_set, first_timeout=90.0)
                except Exception as exc:
                    feed['error'] = exc
            f.start(at_ms / 1000.0)
            feed['thread'] = threading.Thread(target=work, daemon=True)
            feed['thread'].start()
            position['ms'] = at_ms
            state.update(seeking=True, opened_at=time.monotonic())

        def lines():
            f = feed['feeder']
            tail = (f.errors[-1][1].strip().splitlines() or [''])[-1] if f is not None and f.errors else ''
            return [ui.clip(v.get('title', ''), 33), 'Starting... %ds' % (time.monotonic() - state['opened_at']),
                    '%s, %d kbps (H264 core)' % (media.label, round(media.tbr or 0)), ui.clip(pipeline.safe_text(tail), 33)]

        def set_crop(mode, now):
            if mode == state['crop']:
                return
            state['crop'] = mode
            crops = dict(self.store.settings.get('crops', {}))
            crops.pop(v['id'], None)
            crops[v['id']] = mode
            self.store.set('crops', dict(list(crops.items())[-200:]))
            self._h264_crop(mode)
            state['note'] = 'Crop: ' + mode

        def more_key(act, now):
            m = state['more']
            if act in ('back', 'menu'):
                state['more'] = None
            elif act == 'down':
                m['sel'] = (m['sel'] + 1) % 4
            elif act == 'up':
                m['sel'] = (m['sel'] - 1) % 4
            elif m['sel'] == 0 and act in ('left', 'right', 'ok'):
                step = -1 if act == 'left' else 1
                set_crop(profiles.CROPS[(profiles.CROPS.index(state['crop']) + step) % len(profiles.CROPS)], now)
            elif m['sel'] == 1 and act in ('left', 'right', 'ok'):
                step = -1 if act == 'left' else 1
                state['quality'] = profiles.QUALITIES[(profiles.QUALITIES.index(state['quality']) + step) % len(profiles.QUALITIES)]
                state.update(quality_req=state['quality'], quality_at=now, note='Quality: ' + state['quality'])
            elif m['sel'] == 2 and act in ('left', 'right', 'ok'):
                state['note'] = 'Saved' if self.store.toggle_later(v) else 'Removed'
            elif m['sel'] == 3 and act == 'ok':
                state['more'] = None
            state['osd_until'] = now + OSD_SECONDS

        loading = ui.Message('Starting', lines, spinner=True, on_back=False)
        state['opened_at'] = now
        self.push(loading)
        shown_osd = False
        next_save = now + 10
        try:
            start_feed(start_ms)
            while True:
                if loading is not None:
                    self.loading_frame()
                link.health()
                now = time.monotonic()
                act = self.inputs.get(0.02)
                if act and state['more'] is not None:
                    more_key(act, now)
                    act = None
                if act == 'back':
                    return 'stop', position['ms']
                if act in ('left', 'right', 'pgup', 'pgdn'):
                    d = {'left': -SEEK_SMALL, 'right': SEEK_SMALL, 'pgup': -SEEK_BIG, 'pgdn': SEEK_BIG}[act]
                    basepos = state['pending'] if state['pending'] is not None else position['ms']
                    target = max(0, basepos + d * 1000)
                    if dur_ms:
                        target = min(target, max(0, dur_ms - 2000))
                    state.update(pending=target, pending_at=now, osd_until=now + OSD_SECONDS, note=('>> ' if d > 0 else '<< ') + ui.mmss(target))
                elif act == 'ok':
                    state['paused'] = not state['paused']
                    ring.set_pause(state['paused'])
                    state['osd_until'] = now + OSD_SECONDS
                elif act == 'alt':
                    state['note'] = 'Saved' if self.store.toggle_later(v) else 'Removed'
                    state['osd_until'] = now + OSD_SECONDS
                elif act == 'menu':
                    state.update(more={'sel': 0}, osd_until=now + OSD_SECONDS)
                elif act:
                    state['osd_until'] = now + OSD_SECONDS
                if (state['quality_req'] is not None and now - state['quality_at'] > QUALITY_SETTLE and state['pending'] is None
                        and state['quality_req'] != self.play_quality):
                    self.play_quality, state['quality_req'] = state['quality_req'], None
                    return 'quality', position['ms']
                if state['pending'] is not None and now - state['pending_at'] > SEEK_SETTLE:
                    target, state['pending'] = state['pending'], None
                    state.update(paused=False, note='Seeking...', osd_until=now + OSD_SECONDS + 3)
                    start_feed(target)
                # what the core says: the position, and whether a picture is on the screen
                st = ring.status()
                if st['state'] == 5:
                    raise pipeline.PlaybackError('The H264 core stopped: ' + ring.state_text(st))
                if st['state'] in (3, 4) and st['pictures_seen'] > 0:
                    position['ms'] = min(dur_ms or 1 << 60, st['stc'] // 90)
                    if loading is not None:
                        if self.screen is loading:
                            self.pop()
                        loading = None
                        self.log('First picture %.1f s after starting' % (now - state['opened_at']))
                        link.hide_overlay()
                        shown_osd = False
                    if state['seeking']:
                        state.update(seeking=False, note='', osd_until=now + 1.5)
                # the playback bar
                if loading is None:
                    show_osd = now < state['osd_until'] or state['more'] is not None
                    if show_osd:
                        self._osd_overlay(v, position['ms'] if state['pending'] is None else state['pending'], dur_ms,
                                          state['paused'], state)
                        shown_osd = True
                    elif shown_osd:
                        link.hide_overlay()
                        self.osd_sig = None
                        shown_osd = False
                if now >= next_save and not state['paused']:
                    next_save = now + 10
                    self.store.record_play(v, position['ms'], dur_ms, force=False)
                if feed['error'] is not None:
                    exc = feed['error']
                    feed['error'] = None
                    msg = str(exc)
                    if '403' in msg and not state.get('retried_403'):
                        state['retried_403'] = True
                        self.log('YouTube refused the stream (403); asking for fresh stream URLs')
                        return 'refresh', position['ms']
                    raise pipeline.PlaybackError(msg)
                if feed['result'] == 'ended':
                    return 'eof', dur_ms
        finally:
            stop_feed()
            link.hide_overlay()
            self.osd_sig = None
            if loading is not None and self.screen is loading:
                self.pop()

    @staticmethod
    def _decoder_load(state):
        """How much of a core ffmpeg used over the last few seconds (percent); 100 when it is not known yet."""
        recent = state.get('video_recent')
        return sum(recent) / len(recent) if recent else 100.0

    def _play_loop(self, v, media, prof, speed, start_ms, may_fall_back=False, can_refresh=False):
        link, fps = self.link, prof.fps
        state = {'start': start_ms, 'base': link.sent, 'paused': False, 'pause_pushed': False,
                 'osd_until': time.monotonic() + OSD_SECONDS, 'note': '', 'last': None,
                 'pending': None, 'pending_at': 0.0, 'seeking': False,
                 'crop': self.store.settings.get('crops', {}).get(v['id']) or self.store.settings.get('crop', 'Fill'), 'more': None, 'crop_change': False,
                 'quality': self.play_quality, 'quality_req': None, 'quality_at': 0.0,
                 'opened_at': time.monotonic(), 'starved': 0.0, 'win_start': time.monotonic(),
                 'tick': time.monotonic(), 'tier': media.tier, 'retries': 0, 'smooth_noted': False,
                 'crashes': 0, 'crash_at': 0.0}
        dur_ms = media.duration * 1000
        ytarget = pipeline.yuv_target(media, link.interlaced, self.big_pictures(), state['crop']) if self.yuv else None
        state['flags'] = 0

        def pos_ms():
            return min(dur_ms or 1 << 60, state['start'] + max(0, link.consumed - state['base']) * 1000 // fps)

        def open_pipe(at_ms):
            state['start'], state['base'] = at_ms, link.sent
            state['opened_at'] = time.monotonic()
            state['first_logged'] = False
            state['starved'], state['win_start'], state['win_push'] = 0.0, time.monotonic(), 0.0
            state['pipe_target'] = ytarget                   # the picture size this decoder produces (a crop change opens a new one)
            try:
                return pipeline.open_pipeline(self.rt, prof, media, at_ms / 1000.0, speed, lambda: None,
                                              crop=state['crop'], target=ytarget)
            except Exception as exc:
                raise pipeline.PlaybackError(str(exc))

        def set_crop(mode, now):
            """Change the crop for this video. The picture filter lives in ffmpeg, so the decoders
            restart at the current position once changes settle (same path as a seek)."""
            if mode == state['crop']:
                return
            state['crop'] = mode
            crops = dict(self.store.settings.get('crops', {}))     # remembered for this video: it opens like this next time
            crops.pop(v['id'], None)
            crops[v['id']] = mode
            self.store.set('crops', dict(list(crops.items())[-200:]))
            nonlocal ytarget
            if self.yuv:
                new_target = pipeline.yuv_target(media, link.interlaced, self.big_pictures(), mode)
                if new_target == ytarget:                   # the FPGA does the crop: it takes effect on the next picture
                    state.update(note='Crop: ' + mode)
                    return
                ytarget = new_target                        # ffmpeg trims the sides: the picture size changes, so the decoders restart
            if state['pending'] is None:
                state['pending'] = pos_ms()
            state.update(pending_at=now, crop_change=True, note='Crop: ' + mode)

        def more_key(act, now):
            """Keys while the More panel is open: up/down choose, left/right/ok change, back closes."""
            m = state['more']
            if act in ('back', 'menu'):
                state['more'] = None
            elif act == 'down':
                m['sel'] = (m['sel'] + 1) % 4
            elif act == 'up':
                m['sel'] = (m['sel'] - 1) % 4
            elif m['sel'] == 0 and act in ('left', 'right', 'ok'):
                step = -1 if act == 'left' else 1
                set_crop(profiles.CROPS[(profiles.CROPS.index(state['crop']) + step) % len(profiles.CROPS)], now)
            elif m['sel'] == 1 and act in ('left', 'right', 'ok'):
                # Quality for this video only. It applies about a second after the last press (so cycling through the
                # choices does not restart the picture each time), at the same position.
                step = -1 if act == 'left' else 1
                state['quality'] = profiles.QUALITIES[(profiles.QUALITIES.index(state['quality']) + step) % len(profiles.QUALITIES)]
                state.update(quality_req=state['quality'], quality_at=now, note='Quality: ' + state['quality'])
            elif m['sel'] == 2 and act in ('left', 'right', 'ok'):
                state['note'] = 'Saved' if self.store.toggle_later(v) else 'Removed'
            elif m['sel'] == 3 and act == 'ok':
                state['more'] = None
            state['osd_until'] = now + OSD_SECONDS
            state['pause_pushed'] = False                 # redraw a paused frame so the panel updates
        pipe = open_pipe(start_ms)
        next_save = time.monotonic() + 10
        cpuw, next_cpu = cpu.CpuWatch({}), time.monotonic() + 12.0          # the first sample early: a stream that struggles steps down soon
        quick, next_quick = cpu.CpuWatch({}), time.monotonic() + CPU_QUICK_SECONDS
        loop_t = {'since': time.monotonic(), 'wait': 0.0, 'push': 0.0, 'poll': 0.0, 'keys': 0.0}

        def starting_lines():
            """What the screen says while the decoders start (a video whose stream details are cached has no lookup to show)."""
            try:
                tail = bytes(pipe.decoder.errors.get('video', b'')).decode(errors='replace').strip().splitlines()
            except Exception:
                tail = []
            return [ui.clip(v.get('title', ''), 33), 'Starting the decoders... %ds' % (time.monotonic() - state['opened_at']),
                    '%s, %d kbps' % (media.label, round(media.tbr or 0)), ui.clip(pipeline.safe_text(tail[-1]) if tail else '', 33)]
        loading = ui.Message('Starting', starting_lines, spinner=True, on_back=False)
        self.push(loading)                                   # with the spinner, until the first picture arrives
        try:
            while True:
                if loading is not None:
                    self.loading_frame()
                link.health()
                now = time.monotonic()
                tin = time.monotonic()
                act = self.inputs.get(0)                     # (it used to wait up to 3 ms here on every pass)
                loop_t['keys'] += time.monotonic() - tin
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
                if (state['quality_req'] is not None and now - state['quality_at'] > QUALITY_SETTLE
                        and state['pending'] is None and state['quality_req'] != self.play_quality):
                    self.play_quality, state['quality_req'] = state['quality_req'], None
                    return 'quality', pos_ms()
                if state['quality_req'] == self.play_quality:
                    state['quality_req'] = None
                if state['pending'] is not None and now - state['pending_at'] > SEEK_SETTLE:
                    seek_to, state['pending'] = state['pending'], None
                    pipe.close(wait=False)                  # do not wait for the old ffmpeg to exit
                    pipe = open_pipe(seek_to)
                    state.update(paused=False, pause_pushed=False, seeking=True,
                                 note='Applying crop...' if state['crop_change'] else 'Seeking...',
                                 crop_change=False, osd_until=now + OSD_SECONDS + 3)
                    if state['last'] is not None and link.queued < link.slots:   # immediate on-screen feedback
                        self._present(state['last'], v, seek_to, dur_ms, False, state, prof, True, True, state['last_target'])
                # Is the ARM keeping up? The FPGA queue only runs dry when the decoder falls behind.
                dt, state['tick'] = now - state['tick'], now
                rs = getattr(pipe, 'restarts', 0)                 # an audio stream reopened: the picture waits for it, not for the ARM
                if rs != state.get('restarts', 0):
                    state['restarts'], state['restart_at'] = rs, now
                settling = (now - state.get('restart_at', -99.0) < 8.0) or bool(dur_ms and pos_ms() > dur_ms - 20000)
                empty = link.queued == 0
                counting = (may_fall_back and not state['paused'] and not state['seeking'] and state['pending'] is None
                            and state['last'] is not None and now - state['opened_at'] > STARVE_GRACE and not settling)
                if counting and empty:
                    state['starved'] += dt
                    if not state.get('was_empty'):                 # a new dropout: the audio gaps even when the total time is short
                        state['dry'] = state.get('dry', 0) + 1
                state['was_empty'] = empty
                if (may_fall_back and not state['smooth_noted'] and state['starved'] == 0.0
                        and now - state['opened_at'] > SMOOTH_AFTER):
                    state['smooth_noted'] = True
                    self._note_smooth(media)
                if now - state['win_start'] > STARVE_WINDOW:
                    if may_fall_back and (state['starved'] > STARVE_LIMIT or state.get('dry', 0) >= DRY_LIMIT):
                        span = max(0.001, now - state['win_start'])
                        dec, push = self._decoder_load(state), 100.0 * state.get('win_push', 0.0) / span
                        if dec >= BOOST_MIN_CPU or push >= 100.0 * BUSY_PUSH:
                            self.log('Starved %.1fs (%d dropouts) of the last %.0fs at %s (ffmpeg %d%% of a core, picture copy %d%% of the loop): stepping down' %
                                     (state['starved'], state.get('dry', 0), span, media.tier, dec, push))
                            return 'downgrade', pos_ms()
                        # Neither the decoder nor the picture copy was busy: the data did not arrive (the network, a slow start-up),
                        # which a smaller picture does not fix and which says nothing about the ARM. Once is ignored; a repeat steps
                        # down to a stream that needs less data, without making every later estimate more cautious.
                        self.log('Starved %.1fs (%d dropouts) of the last %.0fs at %s while ffmpeg used %d%% of a core and the copy %d%% of the loop: %s' %
                                 (state['starved'], state.get('dry', 0), span, media.tier, dec, push,
                                  'waiting for data, ignoring it once' if not state.get('stalls') else 'waiting for data again, stepping down'))
                        if state.get('stalls'):
                            return 'stall', pos_ms()
                        state['stalls'] = 1
                    else:
                        state['stalls'] = 0                       # a clean window forgives an earlier stall
                    state['starved'], state['dry'], state['win_start'], state['win_push'] = 0.0, 0, now, 0.0
                if now >= next_quick:                        # how busy ffmpeg is, a few seconds at a time, for the starve decision below
                    next_quick = now + CPU_QUICK_SECONDS
                    try:
                        procs = getattr(getattr(pipe, 'decoder', None), 'procs', {}) or {}
                        quick.named = {'video ffmpeg': getattr(procs.get('video'), 'pid', None)}
                        quick.sample()
                        reading = quick.values.get('video ffmpeg')
                        if reading is not None and reading >= 0:       # (a restarted decoder is a new process: its first reading is void)
                            state['video_recent'] = (state.get('video_recent', []) + [reading])[-2:]
                    except Exception:
                        pass
                if now >= next_cpu:                          # where the ARM's time goes (percent of one core; both busy = 200)
                    next_cpu = now + CPU_LOG_SECONDS
                    span = max(0.001, now - loop_t['since'])
                    where = 'loop: %d%% waiting for the decoder, %d%% reading a picture, %d%% sending pictures, %d%% keys and health' % (
                        round(100 * loop_t['wait'] / span), round(100 * loop_t['poll'] / span), round(100 * loop_t['push'] / span),
                        round(100 * loop_t['keys'] / span))
                    loop_t.update(since=now, wait=0.0, push=0.0, poll=0.0, keys=0.0)
                    try:
                        procs = getattr(getattr(pipe, 'decoder', None), 'procs', {}) or {}
                        cpuw.named = {'video ffmpeg': getattr(procs.get('video'), 'pid', None),
                                      'audio ffmpeg': getattr(procs.get('audio'), 'pid', None)}
                        text = cpuw.sample()
                        if text and not state['paused']:
                            self.log('CPU %s (%s, %d queued); %s' % (text, media.tier, link.queued, where))
                    except Exception:                        # a log line must never stop a video
                        pass
                if now >= next_save:
                    self.store.record_play(v, pos_ms(), dur_ms, force=False)
                    next_save = now + 10
                if link.queued >= link.slots:
                    time.sleep(.002)
                    continue
                if state['paused']:
                    if not state['pause_pushed'] and state['last'] is not None:
                        self._present(state['last'], v, pos_ms(), dur_ms, True, state, prof, True, True, state['last_target'])
                        state['pause_pushed'] = True
                    else:
                        time.sleep(.01)
                    continue
                tp = time.monotonic()
                try:
                    kind, rec = pipe.poll()
                    if kind == 'record':
                        loop_t['poll'] += time.monotonic() - tp
                except Exception as exc:
                    msg = str(exc)
                    if '403' in msg and state['retries'] < 3:      # YouTube sometimes refuses at random: try again
                        state['retries'] += 1
                        self.log('YouTube refused the stream (403); retry %d of 3' % state['retries'] +
                                 ((': ' + ' | '.join(msg.splitlines())[-200:]) if state['retries'] == 1 else ''))
                        pipe.close(wait=False)
                        time.sleep(0.6)
                        pipe = open_pipe(pos_ms())
                        state.update(seeking=True, note='Retrying...', osd_until=now + OSD_SECONDS + 3)
                        continue
                    if '403' in msg and can_refresh:
                        self.log('403 persists; asking YouTube for fresh stream URLs')
                        return 'refresh', pos_ms()
                    if re.search(r'\(-11\)|reset by peer|Connection reset|Broken pipe', msg):
                        # This ffmpeg build crashes (SIGSEGV) when YouTube drops the connection: carry on from here.
                        if now - state['opened_at'] < 8.0 and not state['first_logged']:
                            # It crashed before showing a single picture: starting again the same way will crash again.
                            state['early'] = state.get('early', 0) + 1
                            if state['early'] == 1:
                                state['early_base'] = pos_ms()
                                self._log_crash_clues()
                                if msg.startswith('audio'):
                                    self._probe_audio(media, prof, pos_ms())
                            if state['early'] <= len(EARLY_NUDGES_MS):
                                # ffmpeg's seek crashes at certain positions of some streams (the same position crashes every time,
                                # a second further on works): start a little before or after instead of at the same place.
                                at = max(0, state['early_base'] + EARLY_NUDGES_MS[state['early'] - 1])
                                self.log('The decoder crashes as soon as it starts at %s; trying %s (%d of %d)' % (
                                    ui.mmss(state['early_base']), ui.mmss(at), state['early'], len(EARLY_NUDGES_MS)))
                                pipe.close(wait=False)
                                time.sleep(0.3)
                                pipe = open_pipe(at)
                                state.update(seeking=True, note='Reconnecting...', osd_until=now + OSD_SECONDS + 3)
                                continue
                            if state['early'] > len(EARLY_NUDGES_MS):
                                if may_fall_back:
                                    self.log('The decoder crashes as soon as it starts at %s: stepping down' % media.tier)
                                    return 'crashstep', pos_ms()
                                raise pipeline.PlaybackError(
                                    'The decoder crashed as soon as it started at %s (%s). Try a lower quality (X, then Quality) or Auto.' %
                                    (media.tier, ' '.join(msg.split())[:50]))
                        else:
                            state['early'] = 0
                        if now - state['crash_at'] > 120:
                            state['crashes'] = 0
                        state['crash_at'] = now
                        if state['crashes'] < 5:
                            state['crashes'] += 1
                            self.log('The decoder stopped (%s); reconnecting at %s (%d of 5)' % (
                                ' | '.join(msg.splitlines())[:120], ui.mmss(pos_ms()), state['crashes']))
                            pipe.close(wait=False)
                            time.sleep(0.3)
                            pipe = open_pipe(pos_ms())
                            state.update(seeking=True, note='Reconnecting...', osd_until=now + OSD_SECONDS + 3)
                            continue
                    raise pipeline.PlaybackError(msg)
                if kind == 'wait':
                    time.sleep(.002)
                    loop_t['wait'] += time.monotonic() - tp
                    continue
                if kind == 'eof':
                    return 'eof', dur_ms
                state['last'] = rec
                state['last_target'] = state.get('pipe_target')
                state['retries'] = 0
                if self.yuv and not state.get('flags_known'):
                    state['flags'] = pipe.decoder.colour_flags(state['pipe_target']['h'])      # matrix and range, from ffmpeg's stream line
                    state['flags_known'] = True
                if state.get('opened_at') and not state.get('first_logged'):
                    state['first_logged'] = True
                    self.log('First picture %.1f s after starting the decoders' % (time.monotonic() - state['opened_at']))
                if loading is not None:
                    if self.screen is loading:
                        self.pop()
                    loading = None
                if state['seeking']:                         # first picture from the new position
                    state.update(seeking=False, note='', osd_until=now + 1.5)
                show_osd = now < state['osd_until'] or state['more'] is not None
                if show_osd:
                    shown = state['pending'] if state['pending'] is not None else pos_ms()
                else:
                    shown = 0
                    if state['note']:
                        state['note'] = ''
                tq = time.monotonic()
                self._present(rec, v, shown, dur_ms, False, state, prof, False, show_osd, state.get('pipe_target'))
                loop_t['push'] += time.monotonic() - tq
                state['win_push'] = state.get('win_push', 0.0) + (time.monotonic() - tq)
        finally:
            if loading is not None and self.screen is loading:
                self.pop()
            pipe.close(wait=False)        # kill the decoders at once: a step-down or a stop must not wait for a busy ffmpeg to exit

    def _present(self, rec, v, pos, dur, paused, state, prof, silent, osd, target):
        """Publish one decoded picture + PCM to the core, with the playback bar when `osd` (v0.15: as an overlay layer the
        FPGA draws, so nothing is burned into the picture; v0.14: burned into the 320x240 record)."""
        if not self.yuv:
            if osd:
                rec = self._osd(rec, v, pos, dur, paused, state, prof, silent)
            elif silent:
                rec = rec[:prof.video_bytes] + bytes(prof.audio_bytes)
            self.link.push(rec)
            return
        frame, audio = rec
        if silent:
            audio = self.silence
        overlay = self._osd_overlay(v, pos, dur, paused, state) if osd else None
        self.link.push_picture(frame, audio, target['w'], target['h'], 'Off' if target.get('crop_w') else state['crop'],
                               state.get('flags', 0), overlay)

    def _osd_overlay(self, v, pos, dur, paused, state):
        """The overlay buffer holding the playback bar for this moment: drawn again only when what it shows changes."""
        m = state.get('more')
        sig = (pos // 1000, paused, state['note'], None if m is None else m['sel'], state['crop'], state.get('tier'), state.get('quality'),
               self.store.in_later(v['id']), state.get('flags', 0))
        if sig == self.osd_sig and self.osd_ov is not None:
            return self.osd_ov
        idx = self.link.free_overlay(keep=self.osd_ov)
        if idx is None:                                   # all busy: keep showing the old one
            return self.osd_ov
        bt601 = bool(state.get('flags', 0) & yuv.FLAG_601)
        if self.osd_canvas is None or self.osd_canvas.bt601 != bt601:
            self.osd_canvas = YuvCanvas(320, 240, bt601)
        c = self.osd_canvas
        c.clear()
        self._draw_osd(c, v, pos, dur, paused, state)
        self.link.upload_overlay(idx, c.y, c.u, c.v)
        self.osd_ov, self.osd_sig = idx, sig
        return idx

    def _osd(self, rec, v, pos, dur, paused, state, prof, silent=False):
        """Return `rec` with a title/progress bar burned into its picture."""
        buf = bytearray(rec)
        if silent:
            buf[prof.video_bytes:] = bytes(prof.audio_bytes)
        self._draw_osd(Canvas(320, 240, buf, 0), v, pos, dur, paused, state)
        return bytes(buf)

    def _draw_osd(self, c, v, pos, dur, paused, state):
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
            px, py, pw, ph = 56, 40, 208, 102
            c.rect(px, py, pw, ph, ui.BLACK)
            c.frame(px, py, pw, ph, ui.RED, 2)
            c.text(px + 10, py + 7, 'More', ui.WHITE, ui.BLACK)
            rows = ['Crop: ' + state['crop'],
                    'Quality: ' + state['quality'],
                    'Watch Later: ' + ('Saved' if self.store.in_later(v['id']) else 'Add'),
                    'Close']
            for i, text in enumerate(rows):
                bg = ui.SEL if i == m['sel'] else ui.BLACK
                yy = py + 22 + i * 16
                c.rect(px + 4, yy, pw - 8, 14, bg)
                c.text(px + 10, yy + 3, text, ui.WHITE if i == m['sel'] else ui.TEXT, bg)
            c.text(px + 10, py + 90, 'Left/Right   B:Back', ui.DIM, ui.BLACK)
