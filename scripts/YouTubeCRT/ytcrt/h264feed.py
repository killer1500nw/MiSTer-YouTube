"""Feeds a video to the MiSTer H264 core: ffmpeg repackages the H.264 (no decoding) and decodes the audio; both go into the ring.

    video: ffmpeg -c:v copy -f mpegts   ->  TsVideo  ->  one VIDEO_AU record per access unit, with its 90 kHz presentation time
    audio: ffmpeg -> PCM 48 kHz stereo  ->  AUDIO records of 100 ms, each with its time

Both streams use the source's own time line (`-copyts`). A seek lands on the keyframe before the wanted position; the video's first
time stamp says where that is, and the audio is then started at exactly that time, so the two begin together (the picture starts a
little before the requested position, never after).
"""
import queue
import subprocess
import threading
import time

from . import h264ring as hr
from .tsdemux import TsVideo

CLOCK = 90000
AUDIO_CHUNK_FRAMES = 4800                  # 100 ms: 256 records of 20 ms would be only 5 s of the ring
AUDIO_RUNWAY = int(0.6 * CLOCK)               # the sound is written at least this far ahead of the picture it goes with (and at most AUDIO_LEAD):
                                           # the core reads the ring in order, and sound that sits right behind its picture reaches it too late
AUDIO_LEAD = CLOCK                         # write audio only up to one second ahead of the newest video written
AHEAD = 8 * CLOCK                          # nothing is written more than 8 s ahead of what the core is playing (YouTube resets a connection that sits idle)
AUDIO_NUDGES = (1.2, 3.0, -2.0, 6.0)         # where to try again (seconds from the start that crashed): this ffmpeg build segfaults when it seeks to
                                           # certain positions of a stream, and a second further on works
AUDIO_START_WAIT = 12.0                    # seconds to wait for the first sound before starting the picture without it
START_AHEAD = 3 * CLOCK                    # ... or, before the core's clock runs, 3 s past the first picture
VIDEO_QUEUE = 90
AUDIO_QUEUE = 60
AUDIO_PREROLL = 40                         # chunks (4 s) of sound to have in hand before the picture starts: YouTube serves audio at only about
                                           # twice real time after its first burst, and a start with almost nothing buffered drops sound


def _input_args(rt, source, kind):
    args = ['-rw_timeout', '20000000', '-protocol_whitelist', 'file,http,https,tcp,tls,crypto']
    if source['url'].startswith('https://'):
        args += ['-tls_verify', '1', '-ca_file', str(rt.ca)]
    if kind == 'video' and source['url'].startswith(('http://', 'https://')):
        args += ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_at_eof', '1', '-reconnect_delay_max', '5']
    if source.get('headers'):
        args += ['-headers', ''.join(k + ': ' + v + '\r\n' for k, v in source['headers'].items())]
    return args


def video_copy_command(rt, source, start_s=0.0):
    """ffmpeg: the H.264 stream, unchanged, as MPEG-TS with the source's own time stamps."""
    args = [str(rt.ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin'] + _input_args(rt, source, 'video')
    args += ['-probesize', '200000', '-analyzeduration', '500000', '-copyts']
    if start_s > 0:
        args += ['-ss', '%.3f' % start_s]
    return args + ['-i', source['url'], '-map', '0:v:0', '-c:v', 'copy', '-an', '-muxdelay', '0', '-muxpreload', '0',
                   '-avoid_negative_ts', 'disabled', '-f', 'mpegts', '-flush_packets', '1', 'pipe:1']


def audio_pcm_command(rt, source, start_s=0.0):
    """ffmpeg: the audio as 48 kHz 16-bit stereo PCM, starting at `start_s`."""
    args = [str(rt.ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin'] + _input_args(rt, source, 'audio')
    if start_s > 0:
        args += ['-ss', '%.3f' % start_s]
    return args + ['-i', source['url'], '-map', '0:a:0', '-vn', '-af', 'aresample=48000:async=1:first_pts=0',
                   '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']


class Feeder:
    """One playback. `video_cmd` / `audio_cmd` are functions of the start time returning an ffmpeg command line (audio may be None)."""

    AUDIO_RESTARTS = 30

    def __init__(self, ring, video_cmd, audio_cmd=None, log=print, spawn=subprocess.Popen, clock=time.monotonic, duration_s=0):
        self.duration_s = duration_s              # the video's length: audio that ends before it is reopened where it stopped
        self.audio_restarts = 0
        self.lead = {'audio': [None, 0, 0], 'video': [None, 0, 0]}   # how far ahead of the core's clock records were written: min ms, sum, count
        self.stc = 0
        self.ring, self.video_cmd, self.audio_cmd, self.log, self.spawn, self.clock = ring, video_cmd, audio_cmd, log, spawn, clock
        self.procs = {}
        self.vq = queue.Queue(VIDEO_QUEUE)
        self.aq = queue.Queue(AUDIO_QUEUE)
        self.threads = []
        self.stopping = threading.Event()
        self.first_pts = None                    # where the picture starts (90 kHz, the source's time line)
        self.last_pts = 0
        self.errors = []
        self.stats = {'au': 0, 'audio': 0, 'video_bytes': 0}

    # readers ---------------------------------------------------------------------------------------------------------------
    def _spawn(self, kind, cmd):
        proc = self.spawn(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.procs[kind] = proc
        t = threading.Thread(target=self._drain_errors, args=(kind, proc), daemon=True)
        t.start()
        return proc

    def _drain_errors(self, kind, proc):
        try:
            data = proc.stderr.read()
        except (OSError, ValueError):
            return
        if data:
            self.errors.append((kind, data.decode('utf-8', 'replace')[-400:]))

    def _put(self, q, item):
        while not self.stopping.is_set():
            try:
                q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _read_video(self, proc):
        demux = TsVideo()
        try:
            while not self.stopping.is_set():
                data = proc.stdout.read(188 * 128)
                if not data:
                    break
                for pts, dts, au in demux.feed(data):
                    if pts is None:
                        continue
                    if not self._put(self.vq, (pts, au)):
                        return
            for pts, dts, au in demux.flush():
                if pts is not None:
                    self._put(self.vq, (pts, au))
        except (OSError, ValueError):
            pass
        finally:
            self._put(self.vq, None)                     # the end

    def _read_audio(self, proc, start_pts):
        frames = 0                                   # frames read from the process now running
        buf = bytearray()
        want = AUDIO_CHUNK_FRAMES * 4
        failures, fail_base = 0, None
        try:
            while not self.stopping.is_set():
                data = proc.stdout.read(want)
                if data:
                    failures, fail_base = 0, None
                    buf += data
                    while len(buf) >= want:
                        pts = start_pts + frames * CLOCK // 48000
                        if not self._put(self.aq, (pts, bytes(buf[:want]))):
                            return
                        del buf[:want]
                        frames += AUDIO_CHUNK_FRAMES
                    continue
                end_s = (start_pts + frames * CLOCK // 48000) / float(CLOCK)
                if self.stopping.is_set():
                    break
                try:
                    code = proc.wait(timeout=1)
                except Exception:
                    code = '?'
                if frames == 0 and not buf:
                    # It produced nothing at all: ffmpeg crashes at some seek positions. Try a little before or after instead.
                    failures += 1
                    fail_base = end_s if fail_base is None else fail_base
                    if failures > len(AUDIO_NUDGES):
                        self.log('The audio cannot be started near %.1f s (ffmpeg exit %s); playing without it' % (fail_base, code))
                        break
                    target = max(0.0, fail_base + AUDIO_NUDGES[failures - 1])
                    self.log('The audio cannot start at %.1f s (ffmpeg exit %s); trying %.1f s (%d of %d)' % (
                        end_s, code, target, failures, len(AUDIO_NUDGES)))
                    time.sleep(0.3)
                elif (self.duration_s and end_s < self.duration_s - 3.0 and self.audio_restarts < self.AUDIO_RESTARTS):
                    # YouTube's audio connection can end early (exit 0, a few seconds before the end): carry on from there
                    self.audio_restarts += 1
                    target = end_s
                    self.log('The audio ended early at %.1f s of %d; reopening it there (%d)' % (end_s, self.duration_s, self.audio_restarts))
                else:
                    break
                try:
                    proc.kill()
                except (OSError, AttributeError):
                    pass
                proc = self._spawn('audio', self.audio_cmd(target))
                buf.clear()
                start_pts, frames = int(target * CLOCK), 0
            tail = len(buf) - len(buf) % 4
            if tail > 0 and not self.stopping.is_set():
                self._put(self.aq, (start_pts + frames * CLOCK // 48000, bytes(buf[:tail])))
        except (OSError, ValueError):
            pass
        finally:
            self._put(self.aq, None)

    def start(self, start_s=0.0):
        proc = self._spawn('video', self.video_cmd(start_s))
        t = threading.Thread(target=self._read_video, args=(proc,), daemon=True)
        t.start()
        self.threads.append(t)

    def _start_audio(self, first_pts):
        if self.audio_cmd is None:
            self._put(self.aq, None)
            return
        start_s = first_pts / float(CLOCK)
        proc = self._spawn('audio', self.audio_cmd(start_s))
        t = threading.Thread(target=self._read_audio, args=(proc, first_pts), daemon=True)
        t.start()
        self.threads.append(t)

    # writing ---------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _get(q):
        try:
            return q.get_nowait()
        except queue.Empty:
            return _EMPTY

    def run(self, stop=lambda: False, tick=lambda: None, first_timeout=60.0):
        """Write the video and audio into the ring until both have ended, then wait for the core to show the last picture.

        -> 'ended' or 'stopped'; raises hr.RingError / RuntimeError when something fails."""
        ring = self.ring
        try:
            first = self._wait_first(stop, first_timeout)
            if first is None:
                return 'stopped'
            self.first_pts = first[0]
            self._start_audio(first[0])
            v_held, v_next, v_max, v_done = first, _EMPTY, first[0], False
            a_held, a_eof = None, False
            # The picture starts the core's clock; sound that turns up afterwards is late and the core drops it. So nothing is written
            # until the first sound is ready (its ffmpeg needs a few seconds to connect), and then both start together.
            audio_slow = False
            if self.audio_cmd is not None:
                waited = self.clock()
                while not stop():
                    queued = list(self.aq.queue)
                    if len(queued) >= AUDIO_PREROLL or (queued and queued[-1] is None):
                        break                                       # enough sound in hand, or all of it (a short clip)
                    if self.clock() - waited > AUDIO_START_WAIT:
                        self.log('Only %d of %d sound chunks within %.0f s; starting the picture now' % (
                            len([q for q in queued if q is not None]), AUDIO_PREROLL, AUDIO_START_WAIT))
                        audio_slow = True
                        break
                    tick()
                    ring.wait(0.01)
                else:
                    return 'stopped'
            horizon, polled = first[0] + START_AHEAD, 0.0
            a_max, audio_optional = None, self.audio_cmd is None            # the newest sound written; True when the picture need not wait for sound
            if self.audio_cmd is not None and a_eof:
                audio_optional = True
            while not stop():
                progressed = False
                if self.clock() - polled > 0.04:
                    polled = self.clock()
                    stc = ring.status()['stc']
                    self.stc = stc
                    horizon = (first[0] + START_AHEAD) if stc < first[0] - 2 * CLOCK else stc + AHEAD
                if not v_done:
                    if v_next is _EMPTY:
                        v_next = self._get(self.vq)
                    # The sound goes in first, by a runway; but the picture never waits for sound that is itself waiting for the picture
                    runway_ok = (audio_optional or (a_max is not None and v_held[0] <= a_max - AUDIO_RUNWAY)
                                 or (a_held is not None and a_held[0] > v_max + AUDIO_LEAD))
                    if v_next is not _EMPTY and v_held[0] <= horizon and runway_ok:   # now we know whether v_held is the last access unit
                        flags = hr.F_END_OF_STREAM if v_next is None else 0
                        if ring.try_write(hr.T_VIDEO_AU, v_held[1], v_held[0], flags):
                            self._lead('video', v_held[0])
                            self.stats['au'] += 1
                            self.stats['video_bytes'] += len(v_held[1])
                            v_max = max(v_max, v_held[0])
                            self.last_pts = v_max
                            if v_next is None:
                                v_done, v_held = True, None
                            else:
                                v_held, v_next = v_next, _EMPTY
                            progressed = True
                if a_held is None and not a_eof:
                    item = self._get(self.aq)
                    if item is None:
                        a_eof = True
                    elif item is not _EMPTY:
                        a_held = item
                if a_eof or audio_slow:
                    audio_optional = True                                   # no more sound is coming (or it is too slow): do not hold the picture back
                if a_held is not None and a_held[0] <= horizon and (a_held[0] <= v_max + AUDIO_LEAD or v_done):
                    if ring.try_write(hr.T_AUDIO, a_held[1], a_held[0]):
                        a_max = a_held[0] + AUDIO_CHUNK_FRAMES * CLOCK // 48000
                        self._lead('audio', a_held[0])
                        self.stats['audio'] += 1
                        a_held = None
                        progressed = True
                if v_done and a_eof and a_held is None:
                    break
                if not progressed:
                    ring.wait(0.002)
                tick()
            else:
                return 'stopped'
            return self._drain(stop, tick)
        finally:
            self.close()

    def _lead(self, kind, pts):
        """Note how far ahead of the core's clock a record was written (negative: it was already late when it went in)."""
        if self.first_pts is None or self.stc < self.first_pts - 2 * CLOCK:
            return                                             # the clock is not running on this timeline yet
        ms = (pts - self.stc) // 90
        rec = self.lead[kind]
        rec[0] = ms if rec[0] is None else min(rec[0], ms)
        rec[1] += ms
        rec[2] += 1

    def take_lead(self):
        """-> {'audio': (min ms, mean ms) or None, 'video': ...} since the last call."""
        out = {}
        for kind in list(self.lead):
            lo, total, n = self.lead[kind]
            out[kind] = (lo, total // n) if n else None
            self.lead[kind] = [None, 0, 0]
        return out

    def _wait_first(self, stop, timeout):
        end = self.clock() + timeout
        while not stop():
            try:
                item = self.vq.get(timeout=0.2)
            except queue.Empty:
                if self.clock() > end:
                    raise RuntimeError('No video arrived within %.0f s.%s' % (timeout, self._why()))
                continue
            if item is None:
                raise RuntimeError('The video ended before the first picture.%s' % self._why())
            return item
        return None

    def _why(self):
        return (' ffmpeg said: ' + self.errors[-1][1].strip().splitlines()[-1]) if self.errors else ''

    def _drain(self, stop, tick, margin=1.0):
        """Everything is written. Wait until the core has shown the last picture (or the viewer stops)."""
        ring = self.ring
        end_pts = self.last_pts
        while not stop():
            st = ring.status()
            if st['state'] == 4 or (st['state'] == 3 and st['stc'] >= end_pts + 2 * CLOCK):
                return 'ended'           # (normally the core says ENDED once the last picture has been shown)
            if st['state'] == 5:
                raise hr.RingError('The core stopped: ' + ring.state_text(st))
            ring.wait(0.05)
            tick()
        return 'stopped'

    def close(self):
        self.stopping.set()
        for proc in self.procs.values():
            try:
                proc.kill()
            except (OSError, AttributeError):
                pass
        for proc in self.procs.values():
            try:
                proc.wait(timeout=2)
            except Exception:
                pass


_EMPTY = object()
