"""YouTube RSS feeds (no auth) and thumbnails. Network work runs on worker
threads; results land in plain dicts guarded by a lock."""
import calendar
import json
import os
import re
import ssl
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015',
      'm': 'http://search.yahoo.com/mrss/'}
FEED = 'https://www.youtube.com/feeds/videos.xml?channel_id='
TTL = 15 * 60
MAX_LOADED = 500        # stop offering "Load more" at this many videos in one list
PAGE = 50               # videos fetched per page of a channel or playlist (yt-dlp, not the 15-video RSS feed)
RSS_TRIES = 4           # YouTube's RSS endpoint answers 404/500 at random: retry before giving up
RSS_DOWN_AFTER = 2      # this many channels in a row failing on RSS: treat the endpoint as down
RSS_DOWN_FOR = 30 * 60  # and do not try it again for this long (seconds)
RSS_BACKOFF = 1.0       # seconds; waits 1 s, 2 s, 3 s between tries
UA = 'Mozilla/5.0 (X11; Linux armv7l) YouTubeCRT/1.0'
THUMB_W, THUMB_H = 80, 45
THUMB_BYTES = {'rgb565': THUMB_W * THUMB_H * 2,
               'yuv420p': THUMB_W * THUMB_H + 2 * ((THUMB_W + 1) // 2) * ((THUMB_H + 1) // 2)}      # Y, then U and V


def parse_time(text):
    try:
        return calendar.timegm(time.strptime(text[:19], '%Y-%m-%dT%H:%M:%S'))
    except (ValueError, TypeError):
        return 0


def parse_feed(xml_bytes):
    """-> (channel_name, [video dicts newest first])"""
    root = ET.fromstring(xml_bytes)
    channel = (root.findtext('a:author/a:name', default='', namespaces=NS) or
               root.findtext('a:title', default='', namespaces=NS))
    videos = []
    for e in root.findall('a:entry', NS):
        vid = e.findtext('yt:videoId', default='', namespaces=NS)
        if not vid:
            continue
        link = e.find("a:link[@rel='alternate']", NS)
        href = link.get('href', '') if link is not None else ''
        views = e.find('m:group/m:community/m:statistics', NS)
        videos.append({
            'id': vid,
            'title': e.findtext('a:title', default='', namespaces=NS),
            'channel': e.findtext('a:author/a:name', default=channel, namespaces=NS),
            'channel_id': e.findtext('yt:channelId', default='', namespaces=NS),
            'published': parse_time(e.findtext('a:published', default='', namespaces=NS)),
            'short': '/shorts/' in href,
            'views': int(views.get('views', 0)) if views is not None else 0,
        })
    videos.sort(key=lambda v: v['published'], reverse=True)
    return channel, videos


def age_text(ts, now=None):
    if not ts:
        return ''
    s = max(0, int((now or time.time()) - ts))
    for unit, n in (('y', 31536000), ('mo', 2592000), ('w', 604800), ('d', 86400), ('h', 3600), ('m', 60)):
        if s >= n:
            return '%d%s' % (s // n, unit)
    return 'now'


class Feeds:
    def __init__(self, cache_dir, cafile=None):
        self.cache_dir = cache_dir
        self.ctx = ssl.create_default_context(cafile=cafile) if cafile and os.path.exists(cafile) \
            else ssl.create_default_context()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=3)
        self.channels = {}     # channel_id -> {'name','videos','at','ok'}
        self.handles = self._load('handles.json', {})
        self.thumbs = {}       # video id -> bytes | None (failed) ; absent = not requested
        self.thumb_pending = set()
        self.ffmpeg = None
        self.thumb_format = 'rgb565'          # 'yuv420p' when the v0.15 core's YUV canvas draws them
        self.generation = 0    # bumped whenever data changes, UI polls it
        self.log = lambda msg: None   # set by the player so problems reach the log file
        self.status = {}       # subscription ref -> 'ok, 15 videos' or the reason it failed
        self.playlist_fetch = None   # callable(playlist_id) -> (title, [videos]), set by the player
        self.page_fetch = None       # callable(playlist_id, start, count) -> (title, [videos]): later pages
        self.loading = set()         # ('channel' | 'playlist', id) pages being fetched right now
        self.playlists = self._load('playlists.json', {})   # playlist id -> {'title', 'videos', 'at'}
        self.titles = self._load('titles.json', {})         # video id -> {'title', 'channel'} (+ 'err')
        self.fallback = None   # callable(channel_id) -> [video dicts]: used when RSS keeps failing
        self.fallback_lock = threading.Lock()   # one heavy yt-dlp at a time
        self.rss_failed = 0                     # channels in a row whose RSS feed failed completely
        self.rss_down_until = 0.0               # while set, RSS is not even tried: YouTube's feed endpoint is down
        self.hold = threading.Event()           # set while a video is starting or playing: background jobs wait their turn
        disk = self._load('feeds.json', {})
        for cid, e in disk.items():
            e['ok'] = True
            self.channels[cid] = e

    # persistence ------------------------------------------------------
    def _load(self, name, default):
        try:
            with open(os.path.join(self.cache_dir, name), 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save(self, name, data):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = os.path.join(self.cache_dir, name + '.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp, os.path.join(self.cache_dir, name))
        except OSError:
            pass

    def wait_turn(self):
        """Background work stands aside while a video is starting or playing: that is the core's job."""
        while self.hold.is_set():
            time.sleep(0.5)

    def _get(self, url, timeout=15, headers=None):
        req = urllib.request.Request(url, headers=dict({'User-Agent': UA}, **(headers or {})))
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
            return r.read(4 * 1024 * 1024)

    # channels ---------------------------------------------------------
    @staticmethod
    def _why(exc):
        """A short message a person can act on."""
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 404:
                return 'not found (check the spelling)'
            return 'YouTube answered HTTP %d' % exc.code
        return str(exc)[:60] or exc.__class__.__name__

    @staticmethod
    def _clean(exc):
        """yt-dlp's error text as something short to read: drop the 'ERROR: [youtube:tab] id:' decoration."""
        text = (str(exc).strip().splitlines() or ['failed'])[-1]
        text = re.sub(r'^.*?ERROR:\s*', '', text)
        text = re.sub(r'^\[[^\]]*\]\s*(\S+:\s*)?', '', text)
        return (text[:1].upper() + text[1:])[:70] or 'failed'

    def _find_in_page(self, url, patterns, limit=800 * 1024):
        """Fetch a page but stop as soon as one of `patterns` matches: channel pages are 1.5-2.7 MB, and the
        channel id appears in the first few KB of <head>."""
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Cookie': 'CONSENT=YES+1'})
        got = b''
        with urllib.request.urlopen(req, timeout=20, context=self.ctx) as r:
            while len(got) < limit:
                chunk = r.read(32 * 1024)
                if not chunk:
                    break
                got += chunk
                text = got.decode('utf-8', 'replace')
                for pat in patterns:
                    m = re.search(pat, text)
                    if m:
                        return m.group(1)
        return None

    def resolve_handle(self, handle):
        """@handle -> channel id (cached on disk), or None with the reason in self.status."""
        if handle in self.handles:
            return self.handles[handle]
        try:
            cid = self._find_in_page('https://www.youtube.com/' + handle, (
                r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                r'"externalId":"(UC[\w-]{22})"', r'"channelId":"(UC[\w-]{22})"', r'"browseId":"(UC[\w-]{22})"'))
        except Exception as exc:
            self.status[handle] = self._why(exc)
            self.log('channel %s: lookup failed: %s' % (handle, self.status[handle]))
            return None
        if not cid:
            self.status[handle] = 'could not read its channel id'
            self.log('channel %s: %s' % (handle, self.status[handle]))
            return None
        self.handles[handle] = cid
        self._save('handles.json', self.handles)
        self.log('channel %s is %s' % (handle, cid))
        return cid

    def _refresh_one(self, ref, name_override, force):
        cid = ref
        if ref.startswith('@'):
            cid = self.resolve_handle(ref)
            if not cid:
                return
        with self.lock:
            cur = self.channels.get(cid)
        if cur and not force and time.time() - cur.get('at', 0) < TTL:
            if name_override:
                cur['name'] = name_override
            self.status[ref] = 'ok, %d videos' % len(cur['videos'])
            return
        self.wait_turn()
        entry, err = None, None
        rss_down = time.time() < self.rss_down_until
        if rss_down:
            err = RuntimeError('RSS is down')
        for attempt in range(0 if rss_down else RSS_TRIES):
            try:
                name, videos = parse_feed(self._get(FEED + cid))
                entry = {'name': name_override or name, 'videos': videos, 'at': time.time(), 'ok': True}
                self.status[ref] = 'ok, %d videos' % len(videos)
                self.rss_failed, self.rss_down_until = 0, 0.0
                break
            except Exception as exc:
                err = exc
                again = (not isinstance(exc, urllib.error.HTTPError)) or exc.code in (404, 429, 500, 502, 503)
                if again and attempt + 1 < RSS_TRIES:
                    time.sleep(RSS_BACKOFF * (attempt + 1))
                    continue
                break
        if entry is None and not rss_down:
            self.rss_failed += 1
            if self.rss_failed >= RSS_DOWN_AFTER:           # two channels in a row: stop wasting time and CPU on a dead endpoint
                self.rss_down_until = time.time() + RSS_DOWN_FOR
                self.log('YouTube\'s RSS feeds are failing for every channel; using yt-dlp instead for %d min' % (RSS_DOWN_FOR // 60))
        if entry is None and self.fallback:
            self.wait_turn()
            self.log('channel %s: RSS failed (%s); asking yt-dlp instead' % (ref, self._why(err)))
            try:
                with self.fallback_lock:
                    videos = self.fallback(cid)
                if videos:
                    entry = {'name': name_override or videos[0].get('channel') or ref, 'videos': videos,
                             'at': time.time(), 'ok': True}
                    self.status[ref] = 'ok, %d videos (no dates)' % len(videos)
            except Exception as exc:
                self.log('channel %s: yt-dlp fallback failed: %s' % (ref, str(exc)[:100]))
                err = RuntimeError('yt-dlp: ' + self._clean(exc))        # say what really failed, not the RSS 404
        if entry is None:
            self.status[ref] = self._why(err)
            self.log('channel %s (%s): feed failed: %s' % (ref, cid, self.status[ref]))
            if cur:
                cur['ok'] = False
                return
            entry = {'name': name_override or ref, 'videos': [], 'at': 0, 'ok': False}
        with self.lock:
            self.channels[cid] = entry
            self.generation += 1

    def refresh(self, subs, force=False):
        """Fetch every subscription; returns a Future-like list. Non-blocking."""
        def run():
            list(self.pool.map(lambda s: self._refresh_one(s[1], s[0], force), subs))
            with self.lock:
                self.generation += 1
                self._save('feeds.json', {c: {k: v for k, v in e.items() if k != 'ok'}
                                          for c, e in self.channels.items() if e.get('videos')})
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def sub_ids(self, subs):
        out = []
        for name, ref in subs:
            cid = self.handles.get(ref) if ref.startswith('@') else ref
            if cid:
                out.append(cid)
        return out

    def channel_list(self, subs):
        """[(id_or_ref, name, video_count, problem)] for EVERY subscription, so a failure is visible.

        `problem` is '' when the channel loaded, otherwise the reason (or 'not loaded yet')."""
        rows = []
        with self.lock:
            for name, ref in subs:
                cid = self.handles.get(ref) if ref.startswith('@') else ref
                e = self.channels.get(cid) if cid else None
                if e and e.get('ok', True) and e.get('videos') is not None and e.get('at', 0):
                    rows.append((cid, name or e['name'], len(e['videos']), ''))
                else:
                    rows.append((cid or ref, name or ref, -1, self.status.get(ref, 'not loaded yet')))
        return rows

    def channel_videos(self, cid, shorts=True):
        with self.lock:
            e = self.channels.get(cid)
            vids = list(e['videos']) if e else []
        return vids if shorts else [v for v in vids if not v['short']]

    def feed(self, subs, shorts=True, limit=100):
        vids = []
        with self.lock:
            for cid in self.sub_ids(subs):
                e = self.channels.get(cid)
                if e:
                    vids.extend(v for v in e['videos'] if not v.get('paged'))   # later pages have no dates
        if not shorts:
            vids = [v for v in vids if not v['short']]
        vids.sort(key=lambda v: v['published'], reverse=True)
        return vids[:limit]

    # pages ---------------------------------------------------------------
    def _entry(self, kind, ident):
        return (self.channels if kind == 'channel' else self.playlists).get(ident)

    def can_load_more(self, kind, ident):
        """Is there probably another page? (Unknown for channels until tried: their RSS gives only 15.)"""
        if not self.page_fetch:
            return False
        with self.lock:
            e = self._entry(kind, ident)
            if not e:
                return False
            if len(e['videos']) >= MAX_LOADED:
                return False
            return bool(e.get('more', True if kind == 'channel' else len(e['videos']) >= PAGE))

    def is_loading(self, kind, ident):
        return (kind, ident) in self.loading

    def load_more(self, kind, ident):
        """Fetch the next page of a channel's uploads or of a playlist and append it. Non-blocking: -> thread or None."""
        key = (kind, ident)
        if key in self.loading or not self.page_fetch:
            return None
        self.loading.add(key)

        def run():
            try:
                self.wait_turn()
                with self.lock:
                    e = self._entry(kind, ident)
                    have = len(e['videos']) if e else 0
                    start = (e.get('next') if e else 0) or have + 1      # position in the list, counting the ones we hide
                pid = ('UU' + ident[2:]) if kind == 'channel' else ident
                self.log('%s %s: loading more, from video %d (asking yt-dlp for %d)' % (kind, ident, start, PAGE))
                t0 = time.time()
                _, vids = self.page_fetch(pid, start, PAGE)      # not queued behind the background refreshes
                self.log('%s %s: yt-dlp answered with %d videos after %.0f s' % (kind, ident, len(vids), time.time() - t0))
                with self.lock:
                    e = self._entry(kind, ident)
                    seen = {v['id'] for v in e['videos']}
                    new = [dict(v, paged=True) for v in vids if v['id'] not in seen]
                    e['videos'].extend(new)
                    raw = getattr(vids, 'raw', len(vids))
                    e['next'] = start + raw
                    e['more'] = raw >= PAGE            # a full page from YouTube means there may be another
                    self.generation += 1
                    snap_channels = {c: {k: v for k, v in x.items() if k != 'ok'}
                                     for c, x in self.channels.items() if x.get('videos')}
                    snap_playlists = dict(self.playlists)
                self.log('%s %s: loaded %d more videos (%d total)' % (kind, ident, len(new), have + len(new)))
                self._save('feeds.json' if kind == 'channel' else 'playlists.json',
                           snap_channels if kind == 'channel' else snap_playlists)
            except Exception as exc:
                self.status[ident] = self._clean(exc)
                self.log('%s %s: could not load more: %s' % (kind, ident, self.status[ident]))
            finally:
                self.loading.discard(key)
                with self.lock:
                    self.generation += 1
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    # playlists and single videos ----------------------------------------
    def _oembed(self, vid):
        """Title and channel of one video: a light HTTP call (~0.4 s), no yt-dlp."""
        import urllib.parse
        url = 'https://www.youtube.com/oembed?format=json&url=' + urllib.parse.quote(
            'https://www.youtube.com/watch?v=' + vid, safe='')
        d = json.loads(self._get(url, timeout=15).decode('utf-8', 'replace'))
        return {'title': str(d.get('title', '')), 'channel': str(d.get('author_name', ''))}

    def refresh_lists(self, playlists, videos, force=False):
        """Fetch playlist contents (one yt-dlp at a time) and video titles. Non-blocking: returns the thread."""
        def run():
            for name, pid in playlists:
                cur = self.playlists.get(pid)
                if cur and not force and time.time() - cur.get('at', 0) < TTL:
                    self.status[pid] = 'ok, %d videos' % len(cur['videos'])
                    continue
                if not self.playlist_fetch:
                    self.status[pid] = 'yt-dlp is not installed'
                    continue
                self.wait_turn()
                try:
                    with self.fallback_lock:
                        title, vids = self.playlist_fetch(pid)
                    if not vids:
                        raise RuntimeError('empty, private or not found')
                    with self.lock:
                        self.playlists[pid] = {'title': title or pid, 'videos': vids, 'at': time.time(),
                                               'more': getattr(vids, 'raw', len(vids)) >= PAGE,
                                               'next': getattr(vids, 'raw', len(vids)) + 1}
                        self.generation += 1
                    self.status[pid] = 'ok, %d videos' % len(vids)
                except Exception as exc:
                    self.status[pid] = self._clean(exc)
                    self.log('playlist %s: %s' % (pid, self.status[pid]))
            for name, vid in videos:
                if vid in self.titles and not force and not self.titles[vid].get('err'):
                    continue
                self.wait_turn()
                try:
                    self.titles[vid] = self._oembed(vid)
                except Exception as exc:
                    self.titles[vid] = {'title': '', 'channel': '', 'err': self._why(exc)}
                    self.log('video %s: no title (%s)' % (vid, self.titles[vid]['err']))
                with self.lock:
                    self.generation += 1
            with self.lock:                                   # snapshot, so a save never races a change
                self.generation += 1
                snap_playlists, snap_titles = dict(self.playlists), dict(self.titles)
            self._save('playlists.json', snap_playlists)
            self._save('titles.json', snap_titles)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def playlist_list(self, pls):
        """[(id, name, count, problem)] for every playlist, so a failure is visible."""
        rows = []
        with self.lock:
            for name, pid in pls:
                e = self.playlists.get(pid)
                if e:
                    rows.append((pid, name or e['title'], len(e['videos']), ''))
                else:
                    rows.append((pid, name or pid, -1, self.status.get(pid, 'not loaded yet')))
        return rows

    def playlist_videos(self, pid, shorts=True):
        with self.lock:
            e = self.playlists.get(pid)
            vids = list(e['videos']) if e else []
        return vids if shorts else [v for v in vids if not v.get('short')]

    def listed_videos(self, entries):
        """Video dicts for the single videos in the file, with titles as soon as they are known."""
        out = []
        with self.lock:
            for name, vid in entries:
                meta = self.titles.get(vid) or {}
                title = name or meta.get('title') or (('(unavailable) ' if meta.get('err') else '(loading) ') + vid)
                out.append({'id': vid, 'title': title, 'channel': meta.get('channel', ''), 'published': 0,
                            'approx': True, 'short': False, 'views': 0})
        return out

    # thumbnails -------------------------------------------------------
    def thumb(self, vid):
        """-> raw picture bytes (THUMB_W x THUMB_H, in `thumb_format`) or None; schedules a fetch."""
        with self.lock:
            if vid in self.thumbs:
                return self.thumbs[vid]
            if vid in self.thumb_pending or not self.ffmpeg:
                return None
            self.thumb_pending.add(vid)
        self.pool.submit(self._fetch_thumb, vid)
        return None

    def _fetch_thumb(self, vid):
        self.wait_turn()
        fmt = self.thumb_format
        path = os.path.join(self.cache_dir, 'thumbs', vid + ('.rgb565' if fmt == 'rgb565' else '.yuv'))
        data = None
        try:
            with open(path, 'rb') as f:
                data = f.read()
            if len(data) != THUMB_BYTES[fmt]:
                data = None
        except OSError:
            pass
        if data is None:
            try:
                jpg = self._get('https://i.ytimg.com/vi/%s/mqdefault.jpg' % vid, timeout=10)
                p = subprocess.run(
                    [self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-i', 'pipe:0',
                     '-vf', 'scale=%d:%d:flags=area' % (THUMB_W, THUMB_H), '-pix_fmt', 'rgb565le' if fmt == 'rgb565' else 'yuv420p',
                     '-frames:v', '1', '-f', 'rawvideo', 'pipe:1'],
                    input=jpg, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
                if p.returncode == 0 and len(p.stdout) == THUMB_BYTES[fmt]:
                    data = p.stdout
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, 'wb') as f:
                        f.write(data)
                    self._trim_thumbs()
            except Exception:
                data = None
        with self.lock:
            self.thumbs[vid] = data
            self.thumb_pending.discard(vid)
            self.generation += 1

    def _trim_thumbs(self, keep=400):
        d = os.path.join(self.cache_dir, 'thumbs')
        try:
            files = sorted((os.path.getmtime(os.path.join(d, f)), f) for f in os.listdir(d))
            for _, f in files[:-keep]:
                os.remove(os.path.join(d, f))
        except OSError:
            pass
