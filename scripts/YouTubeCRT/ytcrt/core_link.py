"""One long-lived streaming session with the FPGA core.

The handshake and health checks are the ones validated in url_player.py
(v0.13.7); here the session stays open so the menu and the videos share it.
`FakeLink` stands in for the core on a PC.
"""
import mmap
import os
import secrets
import time

from . import transport as t

SLOTS = t.SLOTS
RECORD_BYTES = t.RECORD_BYTES
FRAME_SECONDS = t.FRAME_SECONDS


class Cancelled(RuntimeError):
    pass


class Link:
    def __init__(self, log, cancel):
        self.log = log
        self.cancel = cancel            # callable; raises Cancelled
        self.sent = 0
        self.consumed = 0
        self.session = None
        self.fd = None
        self.memory = None
        self.box = None
        self.last_progress = time.monotonic()
        self.heart = 0
        self.heart_time = time.monotonic()
        self.ident = t.IDENT
        self._probe = None
        self.settle_timeout = 5
        self.heart_timeout = 0.5       # FPGA heartbeat is ~kHz; stop writing fast if it stops
        self.copy_total = 0.0
        self.copy_max = 0.0
        self.copy_count = 0

    # lifecycle --------------------------------------------------------
    def open(self):
        t.check_layout()
        t.load_native()
        self.fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
        self.memory = mmap.mmap(self.fd, t.MAP_BYTES, flags=mmap.MAP_SHARED,
                                prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=t.BASE)
        self.box = t.Mailbox(self.memory)

    def _wait(self, test, timeout, message):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.cancel()
            if test():
                return
            time.sleep(.01)
        raise RuntimeError(message)

    def core_alive(self):
        """Read-only probe: is a v0.14-auto core loaded and its heartbeat ticking?"""
        ident, heart = self.box.read(32), self.box.read(36)
        ok = ident == t.IDENT and self._probe is not None and heart != self._probe
        self._probe = heart if ident == t.IDENT else None
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
                self.log('Waiting: identity word %08x (want %08x), heartbeat %d' %
                         (ident, t.IDENT, self.box.read(36)))
                last_note, last_ident = now, ident
            if end is not None and time.monotonic() > end:
                raise RuntimeError('No live v0.14-auto core detected.')
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

        A stop command makes the v0.14-auto core drop any latched error and
        stale session, so we clear our control words, confirm the core reports
        idle with no error, and only then publish a fresh session.
        """
        box = self.box
        box.command(0)
        self._heartbeats(3, 5, 'Core did not acknowledge stop.')
        end = time.monotonic() + self.settle_timeout
        while not (box.read(20) == 0 and box.read(28) == 0):
            self.cancel()
            if time.monotonic() > end:
                raise RuntimeError('Core did not return to idle (error flag stuck).')
            time.sleep(.005)
        box.write(8, 0)
        box.write(12, 0)
        self.sent = self.consumed = 0
        self.session = secrets.randbelow(0xfffffffe) + 1
        box.command(self.session)
        self._wait(lambda: box.read(20) == self.session, 5, 'Core refused the new session.')
        self.heart, self.heart_time = box.read(36), time.monotonic()
        self.last_progress = time.monotonic()
        self.log('Session established.')

    def end_session(self):
        """Stop our session if (and only if) our core is still running it."""
        try:
            if self.box and self.session is not None and self.box.read(32) == t.IDENT \
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
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    # streaming --------------------------------------------------------
    def health(self):
        self.cancel()
        box, now = self.box, time.monotonic()
        if box.read(32) != t.IDENT:
            raise RuntimeError('Core identity changed. Playback stopped.')
        h = box.read(36)
        if h != self.heart:
            self.heart, self.heart_time = h, now
        if now - self.heart_time > self.heart_timeout:
            raise RuntimeError('Core heartbeat stopped (core unloaded or streaming off).')
        cur = box.status(self.session)
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

    def can_push(self, depth=SLOTS):
        self.health()
        return self.queued < depth

    def push(self, record):
        """Copy one 160000-byte A/V record into the next slot and publish it."""
        if self.sent >= 0xffffff00:
            raise RuntimeError('Session record limit reached.')
        started = time.monotonic()
        self.box.record(self.sent, record)
        took = time.monotonic() - started
        self.copy_total += took
        self.copy_count += 1
        self.copy_max = max(self.copy_max, took)
        if self.sent - self.consumed == 3:
            self.last_progress = time.monotonic()
        self.sent += 1
        self.box.write(8, self.sent)

    def finish(self):
        self.box.write(12, self.sent)

    def measure_copy(self, n=12):
        """Time n record copies into the NEXT slot without publishing it (the core never reads an unpublished slot).
        Call from the thread that pushes, so nothing else writes that slot meanwhile. -> (average ms, worst ms)."""
        data = bytes(RECORD_BYTES)
        times = []
        for _ in range(n):
            started = time.monotonic()
            self.box.record(self.sent, data)
            times.append(time.monotonic() - started)
        return 1000.0 * sum(times) / len(times), 1000.0 * max(times)


class FakeLink:
    """Consumes records at 30 fps in software; used by the simulator/tests.

    Like the real core it does not start consuming until PREFILL records are
    queued, so a producer that queues fewer never gets a picture.
    """
    ident = t.IDENT
    PREFILL = 4

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

    def can_push(self, depth=SLOTS):
        return self.queued < depth

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
