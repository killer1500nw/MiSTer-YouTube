"""One long-lived streaming session with the FPGA core.

The handshake and health checks are the ones validated in url_player.py
(v0.13.7); here the session stays open so the menu and the videos share it.
`FakeLink` / `FakeYuvLink` stand in for the core on a PC.

Two cores are supported: v0.14-auto (`kind == 'rgb'`: 160000-byte RGB565 + PCM records) and v0.15-yuv (`kind == 'yuv'`:
YUV pictures that the FPGA scales, an overlay layer for the GUI/OSD; see docs/FPGA-YUV-v0.15.md).
"""
import mmap
import os
import secrets
import subprocess
import sys
import time

from . import transport as t
from . import yuv

SLOTS = t.SLOTS
RECORD_BYTES = t.RECORD_BYTES
FRAME_SECONDS = t.FRAME_SECONDS
YUV_SLOTS = 6            # records the v0.15 player keeps queued: 8 slots minus the one on screen and one of margin
KINDS = {t.IDENT_RGB: 'rgb', t.IDENT_YUV: 'yuv'}


class Cancelled(RuntimeError):
    pass


class Link:
    def __init__(self, log, cancel):
        self.log = log
        self.cancel = cancel            # callable; raises Cancelled
        self.sent = 0
        self.consumed = 0               # records committed to the screen
        self.released = 0               # v0.15: records whose slot the core no longer reads
        self.session = None
        self.fd = None
        self.memory = None
        self.box = None
        self.last_progress = time.monotonic()
        self.heart = 0
        self.heart_time = time.monotonic()
        self.kind = 'rgb'
        self.ident = t.IDENT_RGB
        self.slots = SLOTS
        self.ov_ref = [-1] * yuv.OVERLAYS   # last record that referenced each overlay buffer
        self._probe = None
        self.settle_timeout = 5
        self.heart_timeout = 0.5       # FPGA heartbeat is ~kHz; stop writing fast if it stops
        self.copy_total = 0.0
        self.copy_max = 0.0
        self.copy_count = 0
        self.copy_bytes = 0
        self.copy_report = {}
        self._last_frame, self._last_at, self._last_size, self.repeats = None, -1, None, 0
        self.full_total, self.full_count = 0.0, 0         # copy time of the records that carried a whole picture

    # lifecycle --------------------------------------------------------
    def open(self):
        t.check_layout()
        t.load_native()
        self.fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
        self.memory = mmap.mmap(self.fd, t.MAP_BYTES, flags=mmap.MAP_SHARED,
                                prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=t.BASE)
        self.fd2 = self.cached_memory = None
        try:                                                     # the same memory without O_SYNC (cacheable if the kernel allows it)
            self.fd2 = os.open('/dev/mem', os.O_RDWR)
            self.cached_memory = mmap.mmap(self.fd2, t.MAP_BYTES, flags=mmap.MAP_SHARED,
                                           prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=t.BASE)
        except (OSError, ValueError):
            self.cached_memory = None
        self.box = t.Mailbox(self.memory, self.fd, self.cached_memory)

    def _wait(self, test, timeout, message):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.cancel()
            if test():
                return
            time.sleep(.01)
        raise RuntimeError(message)

    def _adopt(self, ident):
        self.ident = ident
        self.kind = KINDS[ident]
        self.slots = YUV_SLOTS if self.kind == 'yuv' else SLOTS

    def core_alive(self):
        """Read-only probe: is one of our cores loaded and its heartbeat ticking?"""
        ident, heart = self.box.read(32), self.box.read(36)
        known = ident in KINDS
        ok = known and self._probe is not None and heart != self._probe
        self._probe = heart if known else None
        if ok:
            self._adopt(ident)
        return ok

    def wait_for_core(self, timeout=None, poll=0.25, idle=None):
        """Block until the core is loaded. Writes nothing to memory."""
        self._probe = None
        end = None if timeout is None else time.monotonic() + timeout
        last_note, last_ident = 0.0, None
        while True:
            self.cancel()
            if self.core_alive():
                return
            if idle:
                idle()
            ident = self.box.read(32)
            now = time.monotonic()
            if ident != last_ident or now - last_note > 60:
                # Diagnostics only: what the mailbox looks like while we wait.
                self.log('Waiting: identity word %08x (want %08x or %08x), heartbeat %d' %
                         (ident, t.IDENT_YUV, t.IDENT_RGB, self.box.read(36)))
                last_note, last_ident = now, ident
            if end is not None and time.monotonic() > end:
                raise RuntimeError('No live YouTubeCRT core detected.')
            time.sleep(poll)

    def _heartbeats(self, n, timeout, message):
        """Wait for the core's heartbeat to advance n times (n poll cycles)."""
        seen, last = 0, self.box.read(36)
        end = time.monotonic() + timeout
        while seen < n:
            self.cancel()
            if time.monotonic() > end:
                raise RuntimeError(message)
            cur = self.box.read(36)
            if cur != last:
                seen, last = seen + 1, cur
            else:
                time.sleep(.002)

    def start_session(self):
        """Automatic handshake; the user does nothing.

        A stop command makes the core drop any latched error and
        stale session, so we clear our control words, confirm the core reports
        idle with no error, and only then publish a fresh session.
        """
        box = self.box
        box.command(0)
        self._heartbeats(3, 5, 'Core did not acknowledge stop.')
        end = time.monotonic() + self.settle_timeout
        while not (box.read(20) == 0 and (box.read(28) & 1) == 0):
            self.cancel()
            if time.monotonic() > end:
                raise RuntimeError('Core did not return to idle (error flag stuck).')
            time.sleep(.005)
        box.write(8, 0)
        box.write(12, 0)
        self.sent = self.consumed = self.released = 0
        self._last_frame, self._last_at = None, -1
        self.ov_ref = [-1] * yuv.OVERLAYS
        self.session = secrets.randbelow(0xfffffffe) + 1
        box.command(self.session)
        self._wait(lambda: box.read(20) == self.session, 5, 'Core refused the new session.')
        self.heart, self.heart_time = box.read(36), time.monotonic()
        self.last_progress = time.monotonic()
        self.log('Session established (%s core).' % self.kind)

    def end_session(self):
        """Stop our session if (and only if) our core is still running it."""
        try:
            if self.box and self.session is not None and self.box.read(32) == self.ident \
                    and self.box.read(20) == self.session:
                h = self.box.read(36)
                until = time.monotonic() + .2
                while time.monotonic() < until and self.box.read(36) == h:
                    time.sleep(.005)
                if self.box.read(36) != h:
                    self.box.command(0)
        finally:
            self.session = None

    def close(self):
        try:
            self.end_session()
        finally:
            if self.box:
                self.box.close()
                self.box = None
            if self.memory:
                self.memory.close()
                self.memory = None
            if getattr(self, 'cached_memory', None):
                try:
                    self.cached_memory.close()
                except (OSError, ValueError, BufferError):
                    pass
                self.cached_memory = None
            if getattr(self, 'fd2', None) is not None:
                os.close(self.fd2)
                self.fd2 = None
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    # streaming --------------------------------------------------------
    def health(self):
        self.cancel()
        box, now = self.box, time.monotonic()
        if box.read(32) != self.ident:
            raise RuntimeError('Core identity changed. Playback stopped.')
        h = box.read(36)
        if h != self.heart:
            self.heart, self.heart_time = h, now
        if now - self.heart_time > self.heart_timeout:
            raise RuntimeError('Core heartbeat stopped (core unloaded or streaming off).')
        cur = box.status(self.session)
        if self.kind == 'yuv':
            self.released = cur                 # the ack word: slots below this are free
            cur = box.read(24)                  # records committed to the screen
        if cur < self.consumed or cur > self.sent:
            raise RuntimeError('Invalid consumer sequence.')
        if cur != self.consumed:
            self.consumed, self.last_progress = cur, now
        if self.sent - self.consumed == 0:
            self.last_progress = now
        elif self.sent - self.consumed >= 4 and now - self.last_progress > 5:
            raise RuntimeError('Core stopped consuming a nonempty buffer.')

    @property
    def queued(self):
        return self.sent - self.consumed

    @property
    def interlaced(self):
        """v0.15: is the core showing 480i (else 640x240)? Published in the stats word."""
        return bool(self.box.read(28) & 2) if self.kind == 'yuv' else False

    def can_push(self, depth=None):
        self.health()
        return self.queued < (self.slots if depth is None else depth)

    def _published(self, started, full=True):
        took = time.monotonic() - started
        self.copy_total += took
        self.copy_count += 1
        if full:
            self.full_total += took
            self.full_count += 1
        self.copy_max = max(self.copy_max, took)
        if self.sent - self.consumed == 3:
            self.last_progress = time.monotonic()
        self.sent += 1
        self.box.write(8, self.sent)

    def push(self, record):
        """v0.14 core: copy one 160000-byte A/V record into the next slot and publish it."""
        if self.sent >= 0xffffff00:
            raise RuntimeError('Session record limit reached.')
        started = time.monotonic()
        self.box.record(self.sent, record)
        self._published(started)

    def push_picture(self, frame, audio, w, h, crop='Off', flags=0, overlay=None):
        """v0.15 core: publish a decoded w x h YUV 4:2:0 picture (`frame` = Y, U, V planes in one buffer) with its 6400 bytes
        of PCM. The core scales it; `overlay` is the index of an uploaded overlay buffer to draw over it, or None."""
        if self.sent >= 0xffffff00:
            raise RuntimeError('Session record limit reached.')
        started = time.monotonic()
        # An identical picture (a repeat frame from a 24/25 fps video, a paused video, a still scene) is not copied again: the
        # record names the slot that already holds it. Only the record right after a full one may do this, so the slot is
        # never rewritten while the repeat is on screen (8 slots, at most 6 queued).
        again = (self._last_frame is not None and self._last_at == self.sent - 1 and self._last_size == (w, h)
                 and (frame is self._last_frame or frame == self._last_frame))
        header = yuv.header_for(w, h, crop, self.interlaced, flags, overlay, (self.sent - 1) % 8 if again else None)
        if overlay is not None:
            self.ov_ref[overlay] = self.sent
        if again:
            self.box.picture_again(self.sent, header, audio)
            self.repeats += 1
            self._last_at = -1                                     # the next record must carry its own picture
        else:
            self.box.picture(self.sent, header, frame, w * h, audio)
            self._last_frame, self._last_at, self._last_size = frame, self.sent, (w, h)
        self._published(started, not again)

    def upload_overlay(self, index, y, u, v):
        """Write a finished overlay (320x240 planes, luma 0 = transparent) into buffer `index`, which must be free."""
        self.box.overlay(index, y, u, v)

    def free_overlay(self, keep=None):
        """Index of an overlay buffer no queued or displayed record uses (and that is not `keep`), or None."""
        for i in range(yuv.OVERLAYS):
            if i != keep and self.ov_ref[i] < self.released:
                return i
        return None

    def finish(self):
        self.box.write(12, self.sent)

    FAST_COPY_PROBE = """
import ctypes, mmap, os, sys, time
BASE, MAP, SIZE = 0x30000000, 0x201000, 160000
slot, n = int(sys.argv[1]), int(sys.argv[2])
fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
mm = mmap.mmap(fd, MAP, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=BASE)
base = ctypes.addressof(ctypes.c_char.from_buffer(mm))
dst = base + 0x1000 + slot * 0x40000
src = ctypes.create_string_buffer(bytes(range(256)) * 625, SIZE)
def check():
    return ctypes.string_at(dst, 64) == src.raw[:64] and ctypes.string_at(dst + SIZE - 64, 64) == src.raw[SIZE - 64:]
t = []
for _ in range(n):
    s = time.monotonic(); ctypes.memmove(dst, src, SIZE); t.append(time.monotonic() - s)
print('memmove %.2f %.2f %d' % (1000 * sum(t) / n, 1000 * max(t), check()))
t = []
for _ in range(n):
    s = time.monotonic(); mm[0x1000 + slot * 0x40000: 0x1000 + slot * 0x40000 + SIZE] = src.raw; t.append(time.monotonic() - s)
print('slice %.2f %.2f %d' % (1000 * sum(t) / n, 1000 * max(t), check()))
"""

    def measure_fast_copy(self, n=12):
        """Experiment for the Speed test (v0.14 core only; the v0.15 player always bulk-copies): would a bulk copy
        (libc memmove / mmap slice) into the same unpublished slot be faster than the helper's word-by-word stores, and is
        it intact? Runs in a separate process, so a bus error from an unsuitable access kills only that process.
        -> {method: (average ms, worst ms, intact)} or None."""
        if self.kind == 'yuv':
            return None
        try:
            out = subprocess.run([sys.executable, '-c', self.FAST_COPY_PROBE, str(self.sent % SLOTS), str(n)],
                                 capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode:
            return None
        res = {}
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) == 4:
                res[parts[0]] = (float(parts[1]), float(parts[2]), parts[3] == '1')
        return res or None

    def use_copy_method(self, method):
        if self.kind == 'yuv':
            self.box.use_method(method)

    YUV_TEST_BYTES = 640 * 360 * 3 // 2        # a typical picture: 640x360 YUV 4:2:0

    def measure_copy(self, n=12):
        """Time n copies of a typical picture into the NEXT slot without publishing it (the core never reads an unpublished
        slot). The v0.15 link tries both ways of copying (memmove into the mapping, pwrite to /dev/mem), keeps the faster
        and records every result in `copy_report`. Call from the thread that pushes, so nothing else writes that slot meanwhile.
        -> (average ms, worst ms) of the method in use."""
        def time_it(copy):
            times = []
            for _ in range(n):
                started = time.monotonic()
                copy()
                times.append(time.monotonic() - started)
            return 1000.0 * sum(times) / len(times), 1000.0 * max(times)
        if self.kind != 'yuv':
            data = bytearray(RECORD_BYTES)
            self.copy_bytes = RECORD_BYTES
            self.copy_report = {'record': time_it(lambda: self.box.record(self.sent, bytes(data)))}
            return self.copy_report['record']
        data = bytearray(self.YUV_TEST_BYTES)
        where = yuv.slot_offset(self.sent)
        ysize = 640 * 360
        view = memoryview(data)                               # the planes of a 640x360 picture, as picture() copies them
        planes = [(where + yuv.Y_OFF, view[:ysize]), (where + yuv.U_OFF, view[ysize:ysize + ysize // 4]),
                  (where + yuv.V_OFF, view[ysize + ysize // 4:])]
        self.copy_bytes = self.YUV_TEST_BYTES
        self.copy_report = {}
        keep = self.box.method
        for method in t.COPY_METHODS:
            self.box.use_method(method)
            if self.box.method == method:
                try:
                    result = time_it(lambda: self.box.copy_pieces(planes))
                    if self.box.method == method:          # (a cached copy that failed its check switches itself off)
                        self.copy_report[method] = result
                except (OSError, RuntimeError, ValueError):
                    pass                                   # this way of copying does not work on this system: leave it out
        best = min(self.copy_report, key=lambda m: self.copy_report[m][0]) if self.copy_report else keep
        self.box.use_method(best)
        return self.copy_report.get(best, (0.0, 0.0))


class FakeLink:
    """Consumes records at 30 fps in software; used by the simulator/tests (the v0.14 record format).

    Like the real core it does not start consuming until PREFILL records are
    queued, so a producer that queues fewer never gets a picture.
    """
    ident = t.IDENT
    kind = 'rgb'
    slots = SLOTS
    interlaced = False
    PREFILL = 4
    copy_bytes = 0
    copy_report = {}

    def use_copy_method(self, method):
        pass

    def __init__(self, log=print, cancel=lambda: None, sink=None):
        self.log, self.cancel, self.sink = log, cancel, sink
        self.sent = 0
        self.t0 = None
        self.copy_total = self.copy_max = 0.0
        self.copy_count = 0
        self.last = None

    def open(self):
        pass

    def start_session(self):
        pass

    def close(self):
        pass

    def health(self):
        self.cancel()

    @property
    def consumed(self):
        if self.t0 is None:
            return 0
        return min(self.sent, 1 + int((time.monotonic() - self.t0) / FRAME_SECONDS))

    @property
    def queued(self):
        return self.sent - self.consumed

    def can_push(self, depth=None):
        return self.queued < (self.slots if depth is None else depth)

    def push(self, record):
        if len(record) != RECORD_BYTES:
            raise ValueError('bad record size')
        if self.sink:
            self.sink(self.sent, record)
        self.last = record
        self.copy_count += 1
        self.sent += 1
        if self.t0 is None and self.sent >= self.PREFILL:
            self.t0 = time.monotonic()

    def finish(self):
        pass

    def measure_copy(self, n=12):
        return 0.0, 0.0

    def measure_fast_copy(self, n=12):
        return None


class FakeYuvLink(FakeLink):
    """The same for the v0.15 core: records pictures and overlays instead of RGB565 bytes."""
    ident = t.IDENT_YUV
    kind = 'yuv'
    slots = YUV_SLOTS

    def __init__(self, log=print, cancel=lambda: None, sink=None, interlaced=True):
        super().__init__(log, cancel, sink)
        self.interlaced = interlaced
        self.ov_ref = [-1] * yuv.OVERLAYS
        self.overlays = {}                       # index -> (y, u, v) as uploaded
        self.pictures = []                       # the last few (frame bytes, audio, w, h, crop, flags, overlay)
        self.frame_seconds = t.YUV_FRAME_SECONDS

    @property
    def consumed(self):
        if self.t0 is None:
            return 0
        return min(self.sent, 1 + int((time.monotonic() - self.t0) / self.frame_seconds))

    @property
    def released(self):
        return max(0, self.consumed - 1)

    def push(self, record):
        raise RuntimeError('the v0.15 core takes pictures, not RGB565 records')

    def push_picture(self, frame, audio, w, h, crop='Off', flags=0, overlay=None):
        header = yuv.header_for(w, h, crop, self.interlaced, flags, overlay)       # validates the geometry like the real link
        if len(audio) != yuv.AUDIO_BYTES or len(frame) != w * h * 3 // 2:
            raise ValueError('bad picture size')
        if overlay is not None:
            self.ov_ref[overlay] = self.sent
        entry = (bytes(frame), bytes(audio), w, h, crop, flags, overlay)
        self.pictures = (self.pictures + [entry])[-8:]
        if self.sink:
            self.sink(self.sent, entry)
        self.last = entry
        self.copy_count += 1
        self.sent += 1
        if self.t0 is None and self.sent >= self.PREFILL:
            self.t0 = time.monotonic()
        return header

    def upload_overlay(self, index, y, u, v):
        if self.ov_ref[index] >= self.released:
            raise RuntimeError('overlay buffer %d is still in use' % index)
        self.overlays[index] = (bytes(y), bytes(u), bytes(v))

    def free_overlay(self, keep=None):
        for i in range(yuv.OVERLAYS):
            if i != keep and self.ov_ref[i] < self.released:
                return i
        return None
