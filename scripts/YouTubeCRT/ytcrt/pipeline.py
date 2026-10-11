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
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import profiles


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
        self.ffmpeg = self.ffdir / 'ffmpeg'
        self.ca = Path(here) / 'cacert.pem'

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


def capture(command, env, cancel, timeout=300, on_line=None):
    proc = subprocess.Popen([str(x) for x in command], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=env)
    chunks = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    active = list(chunks)
    deadline = time.monotonic() + timeout
    pending = bytearray()
    try:
        while active:
            cancel()
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
        stop_process(proc)


# Print only what the player needs, as two lines. Past livestreams carry thousands of segment URLs
# per format, so a full --dump-single-json can exceed 20 MB; listing just these keys per format keeps
# the output small (about 65 KB for a normal video, 13 KB for a past livestream).
PRINT_INFO = '%(.{id,title,duration,is_live,live_status,http_headers})j'
PRINT_FORMATS = '%(formats.:.{format_id,protocol,vcodec,acodec,width,height,fps,tbr,ext,url,http_headers})j'
LIVE_MESSAGE = 'This is a live or past livestream; those cannot be played here yet.'


def resolve_command(rt, profile, url):
    return [rt.resolver, '--ignore-config', '--no-playlist', '--no-progress',
            '--no-cache-dir', '--skip-download', '--print', PRINT_INFO, '--print', PRINT_FORMATS,
            '--extractor-args', 'youtube:skip=translated_subs',
            '--socket-timeout', '20', '--retries', '1', '--fragment-retries', '1',
            '--js-runtimes', 'quickjs:' + str(rt.qjs), '--compat-options', 'no-certifi',
            '--match-filters', '!is_live & !is_upcoming', '--ffmpeg-location', str(rt.ffdir), '--', url]


PRINT_UPLOAD = '%(.{id,title,channel,duration,view_count,live_status,url})j'


def channel_uploads(rt, channel_id, cancel=lambda: None, log=lambda m: None, limit=15):
    """A channel's newest uploads straight from YouTube via yt-dlp (no RSS). Newest first, no dates.

    Used when YouTube's RSS feed is failing. The uploads playlist of channel UCxxxx is UUxxxx."""
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    cmd = [rt.resolver, '--ignore-config', '--no-cache-dir', '--flat-playlist', '--playlist-end', str(limit),
           '--socket-timeout', '20', '--retries', '1', '--compat-options', 'no-certifi',
           '--print', PRINT_UPLOAD, '--', 'https://www.youtube.com/playlist?list=UU' + channel_id[2:]]
    out = capture(cmd, env, cancel, timeout=120).decode('utf-8', 'replace')
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


def playlist_entries(rt, playlist_id, cancel=lambda: None, log=lambda m: None, limit=200, start=1):
    """Up to `limit` videos of a playlist from position `start` (1 = the first), in playlist order, via yt-dlp.
    -> (playlist title, [video dicts]). A channel's uploads are the playlist 'UU' + channel id without 'UC'."""
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    cmd = [rt.resolver, '--ignore-config', '--no-cache-dir', '--flat-playlist',
           '--playlist-start', str(start), '--playlist-end', str(start + limit - 1),
           '--socket-timeout', '20', '--retries', '1', '--compat-options', 'no-certifi',
           '--print', PRINT_PLAYLIST, '--', 'https://www.youtube.com/playlist?list=' + playlist_id]
    out = capture(cmd, env, cancel, timeout=180).decode('utf-8', 'replace')
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


def choose_streams(info, formats, profile, quality='Auto', cal=None, mult=1.0, max_cost=None):
    """Pick the video and audio stream for a video.

    quality '240p' / '360p' / '480p': the largest stream that fits that size on both sides (highest bitrate first),
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
    pool = [f for f in vids if (f.get('vcodec') or '').startswith('avc1')] or vids
    box = profiles.QUALITY_BOX.get(quality, 854)
    fit = [f for f in pool if f['height'] <= box and f['width'] <= box] or [min(pool, key=_area)]
    fit = [f for f in fit if min(f['width'], f['height']) >= 240] or fit          # never drop below 240p if 240p exists
    fit = [f for f in fit if f.get('acodec') in (None, 'none')] or fit            # video-only first (audio is separate)
    rank = sorted(fit, key=lambda f: (-_area(f), 0 if (f.get('fps') or 30) <= profile.fps else 1, -(f.get('tbr') or 0)))
    why = ''
    if quality == 'Auto':
        limit = profiles.LIMIT if max_cost is None else max_cost
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
            audio = max(auds, key=lambda f: ((f.get('acodec') or '').startswith('mp4a'), f.get('ext') == 'm4a',
                                            'drc' not in str(f.get('format_id')),      # not the loudness-compressed copy
                                            -abs((f.get('tbr') or 130) - 130)))
    return (_source(video, info), (_source(audio, info) if audio else None),
            profiles.label_for(video['width'], video['height']), profiles.predicted_cost(video, cal, mult), why)


def _client(src):
    from urllib.parse import parse_qs, urlparse
    return '%s/%s' % (parse_qs(urlparse(src['url']).query).get('c', ['?'])[0], src.get('format_id'))


def media_from(info, formats, profile, quality='Auto', cal=None, mult=1.0, log=lambda m: None, max_cost=None):
    video, audio, label, cost, why = choose_streams(info, formats, profile, quality, cal, mult, max_cost)
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
                         max_cost=min(profiles.LIMIT, cur * 0.75))
    same = smaller.video['format_id'] == media.video['format_id']
    return None if same or smaller.cost >= cur else smaller


def resolve(rt, profile, video_id, cancel, log, note=None, quality='Auto', cal=None, mult=1.0):
    env = os.environ.copy()
    env.update(SSL_CERT_FILE=str(rt.ca), REQUESTS_CA_BUNDLE=str(rt.ca), CURL_CA_BUNDLE=str(rt.ca))
    url = 'https://www.youtube.com/watch?v=' + video_id

    def on_line(text):
        log('yt-dlp: ' + text[:160])
        if note:
            note(text)
    log('Resolving %s (%s)' % (video_id, profile.key))
    out = capture(resolve_command(rt, profile, url), env, cancel, on_line=on_line).decode('utf-8', 'replace')
    info = formats = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('{') and info is None:
            info = json.loads(line)
        elif line.startswith('[') and formats is None:
            formats = json.loads(line)
    if info is None or formats is None:
        raise ValueError('YouTube gave no playable stream (the video may be live, private or restricted).')
    return media_from(info, formats, profile, quality, cal, mult, log)


def decoder_command(rt, profile, source, kind, start_s=0, speed='Normal', limit_s=None, to_null=False, crop='Off'):
    args = [str(rt.ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-threads', '2', '-filter_threads', '1', '-rw_timeout', '20000000',
            '-protocol_whitelist', 'http,https,tcp,tls,crypto']
    if source['url'].startswith('https://'):
        args += ['-tls_verify', '1', '-ca_file', str(rt.ca)]
    if source['url'].startswith(('http://', 'https://')):          # a long video's connection may be dropped midway
        args += ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_at_eof', '1', '-reconnect_delay_max', '5']
    if source['headers']:
        args += ['-headers', ''.join(k + ': ' + v + '\r\n' for k, v in source['headers'].items())]
    if kind == 'video':
        args += profiles.decode_speed_args(speed)
    if start_s > 0:
        args += ['-ss', '%.3f' % start_s]
    args += ['-i', source['url']]
    if limit_s:
        args += ['-t', str(limit_s)]
    if kind == 'video':
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

    def __init__(self, commands, video_bytes, audio_bytes, restart_audio=None, start_s=0.0, duration=0):
        self.limits = {'video': video_bytes, 'audio': audio_bytes}
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
                self.errors[kind] = bytearray()
        except BaseException:
            self.close()
            raise

    def close(self):
        for proc in self.procs.values():
            stop_process(proc)

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
                record = bytes(self.data['video']) + bytes(self.data['audio']).ljust(ab, b'\x00')
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
        hook_log('audio stream ended early (exit %s) at %.0f s; reopening it there (%d)' % (code, pos, self.restarts))
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


def open_pipeline(rt, profile, media, start_s, speed, cancel, crop='Off'):
    commands = {'video': decoder_command(rt, profile, media.video, 'video', start_s, speed, crop=crop)}
    again = None
    if media.audio:
        commands['audio'] = decoder_command(rt, profile, media.audio, 'audio', start_s)
        again = lambda at: decoder_command(rt, profile, media.audio, 'audio', at)
    return RecordPipeline(Decoders(commands, profile.video_bytes, profile.audio_bytes, again, start_s, media.duration),
                          cancel)


def benchmark(rt, profile, media, speed, seconds=10, cancel=lambda: None):
    """Decode `seconds` of video into the void; returns achieved/real-time fps.

    >= 1.0x means the ARM keeps up at this profile and speed setting."""
    cmd = decoder_command(rt, profile, media.video, 'video', start_s=min(30, max(0, media.duration // 3)),
                          speed=speed, limit_s=seconds, to_null=True)
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
    return seconds / wall
