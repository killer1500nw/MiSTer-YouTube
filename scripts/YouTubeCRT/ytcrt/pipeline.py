"""URL -> yt-dlp metadata -> ffmpeg pipes -> bounded A/V records.

Derived from the validated url_player.py (v0.13.7): same pipe reader, same
record pairing and four-record read-ahead. Differences: record sizes follow
the profile, playback can start at an offset (resume/seek), and decoder speed
options are selectable.
"""
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import profiles, yuv


class PlaybackError(RuntimeError):
    """A video could not be started or decoded (the menu keeps running)."""


class Runtime:
    """Locations of the external tools (see docs/RUNTIME.md)."""

    def __init__(self, root, here):
        base = Path(root)
        # Newest matching folders win, so a newer yt-dlp/ffmpeg can simply be dropped in.
        rts = sorted(base.glob('resolver-*-qjs-*')) or [base / 'resolver-2026.08.19-qjs-0.17.0']
        ffs = sorted(base.glob('ffmpeg-*-armhf-static')) or [base / 'ffmpeg-7.0.2-armhf-static']
        rt = rts[-1]
        self.resolver = rt / 'bundle/yt-dlp_linux_armv7l'
        self.qjs = rt / 'qjs'
        self.ffdir = ffs[-1]
        nodes = sorted(base.glob('node-*-armhf/node')) + [base / 'node-test' / 'node']      # the last is Test_Node_JS.sh's copy
        self.node = next((n for n in nodes if n.is_file()), None)    # Node.js solves YouTube's JS check in seconds; QuickJS needs minutes
        self.ffmpeg = self.ffdir / 'ffmpeg'
        self.ca = Path(here) / 'cacert.pem'
        self.resolve_cache = base / 'ytcrt' / 'cache' / 'resolve'      # recent resolves: YouTube's stream URLs last hours

    def missing(self):
        return [str(p) for p in (self.resolver, self.qjs, self.ffmpeg, self.ca) if not p.is_file()]


class Media:
    """A resolved video: the streams chosen for it plus the full catalogue, so the quality can change
    (step down when the ARM struggles) without asking YouTube again."""

    def __init__(self, vid, title, duration, video, audio, label='240p', catalog=None, why='', cost=0.0):
        self.id, self.title, self.duration, self.label = vid, title, duration, label
        self.video, self.audio, self.catalog, self.why, self.cost = video, audio, catalog, why, cost

    @property
    def tier(self):                 # the label shown on the playback bar
        return self.label

    @property
    def tbr(self):
        return (self.video or {}).get('tbr') or 0


def stop_process(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        except ProcessLookupError:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()


def safe_text(text):
    text = re.sub(r'https?://[^\s]+', '[URL]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)[:4096]


def enlarge_pipe(proc, size=1 << 20):
    """Give ffmpeg's output pipe room for a whole picture (Linux F_SETPIPE_SZ). The default 64 KB makes ffmpeg stop after every
    64 KB until the player has read it, so decoding waits on the player's reading; a big pipe lets it run ahead. Best effort."""
    try:
        import fcntl
        fcntl.fcntl(proc.stdout.fileno(), 1031, size)
        return True
    except (OSError, ImportError, ValueError, AttributeError):
        return False


BACKGROUND = set()                 # running background yt-dlp processes (see capture(background=True))
PAUSED = threading.Event()         # set while a video starts or plays: background processes are frozen


def pause_background():
    """Freeze every background yt-dlp job so the ARM is entirely the video's. They carry on afterwards."""
    PAUSED.set()
    for proc in list(BACKGROUND):
        try:
            os.killpg(proc.pid, signal.SIGSTOP)
        except (ProcessLookupError, PermissionError, AttributeError, OSError):
            pass


def resume_background():
    PAUSED.clear()
    for proc in list(BACKGROUND):
        try:
            os.killpg(proc.pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError, AttributeError, OSError):
            pass


def niced(command):
    """Run a background job at low priority so it never slows the video the user is waiting for."""
    nice = shutil.which('nice')
    return [nice, '-n', '15'] + list(command) if nice else list(command)


def capture(command, env, cancel, timeout=300, on_line=None, background=False):
    proc = subprocess.Popen([str(x) for x in command], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=env)
    if background:
        BACKGROUND.add(proc)
        if PAUSED.is_set():
            pause_background()
    chunks = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    active = list(chunks)
    deadline = time.monotonic() + timeout
    last = time.monotonic()
    pending = bytearray()
    try:
        while active:
            cancel()
            tick = time.monotonic()
            if background and PAUSED.is_set():
                deadline += tick - last               # frozen time does not count against the job (the real time, not a guess)
            last = tick
            if time.monotonic() > deadline:
                raise RuntimeError('YouTube URL resolution timed out.')
            ready, _, _ = select.select(active, [], [], 0.2)
            for fd in ready:
                data = os.read(fd, 65536)
                if not data:
                    active.remove(fd)
                    continue
                chunks[fd].extend(data)
                if on_line and fd == proc.stderr.fileno():
                    pending.extend(data)
                    *lines, rest = bytes(pending).split(b'\n')
                    pending[:] = rest
                    for line in lines:
                        text = safe_text(line.decode(errors='replace')).strip()
                        if text:
                            on_line(text)
                if len(chunks[fd]) > 16 * 1024 * 1024:
                    raise RuntimeError('Resolver output exceeded its bounded metadata limit.')
        code = proc.wait(timeout=2)
        if code:
            err = safe_text(chunks[proc.stderr.fileno()].decode(errors='replace')[-300:]).strip()
            raise RuntimeError('YouTube resolver failed (%d): %s' % (code, err.splitlines()[-1] if err else '?'))
        return bytes(chunks[proc.stdout.fileno()])
    finally:
        BACKGROUND.discard(proc)
        stop_process(proc)


# Print only what the player needs, as two lines. Past livestreams carry thousands of segment URLs
# per format, so a full --dump-single-json can exceed 20 MB; listing just these keys per format keeps
# the output small (about 65 KB for a normal video, 13 KB for a past livestream).
PRINT_INFO = '%(.{id,title,duration,is_live,live_status,http_headers})j'
PRINT_FORMATS = '%(formats.:.{format_id,protocol,vcodec,acodec,width,height,fps,tbr,ext,url,http_headers,language_preference})j'
LIVE_MESSAGE = 'This is a live or past livestream; those cannot be played here yet.'


def js_runtime_args(rt):
    """--js-runtimes for yt-dlp: Node first when installed (much faster on the ARM), QuickJS as the fallback."""
    node = getattr(rt, 'node', None)
    args = ['--js-runtimes', 'node:' + str(node)] if node else []
    return args + ['--js-runtimes', 'quickjs:' + str(rt.qjs)]


def resolve_command(rt, profile, url):
    return [rt.resolver, '--ignore-config', '--no-playlist', '--no-progress', '--verbose',     # -v: step lines on stderr, for the log
            '--no-cache-dir', '--skip-download', '--print', PRINT_INFO, '--print', PRINT_FORMATS,
            '--extractor-args', 'youtube:skip=translated_subs,hls',          # the HLS manifest is never used; skipping saves a request
            '--force-ipv4', '--socket-timeout', '20', '--retries', '1', '--fragment-retries', '1',
            *js_runtime_args(rt), '--compat-options', 'no-certifi',
            '--match-filters', '!is_live & !is_upcoming', '--ffmpeg-location', str(rt.ffdir), '--', url]


PRINT_UPLOAD = '%(.{id,title,channel,duration,view_count,live_status,url})j'


def channel_uploads(rt, channel_id, cancel=lambda: None, log=lambda m: None, limit=15):
    """A channel's newest uploads straight from YouTube via yt-dlp (no RSS). Newest first, no dates.

    Used when YouTube's RSS feed is failing. The uploads playlist of channel UCxxxx is UUxxxx."""
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    cmd = [rt.resolver, '--ignore-config', '--no-cache-dir', '--flat-playlist', '--playlist-end', str(limit),
           '--force-ipv4', '--socket-timeout', '20', '--retries', '1', '--compat-options', 'no-certifi',
           '--print', PRINT_UPLOAD, '--', 'https://www.youtube.com/playlist?list=UU' + channel_id[2:]]
    out = capture(niced(cmd), env, cancel, timeout=120, background=True).decode('utf-8', 'replace')
    now, videos = int(time.time()), []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        e = json.loads(line)
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', str(e.get('id', ''))):
            continue
        if e.get('live_status') in ('is_live', 'is_upcoming'):
            continue
        videos.append({'id': e['id'], 'title': e.get('title') or '', 'channel': e.get('channel') or '',
                       'channel_id': channel_id, 'published': now - len(videos) * 43200,   # ordering only
                       'approx': True, 'short': '/shorts/' in str(e.get('url', '')),
                       'views': int(e.get('view_count') or 0)})
    return videos


PRINT_PLAYLIST = '%(.{id,title,channel,duration,view_count,live_status,url,playlist_title})j'


class Page(list):
    """Videos of one page; `raw` is how many entries YouTube sent, including live/unusable ones we dropped."""
    raw = 0


def playlist_entries(rt, playlist_id, cancel=lambda: None, log=lambda m: None, limit=200, start=1, background=True):
    """Up to `limit` videos of a playlist from position `start` (1 = the first), in playlist order, via yt-dlp.
    -> (playlist title, [video dicts]). A channel's uploads are the playlist 'UU' + channel id without 'UC'.
    `background=False` for a fetch the user is waiting for: it must not be frozen by pause_background()."""
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    cmd = [rt.resolver, '--ignore-config', '--no-cache-dir', '--flat-playlist', '--verbose',
           '--playlist-start', str(start), '--playlist-end', str(start + limit - 1),
           '--force-ipv4', '--socket-timeout', '20', '--retries', '1', '--compat-options', 'no-certifi',
           '--print', PRINT_PLAYLIST, '--', 'https://www.youtube.com/playlist?list=' + playlist_id]
    shown = [0]

    def note(text):                            # what yt-dlp is doing, for the log: shows where a slow playlist waits
        if shown[0] < 30 and not text.startswith(('[debug] Command-line', '[debug] Encodings', '[debug] Python')):
            shown[0] += 1
            log('yt-dlp playlist %s: %s' % (playlist_id[-6:], text[:140]))
    out = capture(niced(cmd), env, cancel, timeout=120, on_line=note, background=background).decode('utf-8', 'replace')
    return _parse_flat(out)


def _parse_flat(out):
    """yt-dlp --flat-playlist --print output -> (title, Page of video dicts)."""
    title, videos = '', Page()
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        e = json.loads(line)
        videos.raw += 1
        title = title or e.get('playlist_title') or ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', str(e.get('id', ''))) or e.get('live_status') in ('is_live', 'is_upcoming'):
            continue
        videos.append({'id': e['id'], 'title': e.get('title') or '', 'channel': e.get('channel') or '',
                       'published': 0, 'approx': True, 'short': '/shorts/' in str(e.get('url', '')),
                       'views': int(e.get('view_count') or 0)})
    return title, videos


def search_entries(rt, query, cancel=lambda: None, log=lambda m: None, limit=20, start=1):
    """Search YouTube: `limit` results from result number `start`, via yt-dlp (listing only, nothing is resolved).
    -> ('', Page of video dicts); Page.raw counts every result YouTube sent."""
    query = re.sub(r'[\x00-\x1f]', ' ', str(query)).strip()[:120]
    if not query:
        raise ValueError('Nothing to search for.')
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    end = start + limit - 1
    cmd = [rt.resolver, '--ignore-config', '--no-cache-dir', '--flat-playlist', '--force-ipv4',
           '--playlist-start', str(start), '--playlist-end', str(end),
           '--socket-timeout', '20', '--retries', '1', '--compat-options', 'no-certifi',
           '--print', PRINT_PLAYLIST, '--', 'ytsearch%d:%s' % (end, query)]
    log('Searching YouTube for "%s" (results %d-%d)' % (query[:40], start, end))
    out = capture(cmd, env, cancel, timeout=180).decode('utf-8', 'replace')
    return _parse_flat(out)


PRINT_FOUND_PLAYLIST = '%(.{id,title,channel,uploader,playlist_count,n_entries,url})j'
FOUND_PLAYLIST_ID = re.compile(r'(?:PL|OL)[A-Za-z0-9_-]{10,40}')       # real playlists; the auto-made "mixes" (RD...) are not kept
# YouTube search filter "Playlist" (the sp parameter), tried in the forms yt-dlp has accepted
PLAYLIST_FILTERS = ('EgIQAw%253D%253D', 'EgIQAw%3D%3D')


def parse_found_playlists(out):
    """yt-dlp --flat-playlist --print output of a playlist search -> [{'id', 'title', 'channel', 'count'}], no duplicates."""
    found, seen = [], set()
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        pid = str(e.get('id') or '')
        if not pid:                                                          # fall back to the list= part of the link
            m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', str(e.get('url') or ''))
            pid = m.group(1) if m else ''
        if not FOUND_PLAYLIST_ID.fullmatch(pid) or pid in seen:
            continue
        seen.add(pid)
        count = e.get('playlist_count') or e.get('n_entries') or 0
        found.append({'id': pid, 'title': safe_text(str(e.get('title') or pid)),
                      'channel': safe_text(str(e.get('channel') or e.get('uploader') or '')),
                      'count': int(count) if str(count).isdigit() else 0})
    return found


def search_playlists(rt, query, cancel=lambda: None, log=lambda m: None, limit=20):
    """Search YouTube for public playlists: up to `limit` of them, via yt-dlp (listing only). -> [dict]"""
    query = re.sub(r'[\x00-\x1f]', ' ', str(query)).strip()[:120]
    if not query:
        raise ValueError('Nothing to search for.')
    from urllib.parse import quote_plus
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    found = []
    for sp in PLAYLIST_FILTERS:
        cmd = [rt.resolver, '--ignore-config', '--no-cache-dir', '--flat-playlist', '--force-ipv4', '--verbose',
               '--playlist-end', str(limit * 2), '--socket-timeout', '20', '--retries', '1', '--compat-options', 'no-certifi',
               '--print', PRINT_FOUND_PLAYLIST, '--', 'https://www.youtube.com/results?search_query=%s&sp=%s' % (quote_plus(query), sp)]
        log('Searching YouTube playlists for "%s"' % query[:40])
        seen_lines = [0]

        def note(text):                            # what yt-dlp is doing, for the log (the first lines only)
            if seen_lines[0] < 40 and not text.startswith(('[debug] Command-line', '[debug] Encodings', '[debug] Python')):
                seen_lines[0] += 1
                log('yt-dlp: ' + text[:160])
        found = parse_found_playlists(capture(cmd, env, cancel, timeout=100, on_line=note).decode('utf-8', 'replace'))
        if found:
            break
        log('Playlist search: no results with filter %s' % sp)
    return found[:limit]


def _source(fmt, info):
    """A stream description for ffmpeg, with the URL and headers validated."""
    url = fmt.get('url', '')
    if not url.startswith(('https://', 'http://')):
        raise ValueError('Resolver returned an unsupported stream URL.')
    headers = dict(info.get('http_headers') or {})
    headers.update(fmt.get('http_headers') or {})
    clean = {}
    for key, value in headers.items():
        if not re.fullmatch(r'[A-Za-z0-9-]+', str(key)) or any(c in str(value) for c in '\r\n\x00'):
            raise ValueError('Invalid media HTTP header.')
        clean[str(key)] = str(value)
    return {'url': url, 'headers': clean, 'format_id': str(fmt.get('format_id', '?')),
            'width': fmt.get('width'), 'height': fmt.get('height'), 'fps': fmt.get('fps'),
            'tbr': fmt.get('tbr')}


def _area(f):
    return f['width'] * f['height']


def choose_streams(info, formats, profile, quality='Auto', cal=None, mult=1.0, max_cost=None, codec=None):
    """Pick the video and audio stream for a video.

    quality '240p' / '360p' / '480p': the largest stream whose shorter side is at most that (highest bitrate first),
    whatever it costs. 'Auto': the largest stream the ARM is predicted to decode in real time
    (profiles.predicted_cost <= LIMIT, or `max_cost`); the lightest one if none qualifies.
    -> (video source, audio source or None, label such as '360p', predicted cost, why it is not the biggest)
    """
    cal = cal or profiles.CAL_DEFAULT
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        raise ValueError(LIVE_MESSAGE)
    playable = [f for f in formats if f.get('protocol') in ('https', 'http')
                and str(f.get('url', '')).startswith(('https://', 'http://'))]
    vids = [f for f in playable if f.get('vcodec') not in (None, 'none') and f.get('width') and f.get('height')]
    if not vids:
        if any(f.get('protocol') == 'http_dash_segments' for f in formats):
            raise ValueError('This is a past livestream (segmented stream). Those cannot be played here yet.')
        raise ValueError('No supported video stream was found.')
    # H.264 is what the cost model describes and what the ARM decodes best; use anything else only if there is nothing else.
    if codec:                                       # an experiment (the Speed test's AV1 probe): that codec or nothing
        pool = [f for f in vids if (f.get('vcodec') or '').startswith(codec)]
        if not pool:
            raise ValueError('No %s stream.' % codec)
    else:
        pool = [f for f in vids if (f.get('vcodec') or '').startswith('avc1')] or vids
    box = profiles.QUALITY_BOX.get(quality, 480)
    fit = [f for f in pool if min(f['width'], f['height']) <= box] or [min(pool, key=_area)]    # '360p' means 360 lines: 480x360 for 4:3, 640x360 for 16:9
    fit = [f for f in fit if min(f['width'], f['height']) >= 240] or fit          # never drop below 240p if 240p exists
    fit = [f for f in fit if f.get('acodec') in (None, 'none')] or fit            # video-only first (audio is separate)
    rank = sorted(fit, key=lambda f: (-_area(f), 0 if (f.get('fps') or 30) <= profile.fps else 1, -(f.get('tbr') or 0)))
    why = ''
    if quality == 'Auto':
        limit = profiles.limit_for(cal) if max_cost is None else max_cost
        cost = lambda f: profiles.predicted_cost(f, cal, mult)
        fits = [f for f in rank if cost(f) <= limit]
        video = fits[0] if fits else min(rank, key=cost)
        if not fits:
            why = 'nothing fits; using the lightest stream'
        elif video is not rank[0]:
            why = '%s at %d kbps would need %d%% of the ARM' % (profiles.label_for(rank[0]['width'], rank[0]['height']),
                                                                rank[0].get('tbr') or 0, round(cost(rank[0]) * 100))
    else:
        video = rank[0]
    muxed = video.get('acodec') not in (None, 'none')
    audio = None
    if muxed:
        audio = video
    else:
        auds = [f for f in playable if f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')]
        if auds:     # AAC in m4a first, and the ~130 kbps "medium" track: light to decode, plenty for this DAC
            audio = max(auds, key=lambda f: (f.get('language_preference') or 0,        # the original / default language, not an auto-dub
                                            (f.get('acodec') or '').startswith('mp4a'), f.get('ext') == 'm4a',
                                            'drc' not in str(f.get('format_id')),      # not the loudness-compressed copy
                                            -abs((f.get('tbr') or 130) - 130)))
    return (_source(video, info), (_source(audio, info) if audio else None),
            profiles.label_for(video['width'], video['height']), profiles.predicted_cost(video, cal, mult), why)


def _client(src):
    from urllib.parse import parse_qs, urlparse
    return '%s/%s' % (parse_qs(urlparse(src['url']).query).get('c', ['?'])[0], src.get('format_id'))


def media_from(info, formats, profile, quality='Auto', cal=None, mult=1.0, log=lambda m: None, max_cost=None, codec=None):
    video, audio, label, cost, why = choose_streams(info, formats, profile, quality, cal, mult, max_cost, codec)
    log('Streams: video=%s audio=%s ua=%s' % (_client(video), _client(audio) if audio else 'none',
                                              (video['headers'].get('User-Agent', '?'))[:50]))
    log('Selected %s: %sx%s %s fps %s kbps (%s), estimated load %d%%%s' % (
        label, video['width'], video['height'], video['fps'], round(video.get('tbr') or 0), video['format_id'],
        round(cost * 100), ('; ' + why) if why else ''))
    return Media(info.get('id', ''), safe_text(str(info.get('title', ''))), int(info.get('duration') or 0),
                 video, audio, label, (info, formats), why, cost)


def step_down(media, profile, cal=None, mult=1.0, log=lambda m: None):
    """The next lighter stream from the same catalogue (no yt-dlp run), or None if there is none."""
    if not media.catalog:
        return None
    cal = cal or profiles.CAL_DEFAULT
    cur = profiles.predicted_cost(media.video, cal, mult)
    smaller = media_from(media.catalog[0], media.catalog[1], profile, 'Auto', cal, mult, log,
                         max_cost=min(profiles.limit_for(cal), cur * 0.75))
    same = smaller.video['format_id'] == media.video['format_id']
    return None if same or smaller.cost >= cur else smaller


RESOLVE_TTL = 3 * 3600      # stream URLs are valid for about six hours; a 403 triggers a fresh resolve anyway
RESOLVE_KEEP = 40


def _resolve_file(rt, video_id):
    d = getattr(rt, 'resolve_cache', None)
    if d is None or not re.fullmatch(r'[A-Za-z0-9_-]{11}', str(video_id)):
        return None
    return Path(d) / (video_id + '.v2.out')           # v2: the saved details carry the audio language


def forget(rt, video_id):
    """Drop the saved resolve of a video (its stream URLs were refused)."""
    path = _resolve_file(rt, video_id)
    try:
        if path:
            path.unlink()
    except OSError:
        pass


def _cache_load(path):
    try:
        if path and time.time() - path.stat().st_mtime < RESOLVE_TTL:
            return path.read_bytes(), int((time.time() - path.stat().st_mtime) / 60)
    except OSError:
        pass
    return None, 0


def _cache_store(path, out):
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(out)
        os.replace(str(tmp), str(path))
        files = sorted(path.parent.glob('*.out'), key=lambda f: f.stat().st_mtime)
        for old in files[:-RESOLVE_KEEP]:
            old.unlink()
    except OSError:
        pass


def resolve(rt, profile, video_id, cancel, log, note=None, quality='Auto', cal=None, mult=1.0):
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    url = 'https://www.youtube.com/watch?v=' + video_id

    def on_line(text):
        if text.startswith('[debug]') and not re.search(r'player|js|challenge|solv|nsig|po token', text, re.I):
            return                                  # keep the log to the steps that show where the time goes
        if text.startswith(('[debug] Command-line', '[debug] Encodings', '[debug] Python', '[debug] Optional')):
            return
        log('yt-dlp: ' + text[:160])
        if note:
            note(text)
    cpath = _resolve_file(rt, video_id)
    raw, age = _cache_load(cpath)
    if raw is not None:
        log('Resolving %s: using the stream details saved %d min ago' % (video_id, age))
    else:
        log('Resolving %s (%s)' % (video_id, profile.key))
        raw = capture(resolve_command(rt, profile, url), env, cancel, timeout=300, on_line=on_line)     # past this, fail so the user can try again
    out = raw.decode('utf-8', 'replace')
    info = formats = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('{') and info is None:
            info = json.loads(line)
        elif line.startswith('[') and formats is None:
            formats = json.loads(line)
    if info is None or formats is None:
        forget(rt, video_id)
        raise ValueError('YouTube gave no playable stream (the video may be live, private or restricted).')
    _cache_store(cpath, raw)
    return media_from(info, formats, profile, quality, cal, mult, log)


DECODE_THREADS = 2                 # ffmpeg decoder threads for the video (the Speed test can pick another number for this MiSTer)


def decoder_command(rt, profile, source, kind, start_s=0, speed='Normal', limit_s=None, to_null=False, crop='Off',
                    decode_only=False, target=None, threads=None):
    args = [str(rt.ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-threads', str((threads or DECODE_THREADS) if kind == 'video' else 1), '-filter_threads', '1', '-rw_timeout', '20000000',
            '-protocol_whitelist', 'http,https,tcp,tls,crypto']
    if source['url'].startswith('https://'):
        args += ['-tls_verify', '1', '-ca_file', str(rt.ca)]
    if kind == 'video' and source['url'].startswith(('http://', 'https://')):   # a dropped video connection: reconnect (audio is reopened instead)
        args += ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_at_eof', '1', '-reconnect_delay_max', '5']
    if source['headers']:
        args += ['-headers', ''.join(k + ': ' + v + '\r\n' for k, v in source['headers'].items())]
    if kind == 'video':
        args += profiles.decode_speed_args(speed)
    if kind == 'video':
        args += ['-probesize', '200000', '-analyzeduration', '500000']      # the mp4 index says all we need: start sooner
    if start_s > 0:
        args += ['-ss', '%.3f' % start_s]
    args += ['-i', source['url']]
    if limit_s:
        args += ['-t', str(limit_s)]
    if kind == 'video' and target is not None:
        # v0.15 core: planar YUV at the picture's own size (the FPGA scales and converts colour). The crop only trims the
        # width to a multiple of 16 and the height to an even number; `target['prescale']` is set when the core could not
        # step through the picture fast enough.
        args[args.index('-loglevel') + 1] = 'info'                    # the stream line says which colour matrix it uses
        args.insert(args.index('-nostdin') + 1, '-nostats')
        vf = 'fps=%d:start_time=0' % profile.fps
        if target.get('prescale'):
            vf += ',scale=%d:%d:flags=fast_bilinear' % target['prescale']
        else:
            vf += r',crop=iw-mod(iw\,16):ih-mod(ih\,2)'
        args += ['-map', '0:v:0', '-an', '-vf', vf, '-pix_fmt', 'yuv420p', '-threads', '1', '-f', 'rawvideo']
    elif kind == 'video' and decode_only:
        # Speed test only: just the decoder, with no scaling or colour conversion (what an FPGA scaler would leave to the ARM).
        args += ['-map', '0:v:0', '-an', '-vf', 'null', '-pix_fmt', 'yuv420p', '-threads', '1', '-f', 'rawvideo']
    elif kind == 'video':
        args += ['-map', '0:v:0', '-an', '-vf', profiles.video_filter(profile, crop=crop),
                 '-pix_fmt', 'rgb565le', '-threads', '1', '-f', 'rawvideo']
    else:
        args += ['-map', '0:a:0', '-vn', '-af', 'aresample=48000:async=1:first_pts=0',
                 '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', '-f', 's16le']
    if to_null:
        return args[:args.index('-f')] + ['-f', 'null', '-']
    return args + ['-blocksize', '32768', 'pipe:1']


hook_log = lambda msg: None      # the app points this at its log


class Decoders:
    """Drain both streams independently; userspace holds at most one A/V pair."""

    AUDIO_RESTARTS = 30

    def __init__(self, commands, video_bytes, audio_bytes, restart_audio=None, start_s=0.0, duration=0, split=False):
        self.limits = {'video': video_bytes, 'audio': audio_bytes}
        self.split = split                        # hand out (video, audio) instead of one concatenated record
        self.restart_audio = restart_audio        # callable(start_s) -> ffmpeg command: reopen audio where it stopped
        self.start_s, self.duration, self.out_records, self.restarts = start_s, duration, 0, 0
        self.audio_got = 0                        # audio bytes received since the audio ffmpeg was (re)started
        self.procs = {}
        self.data = {'video': bytearray(), 'audio': bytearray()}
        self.eof = {'video': False, 'audio': 'audio' not in commands}
        self.errors = {}
        try:
            for kind, cmd in commands.items():
                self.procs[kind] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
                if kind == 'video':
                    enlarge_pipe(self.procs[kind])
                self.errors[kind] = bytearray()
        except BaseException:
            self.close()
            raise

    def close(self):
        for proc in self.procs.values():
            stop_process(proc)

    def colour_flags(self, height):
        """Header flags (yuv.FLAG_*) for the video, from the stream line ffmpeg printed: matrix and range."""
        text = bytes(self.errors.get('video', b'')).decode(errors='replace')
        m = re.search(r'Video: [^\n]*?yuv\w*\(([^)]*)\)', text)
        tags = m.group(1) if m else ''
        if 'bt709' in tags:
            bt601 = False
        elif 'smpte170m' in tags or 'bt470bg' in tags:
            bt601 = True
        else:                                      # untagged: standard definition is 601, high definition 709
            bt601 = height <= 576
        return (yuv.FLAG_601 if bt601 else 0) | (yuv.FLAG_FULL_RANGE if re.search(r'\bpc\b|\bjpeg\b', tags) else 0)

    def next_record(self, health):
        limits = self.limits
        vb, ab = limits['video'], limits['audio']
        started = time.monotonic()
        while True:
            health()
            if self.eof['video'] and not self.data['video']:
                return None
            if self.eof['video'] and len(self.data['video']) != vb:
                raise RuntimeError('Decoder ended with a partial video frame.')
            if len(self.data['video']) == vb and (len(self.data['audio']) == ab or self.eof['audio']):
                if len(self.data['audio']) % 4:
                    raise RuntimeError('Decoder ended with a partial stereo sample.')
                audio = bytes(self.data['audio']).ljust(ab, b'\x00')
                if self.split:                        # the video buffer changes hands without a copy
                    record = (self.data['video'], audio)
                    self.data['video'] = bytearray()
                else:
                    record = bytes(self.data['video']) + audio
                    self.data['video'].clear()
                self.data['audio'].clear()
                self.out_records += 1
                return record
            if time.monotonic() - started > 45:
                raise RuntimeError('No complete video/audio record within 45 seconds. Network or decoder stalled.')
            readers = {}
            for kind, proc in self.procs.items():
                if not self.eof[kind] and len(self.data[kind]) < limits[kind]:
                    readers[proc.stdout.fileno()] = (kind, False)
                if not proc.stderr.closed:
                    readers[proc.stderr.fileno()] = (kind, True)
            ready, _, _ = select.select(list(readers), [], [], .05)
            for fd in ready:
                kind, is_error = readers[fd]
                buf = self.errors[kind] if is_error else self.data[kind]
                count = 4096 if is_error else limits[kind] - len(buf)
                chunk = os.read(fd, count)
                if is_error:
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) > 8192:
                            del buf[:-8192]
                    else:
                        self.procs[kind].stderr.close()
                elif chunk:
                    buf.extend(chunk)
                    if kind == 'audio':
                        self.audio_got += len(chunk)
                else:
                    code = self.procs[kind].wait(timeout=2)
                    if kind == 'audio' and self._audio_ended_early():
                        self._reopen_audio(code)
                        break
                    self.eof[kind] = True
                    if code:
                        message = safe_text(self.errors[kind].decode(errors='replace'))
                        raise RuntimeError('%s decoder failed (%d): %s' % (kind, code, message))


    def _audio_position(self):
        """Seconds of the video at which the audio we hold ends."""
        ab = self.limits['audio']
        return self.start_s + (self.out_records * ab + len(self.data['audio'])) / 192000.0   # 48 kHz stereo s16

    def _audio_ended_early(self):
        """The audio stopped but the video goes on: the connection was dropped, not the end of the file."""
        if not self.restart_audio or self.eof['video'] or not self.audio_got:      # no data at all: a real error (e.g. 403)
            return False
        return not self.duration or self._audio_position() < self.duration - 4

    def _reopen_audio(self, code):
        if self.restarts >= self.AUDIO_RESTARTS:
            raise RuntimeError('The audio stream kept ending early.')
        self.restarts += 1
        pos = self._audio_position()
        why = safe_text(self.errors['audio'].decode(errors='replace')).strip().replace(chr(10), ' | ')[-160:]
        hook_log('audio stream ended early (exit %s) at %.0f s; reopening it there (%d)%s' % (
            code, pos, self.restarts, (': ' + why) if why else ''))
        stop_process(self.procs['audio'])
        self.procs['audio'] = subprocess.Popen(self.restart_audio(pos), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self.errors['audio'] = bytearray()
        self.eof['audio'] = False
        self.audio_got = 0


class RecordPipeline:
    """One pipe reader thread, bounded A/V records, one main-thread DDR writer."""
    CAPACITY = 4

    def __init__(self, decoder, cancel=lambda: None):
        self.decoder = decoder
        self.cancel = cancel
        self.ready = queue.Queue(maxsize=self.CAPACITY)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._produce, name='youtube-pipe-reader', daemon=True)
        self.thread.start()

    def _check(self):
        if self.stopping.is_set():
            raise RuntimeError('Pipe reader stopped.')
        self.cancel()

    def _put(self, item):
        while not self.stopping.is_set():
            try:
                self.ready.put(item, timeout=.05)
                return
            except queue.Full:
                pass

    def _produce(self):
        try:
            while not self.stopping.is_set():
                record = self.decoder.next_record(self._check)
                self._put((record, None))
                if record is None:
                    return
        except Exception as exc:
            self._put((None, exc))

    def poll(self):
        """Non-blocking: ('record', bytes) | ('eof', None) | ('wait', None). Raises decoder errors."""
        try:
            record, error = self.ready.get_nowait()
        except queue.Empty:
            if not self.thread.is_alive():
                raise RuntimeError('Pipe reader exited without an end marker.')
            return 'wait', None
        if error is not None:
            raise error
        return ('eof', None) if record is None else ('record', record)

    def next_record(self, health):
        while True:
            health()
            kind, rec = self.poll()
            if kind == 'wait':
                time.sleep(.005)
                continue
            return rec

    def _finish_close(self):
        try:
            self.thread.join(timeout=5)
            self.decoder.close()
        except Exception:
            pass

    def close(self, wait=True):
        self.stopping.set()
        if not wait:
            # Seeking: kill the decoders at once and clean up in the background, so the
            # new position starts without waiting for the old ffmpeg to exit.
            for proc in self.decoder.procs.values():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
            threading.Thread(target=self._finish_close, daemon=True).start()
            return
        self.thread.join(timeout=5)
        self.decoder.close()
        if self.thread.is_alive():
            raise RuntimeError('Pipe reader did not stop within five seconds.')


def yuv_target(media, interlaced=True, big=False):
    """Size of the YUV pictures for this video: its own size trimmed to a multiple of 16 x 2, or a pre-scaled size when it
    is larger than the core's row buffers (640 x 480) or the core could not step through it fast enough.
    `big` = the v0.16 core is reading pictures from the framebuffer: pictures up to 864 x 480 go in unscaled (the FPGA does the shrinking,
    which ffmpeg needs half its time for on this ARM).
    -> {'w', 'h', 'prescale'}"""
    w, h = yuv.aligned_size(int(media.video['width']), int(media.video['height']))
    pre = None
    max_w = yuv.BIG_MAX_W if big else yuv.MAX_W
    if w > max_w or h > yuv.MAX_H:
        k = min(max_w / float(w), yuv.MAX_H / float(h))
        pre = yuv.aligned_size(int(w * k), int(h * k))
        w, h = pre
    steep = yuv.layout(w, h, 'Off', interlaced)['prescale']      # 'Off' has the smallest destination: the strictest case
    if steep:
        pre = steep
        w, h = steep
    return {'w': w, 'h': h, 'prescale': pre}


def open_pipeline(rt, profile, media, start_s, speed, cancel, crop='Off', target=None):
    """`target` (from yuv_target) selects the v0.15 core's YUV pictures instead of 320x240 RGB565."""
    commands = {'video': decoder_command(rt, profile, media.video, 'video', start_s, speed, crop=crop, target=target)}
    again = None
    if media.audio:
        commands['audio'] = decoder_command(rt, profile, media.audio, 'audio', start_s)
        again = lambda at: decoder_command(rt, profile, media.audio, 'audio', at)
    vbytes = target['w'] * target['h'] * 3 // 2 if target else profile.video_bytes
    return RecordPipeline(Decoders(commands, vbytes, profile.audio_bytes, again, start_s, media.duration, split=bool(target)),
                          cancel)


def _bench_wall(rt, profile, media, speed, seconds, cancel, decode_only, target=None, threads=None):
    """Wall-clock seconds ffmpeg needs to decode `seconds` of the video into the void."""
    cmd = decoder_command(rt, profile, media.video, 'video', start_s=min(30, max(0, media.duration // 3)),
                          speed=speed, limit_s=seconds, to_null=True, decode_only=decode_only, target=target, threads=threads)
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
        while proc.poll() is None:
            cancel()
            if time.monotonic() - started > seconds * 6 + 30:
                raise RuntimeError('Benchmark timed out.')
            time.sleep(.1)
        wall = time.monotonic() - started
        if proc.returncode:
            raise RuntimeError('ffmpeg failed: ' + safe_text(proc.stderr.read().decode(errors='replace'))[-120:])
    finally:
        stop_process(proc)
    return wall


def benchmark(rt, profile, media, speed, seconds=10, cancel=lambda: None, decode_only=False, target=None, threads=None):
    """How many times faster than real time the ARM decodes this stream (>= 1.0x keeps up).

    ffmpeg needs a few seconds just to start and open the stream, which made short tests read about 25% too slow.
    Two runs of different lengths cancel that out: the rate is the extra video divided by the extra time.
    decode_only skips the scaling and colour conversion, to show how much of the time they take."""
    short = max(3.0, seconds * 0.4)
    wall_short = _bench_wall(rt, profile, media, speed, short, cancel, decode_only, target, threads)
    wall_long = _bench_wall(rt, profile, media, speed, seconds, cancel, decode_only, target, threads)
    extra = wall_long - wall_short
    if extra > 0.25:                                    # a sane difference: use the slope
        return min(10.0, (seconds - short) / extra)
    return seconds / wall_long                          # too close to call (e.g. network noise): the plain figure
