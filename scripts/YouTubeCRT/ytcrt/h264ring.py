"""The MiSTer H264 core's ring interface (version 1.1), spoken from Python.

The core (github.com/owenb321/MiSTer_h264_decoder, GPL-3.0-or-later; its client library is MIT) decodes H.264 in the FPGA. The ARM only
demultiplexes: it writes H.264 access units (Annex B, one per record, each with a 90 kHz presentation time) and PCM audio into a ring
in the FPGA's half of the DDR3, and reads a status block back. This module follows the interface's published layout, which is frozen
for every 1.x core: the control block at 0x31400000 (64-bit words, used here as 32-bit halves), the 8 MB ring at 0x31800000, records
of a 16-byte header plus a payload padded to 16 bytes, a write index that publishes them, and a nonce that starts a session.

Nothing here touches hardware by itself: `Ring` works on any writable buffer, which is /dev/mem on a MiSTer (see `open_devmem`) and a
plain bytearray in the tests.
"""
import ctypes
import mmap
import os
import time

CTRL_BYTE = 0x31400000
RING_BYTE = 0x31800000
RING_SIZE = 0x800000
REGION_LEN = RING_BYTE + RING_SIZE - CTRL_BYTE
RING_OFFSET = RING_BYTE - CTRL_BYTE            # the ring's offset inside the mapped region
MAGIC = 0x343632485453694D                      # "MiSTH264"
VERSION = 0x101                                 # 1.1: the same major, a core minor at least this
SYNC = 0x5248
MAX_PAYLOAD = 0x400000
LOCK_PATH = '/tmp/h264ring.lock'
MGL_PATH = '/tmp/h264ring.mgl'

T_PAD, T_VIDEO_AU, T_AUDIO = 1, 2, 3
F_PTS_VALID, F_DISCONTINUITY, F_END_OF_STREAM, F_LOW_LATENCY, F_GRAPHICS = 1, 2, 4, 8, 16

# control block words (64-bit word numbers)
W_HEARTBEAT, W_STATE, W_READ, W_ERR_WORD, W_STC = 3, 4, 5, 6, 7
W_AU_COUNTS, W_DISP = 8, 26
W_WINDEX, W_NONCE, W_PAUSE, W_SETTINGS = 32, 33, 34, 35
W_SEQ_TAIL = 31

STATES = {0: 'unwritten', 1: 'no session', 2: 'ready', 3: 'playing', 4: 'ended', 5: 'error'}
ERRORS = {
    1: 'the write index is not a multiple of 16', 2: 'the write index moved backwards or too far ahead',
    3: 'a record header is wrong', 4: 'an unknown record type', 5: 'a flag that is not allowed', 6: 'a payload length out of range',
    7: 'a record crossing the ring end', 8: 'a record out of sequence', 9: 'a unit after end of stream without a discontinuity',
    10: 'a control record', 11: 'pictures and access units mixed', 17: 'a colour code that is not allowed',
}


class RingError(RuntimeError):
    pass


def _mask(width):
    return (1 << width) - 1


class Ring:
    """One program's side of the interface. `region` is a writable buffer of REGION_LEN bytes."""

    def __init__(self, region, tick=None, clock=time.monotonic, sleep=time.sleep):
        self.region = region
        self.words = memoryview(region).cast('I')                 # 32-bit accesses only: unaligned ones fault on the MiSTer's ARM
        self._anchor = ctypes.c_char.from_buffer(region)          # keeps the buffer exported; `release()` lets the mapping close again
        self.base = ctypes.addressof(self._anchor)
        self.tick, self.clock, self.sleep = tick, clock, sleep    # `tick` is for the tests: it plays the core while we wait
        self.session = False
        self.nonce = 0
        self.w = 0                                                # the write index (bytes since forever, wraps at 2**32)
        self.seq = 0
        self.need_disc = False
        self.paused, self.atten = False, 0
        self.sent = {'au': 0, 'audio': 0, 'pad': 0}

    # control block ---------------------------------------------------------------------------------------------------------
    def lo(self, w):
        return self.words[2 * w]

    def hi(self, w):
        return self.words[2 * w + 1]

    def word(self, w):
        return self.lo(w) | (self.hi(w) << 32)

    def field(self, w, lsb, width):
        return (self.word(w) >> lsb) & _mask(width)

    def store(self, w, value):
        self.words[2 * w] = value & 0xFFFFFFFF

    def status(self, attempts=20):
        """A consistent copy of the words the player cares about (the core writes them once a frame; read the tail, the words, the head)."""
        for _ in range(attempts):
            tail = self.lo(W_SEQ_TAIL)
            words = {w: self.word(w) for w in (W_STATE, W_READ, W_ERR_WORD, W_STC, 8, 15, 16, 19, 25, 26, 27, 30, 40, 41, 42, 43, 44)}
            if self.lo(W_HEARTBEAT) == tail:
                st = words[W_STATE]
                return {'heartbeat': tail, 'state': st & 0xFF, 'error': (st >> 8) & 0xFF, 'error_at': st >> 32,
                        'read_index': words[W_READ] & 0xFFFFFFFF, 'ack_nonce': words[W_READ] >> 32,
                        'stc': words[W_STC] & _mask(33), 'lag_ms': (words[W_STC] >> 48) & 0xFFFF,
                        'au': words[8] & 0xFFFFFFFF, 'audio_records': words[8] >> 32,
                        'pictures_seen': words[15] & 0xFFFFFFFF, 'refused': words[15] >> 32,
                        'frontend_errors': words[16] & 0xFFFFFFFF, 'pictures_decoded': words[19] & 0xFFFFFFFF,
                        'skipped': words[26] & 0xFFFFFFFF, 'late': (words[26] >> 32) & 0xFFFF, 'starved': (words[26] >> 48) & 0xFFFF,
                        'audio': {'state': words[40] & 3, 'queued': (words[40] >> 16) & 0x1FF, 'played': words[41] & 0xFFFFFFFF,
                                  'silence': words[42] & 0xFFFFFFFF, 'late': words[42] >> 32, 'underruns': words[43] & 0xFFFF,
                                  'slip_drop': (words[43] >> 16) & 0xFFFF, 'slip_repeat': (words[43] >> 32) & 0xFFFF,
                                  'offset': words[44] & 0xFFFFFFFF},
                        'raw_state': st, 'raw_settings_seq': (words[30] >> 24) & 0xFF}
            self.wait(0.0005)
        raise RingError('The core is writing its status all the time.')

    def state_text(self, st=None):
        st = st or self.status()
        text = STATES.get(st['state'], 'state %d' % st['state'])
        if st['state'] == 5:
            text += ' (%s)' % ERRORS.get(st['error'], 'error %d' % st['error'])
        return text

    def wait(self, seconds):
        if self.tick:
            self.tick()
        else:
            self.sleep(seconds)

    # attaching ---------------------------------------------------------------------------------------------------------------
    def alive(self, timeout=3.0):
        """The magic, a compatible version, and a heartbeat that advances: a core is running. Raises RingError otherwise."""
        if self.word(0) != MAGIC:
            raise RingError('No H264 core answers (the control block has no magic). Load the H264 core first.')
        ver = self.lo(1) & 0xFFFF
        if (ver >> 8) != (VERSION >> 8) or (ver & 0xFF) < (VERSION & 0xFF):
            raise RingError('The core speaks interface %d.%d; this player needs %d.%d or a later 1.x.' % (
                ver >> 8, ver & 0xFF, VERSION >> 8, VERSION & 0xFF))
        first = self.lo(W_HEARTBEAT)
        end = self.clock() + timeout
        while self.clock() < end:
            self.wait(0.01)
            if self.lo(W_HEARTBEAT) != first:
                return True
        raise RingError('The core\'s heartbeat is not moving (a core left over from an earlier run?).')

    def new_session(self, timeout=3.0):
        """A new nonce starts a session (and drops whatever the core has not read yet). Also the way to seek."""
        st = self.status()
        ack, cur = st['ack_nonce'], self.lo(W_NONCE)
        nonce = (int(self.clock() * 1e6) * 2654435761 ^ os.getpid()) & 0xFFFFFFFF
        while nonce == 0 or nonce in (ack, cur):
            nonce = (nonce + 1) & 0xFFFFFFFF
        self.session = False
        self.store(W_PAUSE, self._pause_word())
        if not self.set_word:
            self.set_seq = (self.status()['raw_settings_seq'])         # numbering continues from what the core consumed
        self.store(W_SETTINGS, self.set_word)
        if self.ovl:
            self._ovl_write()                                     # this program's overlay again (a new session starts without one)
        widx = self.lo(W_WINDEX)
        if widx & 15:
            self.store(W_WINDEX, widx & ~15)                      # a cold boot's random memory
        self.store(W_NONCE, nonce)                                # the nonce alone starts the session
        end = self.clock() + timeout
        while True:
            st = self.status()
            if st['ack_nonce'] == nonce:
                self.nonce, self.w, self.seq = nonce, st['read_index'], 0
                self.need_disc = False
                self.session = True
                return
            if self.clock() > end:
                raise RingError('The core did not start a session.')
            self.wait(0.001)

    # writing ---------------------------------------------------------------------------------------------------------------
    def free(self):
        used = (self.w - self.status()['read_index']) & 0xFFFFFFFF
        return 0 if used > RING_SIZE else RING_SIZE - used

    def _put(self, rtype, flags, pts, payload, length=None):
        if length is None:
            length = len(payload) if payload is not None else 0
        n = 16 + ((length + 15) & ~15)
        w0 = rtype | (flags << 8) | ((self.seq & 0xFFFF) << 16) | (length << 32)
        w1 = (pts & _mask(33)) | (SYNC << 48)
        at = self.w
        offset = RING_OFFSET + (at & (RING_SIZE - 1))
        idx = offset // 4
        self.words[idx], self.words[idx + 1] = w0 & 0xFFFFFFFF, w0 >> 32
        self.words[idx + 2], self.words[idx + 3] = w1 & 0xFFFFFFFF, w1 >> 32
        if rtype != T_PAD and payload is not None and length:
            padded = ctypes.create_string_buffer(bytes(payload), n - 16)    # zero-padded to 16 and aligned
            ctypes.memmove(self.base + offset + 16, padded, n - 16)
        self.w = (at + n) & 0xFFFFFFFF
        self.seq = (self.seq + 1) & 0xFFFF

    def try_write(self, rtype, payload, pts=None, flags=0):
        """Write one record if the ring has room. -> True, or False (the ring is full: try again after a moment)."""
        if not self.session:
            raise RingError('No session.')
        length = len(payload)
        if rtype == T_VIDEO_AU and length == 0 or rtype == T_AUDIO and (length == 0 or length % 4) or length > MAX_PAYLOAD:
            raise RingError('A record of %d bytes is not allowed.' % length)
        if pts is not None:
            flags |= F_PTS_VALID
        if rtype == T_VIDEO_AU and self.need_disc:
            flags |= F_DISCONTINUITY
        n = 16 + ((length + 15) & ~15)
        to_end = RING_SIZE - (self.w & (RING_SIZE - 1))
        pad = to_end if n > to_end else 0
        used_free = self.free()
        if pad:
            if pad > used_free:
                return False
            self._put(T_PAD, 0, 0, None, length=to_end - 16)      # fills the ring's tail; its payload is never read
            self.store(W_WINDEX, self.w)
        if n > (self.free() if pad else used_free):
            return False
        self._put(rtype, flags, pts if pts is not None else 0, payload)
        self.store(W_WINDEX, self.w)
        if rtype == T_VIDEO_AU:
            if flags & F_END_OF_STREAM:
                self.need_disc = True
            elif flags & F_DISCONTINUITY:
                self.need_disc = False
        self.sent['au' if rtype == T_VIDEO_AU else 'audio'] += 1
        return True

    def write(self, rtype, payload, pts=None, flags=0, timeout=5.0):
        """try_write, waiting for room. Raises RingError after `timeout` seconds."""
        end = self.clock() + timeout
        while not self.try_write(rtype, payload, pts, flags):
            if self.clock() > end:
                raise RingError('The ring stayed full for %.0f s (the core is not reading).' % timeout)
            self.wait(0.002)

    def write_au(self, au, pts=None, flags=0, timeout=5.0):
        self.write(T_VIDEO_AU, au, pts, flags, timeout)

    def write_audio(self, pcm, pts=None, timeout=5.0):
        self.write(T_AUDIO, pcm, pts, 0, timeout)

    # pause / volume ----------------------------------------------------------------------------------------------------------
    def _pause_word(self):
        return (1 if self.paused else 0) | ((self.atten & 0xFF) << 8)

    def set_pause(self, paused):
        self.paused = bool(paused)
        self.store(W_PAUSE, self._pause_word())

    def set_volume(self, atten):
        """Attenuation in 0.5 dB steps: 0 full volume, 255 mute."""
        self.atten = max(0, min(255, int(atten)))
        self.store(W_PAUSE, self._pause_word())

    # the overlay: a page of the Linux framebuffer drawn over the video -----------------------------------------------------------
    OVL_FORMATS = {'1555': 0, '4444': 1, '8888': 2}
    set_word = 0
    set_seq = 0
    OPT_WIDTH = (2, 2, 2, 1, 2, 2, 2)           # the core's menu options in the order of its mirror: aspect, deinterlace, video output,
    OPT_COUNT = (3, 3, 4, 2, 4, 4, 3)           # HDMI deinterlace, analog aspect, refresh rate, anti-twitter
    OPT_NAMES = ('aspect', 'deint', 'video_out', 'hdmi_deint', 'analog_aspect', 'refresh', 'anti_twitter')

    def request_settings(self, **values):
        """Change menu options of the core from here (analog_aspect=3 ...). The core's own menu adopts them. -> the request's number."""
        mask, word, lsb = 0, 0, 0
        for i, name in enumerate(self.OPT_NAMES):
            if name in values:
                if not 0 <= values[name] < self.OPT_COUNT[i]:
                    raise RingError('%s=%r is not a value of that option.' % (name, values[name]))
                mask |= 1 << i
                word |= values[name] << lsb
            lsb += self.OPT_WIDTH[i]
        if not mask:
            raise RingError('No option named.')
        self.set_seq = 1 if self.set_seq >= 255 else self.set_seq + 1
        word |= (mask << 13) | (self.set_seq << 24)
        self.store(W_SETTINGS, word)
        self.set_word = word
        return self.set_seq

    def clear_settings(self):
        self.set_word = 0
        self.store(W_SETTINGS, 0)

    ovl = None                       # (ctl, base, size word, stride, band word) of the descriptor in force, or None (hidden)
    ovl_gen = 0

    def overlay_show(self, base, width, height, stride, fmt='8888', band=None):
        """Describe an overlay page (a physical address inside the framebuffer). -> the generation to wait for in `overlay_seen`."""
        first, last = band if band else (0, height - 1)
        self.ovl = (1 | (self.OVL_FORMATS[fmt] << 1), base, width | (height << 16), stride, first | (last << 16))
        return self._ovl_write()

    def overlay_hide(self):
        self.ovl = None
        return self._ovl_write()

    def _ovl_write(self):
        self.ovl_gen = (self.ovl_gen + 1) & 0xFFFFFFFF or 1
        ctl, base, size, stride, band = self.ovl if self.ovl else (0, 0, 0, 0, 0)
        self.store(79, self.ovl_gen)                               # the generation first ...
        self.store(73, ctl)
        self.store(74, base)
        self.store(75, size)
        self.store(76, stride)
        self.store(77, band)
        self.store(78, 0)
        self.store(72, self.ovl_gen)                               # ... and last: if they differ the core caught us writing
        return self.ovl_gen

    def overlay_seen(self):
        """(generation the core read last, state: 0 hidden / 1 shown / 2 refused, error code)."""
        w = self.word(46)
        return w & 0xFFFFFFFF, (w >> 32) & 3, (w >> 36) & 15

    def raster(self):
        """(width, height) of the core's output picture area, which overlay pages are centred on."""
        w = self.word(80)
        return w & 0xFFF, (w >> 12) & 0xFFF

    # the input region: MiSTer's gamepads and keys, passed on by the core -----------------------------------------------------
    def input_snapshot(self, attempts=10):
        """-> (pad buttons [6], event_count) as a consistent copy of the input region (word 71 first, word 48 last)."""
        for _ in range(attempts):
            tail = self.lo(71)
            pads = [self.lo(49 + k) & 0xFFFF for k in range(6)]
            count = self.hi(48)
            if self.lo(48) == tail:
                return pads, count
        return pads, count

    def input_events(self, since):
        """Events queued after number `since`: -> (new count, [16-bit events]). Events lost to a slow reader are skipped."""
        pads, count = self.input_snapshot()
        behind = (count - since) & 0xFFFFFFFF
        if behind > 0x80000000:                                      # since is ahead of the core (a core reload): start over
            return count, []
        behind = min(behind, 32)
        out = []
        for n in range((count - behind) & 0xFFFFFFFF, count):
            entry = n % 32
            word = self.lo(56 + entry // 4) | (self.hi(56 + entry // 4) << 32)
            out.append((word >> (16 * (entry % 4))) & 0xFFFF)
        return count, out

    def close(self):
        try:
            self.store(W_PAUSE, 0)
        except Exception:
            pass

    def release(self):
        """Drop every view of the mapped memory (so that it can be unmapped). The ring is unusable afterwards."""
        self._anchor = None
        try:
            self.words.release()
        except ValueError:
            pass


class DevMem:
    """/dev/mem mapped uncached over the control block and the ring, plus the lock that keeps two programs from feeding the core."""

    def __init__(self):
        import fcntl                                   # (not on Windows: the tests never get here)
        self.lock_fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            raise RingError('Another program is feeding the core.')
        self.fd = os.open('/dev/mem', os.O_RDWR | os.O_SYNC)
        self.map = mmap.mmap(self.fd, REGION_LEN, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=CTRL_BYTE)

    def close(self):
        for closer in (self.map.close, lambda: os.close(self.fd), lambda: os.close(self.lock_fd)):
            try:
                closer()
            except (OSError, ValueError, BufferError):
                pass


def open_devmem():
    """-> (Ring, DevMem). Keep the DevMem alive while the Ring is in use."""
    mem = DevMem()
    try:
        return Ring(mem.map), mem
    except Exception:
        mem.close()
        raise


def core_name():
    try:
        with open('/tmp/CORENAME') as f:
            return f.read().strip()
    except OSError:
        return ''


def launch_core(rbf='_Other/H264', timeout=15.0):
    """Ask MiSTer's main program to load the H264 core (an .mgl file and a command on its FIFO). Returns when it reports the name."""
    if core_name() == 'H264':
        return
    with open(MGL_PATH, 'w') as f:
        f.write('<mistergamedescription>\n  <rbf>%s</rbf>\n</mistergamedescription>\n' % rbf)
    fd = os.open('/dev/MiSTer_cmd', os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, ('load_core %s\n' % MGL_PATH).encode())
    finally:
        os.close(fd)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        time.sleep(0.1)
        if core_name() == 'H264':
            return
    raise RingError('The H264 core did not load (is _Other/H264_*.rbf on the card?).')
