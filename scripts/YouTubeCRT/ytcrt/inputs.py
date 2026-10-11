"""Gamepad / keyboard input from Linux evdev (no extra packages), plus a
terminal fallback for SSH sessions and a queue for tests.

Actions: up down left right ok back menu alt pgup pgdn start
"""
import glob
import os
import queue
import select
import struct
import sys
import threading
import time

EV_KEY, EV_ABS = 1, 3
EVENT = struct.Struct('llHHi')          # native timeval: 8 bytes on the ARM, 16 on 64-bit

KEYS = {
    103: 'up', 108: 'down', 105: 'left', 106: 'right',
    28: 'ok', 96: 'ok', 57: 'ok', 1: 'back', 14: 'back',
    15: 'menu', 17: 'alt', 16: 'start', 104: 'pgup', 109: 'pgdn', 102: 'start',
    # gamepad buttons (Linux BTN_*): A/B/X/Y by position, shoulders, start/select
    304: 'ok', 305: 'back', 307: 'menu', 308: 'alt', 310: 'pgup', 311: 'pgdn',
    315: 'start', 314: 'menu', 288: 'ok', 289: 'back', 290: 'menu', 291: 'alt',
    292: 'ok', 293: 'back', 294: 'pgup', 295: 'pgdn', 296: 'back', 297: 'start',
    # d-pad reported as buttons
    544: 'up', 545: 'down', 546: 'left', 547: 'right',
}
# Controller bits as MiSTer's main program sends them to the core (hps_io joystick_N):
# 0-3 = right, left, down, up; then the buttons named in the core's J1 line
# (A, B, X, Y, L, R, Select, Start).
JOY_BITS = {0: 'right', 1: 'left', 2: 'down', 3: 'up', 4: 'ok', 5: 'back', 6: 'menu', 7: 'alt',
            8: 'pgup', 9: 'pgdn', 10: 'menu', 11: 'start'}
# PS/2 set-2 make codes as (extended, code), from hps_io ps2_key.
PS2_KEYS = {
    (1, 0x75): 'up', (1, 0x72): 'down', (1, 0x6B): 'left', (1, 0x74): 'right',
    (0, 0x5A): 'ok', (1, 0x5A): 'ok', (0, 0x29): 'ok',
    (0, 0x76): 'back', (0, 0x66): 'back',
    (0, 0x0D): 'menu', (0, 0x1D): 'alt', (0, 0x15): 'start', (1, 0x6C): 'start',
    (1, 0x7D): 'pgup', (1, 0x7A): 'pgdn',
}
# Typing from a real keyboard (PS/2 set-2 make codes), used only while a text box is on screen.
_LET = {0x1C: 'a', 0x32: 'b', 0x21: 'c', 0x23: 'd', 0x24: 'e', 0x2B: 'f', 0x34: 'g', 0x33: 'h', 0x43: 'i',
        0x3B: 'j', 0x42: 'k', 0x4B: 'l', 0x3A: 'm', 0x31: 'n', 0x44: 'o', 0x4D: 'p', 0x15: 'q', 0x2D: 'r',
        0x1B: 's', 0x2C: 't', 0x3C: 'u', 0x2A: 'v', 0x1D: 'w', 0x22: 'x', 0x35: 'y', 0x1A: 'z'}
TEXT_KEYS = {(0, c): (ch, ch.upper()) for c, ch in _LET.items()}
TEXT_KEYS.update({(0, c): (d, s_) for c, d, s_ in (
    (0x45, '0', ')'), (0x16, '1', '!'), (0x1E, '2', '@'), (0x26, '3', '#'), (0x25, '4', '$'), (0x2E, '5', '%'),
    (0x36, '6', '^'), (0x3D, '7', '&'), (0x3E, '8', '*'), (0x46, '9', '('), (0x4E, '-', '_'), (0x55, '=', '+'),
    (0x54, '[', '{'), (0x5B, ']', '}'), (0x4C, ';', ':'), (0x52, "'", '"'), (0x41, ',', '<'), (0x49, '.', '>'),
    (0x4A, '/', '?'), (0x0E, '`', '~'), (0x5D, '\\', '|'), (0x29, ' ', ' '))})
SHIFT_CODES = (0x12, 0x59)
DIRS = ('up', 'down', 'left', 'right')
REPEAT_FIRST, REPEAT_NEXT = 0.40, 0.09
TTY_KEYS = {'\x1b[A': 'up', '\x1b[B': 'down', '\x1b[D': 'left', '\x1b[C': 'right', '\r': 'ok',
            '\n': 'ok', ' ': 'ok', '\x1b': 'back', '\x7f': 'back', 'b': 'back', '\t': 'menu',
            'w': 'alt', 'q': 'start', '\x1b[5~': 'pgup', '\x1b[6~': 'pgdn'}


class Input:
    def __init__(self, evdev=True, tty=True):
        self.q = queue.Queue()
        self.held = {}                   # dir -> next repeat time
        self.stop = threading.Event()
        self.fds = {}                    # fd -> abs ranges
        self.threads = []
        self.core = None                 # mailbox with controller/keyboard words, once attached
        self._joy_prev = 0
        self._key_seq = 0
        self.text_mode = False           # True while a text box is shown: keys type instead of navigate
        self._shift = False
        if evdev:
            self.threads.append(threading.Thread(target=self._evdev_loop, daemon=True))
        if tty and sys.stdin and sys.stdin.isatty():
            self.threads.append(threading.Thread(target=self._tty_loop, daemon=True))
        for t in self.threads:
            t.start()

    def push(self, action):
        self.q.put(action)

    # input delivered by the core --------------------------------------
    def attach_core(self, box):
        """Read controller/keyboard from the core's mailbox words (offsets 40-55).

        MiSTer's main program owns the real devices and passes them to the loaded
        core through hps_io; Linux processes do not see them. Only call this while
        our own core is running (the words mean nothing otherwise).
        """
        self.core = box
        self._joy_prev = (box.read(40) | box.read(44)) & 0xFFF
        self._key_seq = box.read(52) >> 16
        self.held.clear()

    def detach_core(self):
        self.core = None
        self.held.clear()

    def _poll_core(self):
        box = self.core
        joy = (box.read(40) | box.read(44)) & 0xFFF
        if joy != self._joy_prev:
            for bit, action in JOY_BITS.items():
                was, now = self._joy_prev >> bit & 1, joy >> bit & 1
                if was != now:
                    self._edge(action, bool(now))
            self._joy_prev = joy
        lo, hi = box.read(48), box.read(52)
        seq = hi >> 16
        n = (seq - self._key_seq) & 0xFFFF
        if n:
            events = [lo & 0xFFFF, lo >> 16, hi & 0xFFFF]        # newest first
            for ev in reversed(events[:min(n, 3)]):
                ext, code, down = ev >> 8 & 1, ev & 0xFF, bool(ev >> 9 & 1)
                if not ext and code in SHIFT_CODES:
                    self._shift = down
                    continue
                if self.text_mode:
                    typed = self._typed(ext, code, down)
                    if typed:
                        self.q.put(typed)
                        continue
                action = PS2_KEYS.get((ext, code))
                if action:
                    self._edge(action, down)
            self._key_seq = seq

    def _typed(self, ext, code, down):
        """What a key press means in a text box: 'ch:x', 'bksp', 'submit', 'back', or None (navigate)."""
        if not down:
            return None
        if (ext, code) in TEXT_KEYS:
            lower, upper = TEXT_KEYS[(ext, code)]
            return 'ch:' + (upper if self._shift else lower)
        if (ext, code) == (0, 0x66):
            return 'bksp'
        if (ext, code) in ((0, 0x5A), (1, 0x5A)):
            return 'submit'
        if (ext, code) == (0, 0x76):
            return 'back'
        return None

    def get(self, timeout=0.0):
        """Next action or None. Also emits d-pad auto-repeat while held."""
        if self.core is not None:
            self._poll_core()
        now = time.monotonic()
        for d, t in list(self.held.items()):
            if now >= t:
                self.held[d] = now + REPEAT_NEXT
                self.q.put(d)
        try:
            return self.q.get(timeout=timeout) if timeout else self.q.get_nowait()
        except queue.Empty:
            return None

    def close(self):
        self.stop.set()

    # evdev --------------------------------------------------------------
    def _open_all(self):
        for path in glob.glob('/dev/input/event*'):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            if fd not in self.fds:
                self.fds[fd] = {}
        return list(self.fds)

    def _abs_range(self, fd, code):
        try:
            import fcntl
            buf = bytearray(24)          # struct input_absinfo: 6 x s32
            fcntl.ioctl(fd, 0x80184540 + code, buf)
            _, lo, hi = struct.unpack('iii', bytes(buf[:12]))
            return lo, hi
        except Exception:
            return None

    def _evdev_loop(self):
        last_scan = 0.0
        while not self.stop.is_set():
            if time.monotonic() - last_scan > 5:
                self._open_all()
                last_scan = time.monotonic()
            if not self.fds:
                time.sleep(1)
                continue
            try:
                ready, _, _ = select.select(list(self.fds), [], [], 0.2)
            except (OSError, ValueError):
                self.fds.clear()
                continue
            for fd in ready:
                try:
                    data = os.read(fd, EVENT.size * 32)
                except BlockingIOError:
                    continue
                except OSError:
                    self.fds.pop(fd, None)
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    continue
                for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, etype, code, value = EVENT.unpack_from(data, off)
                    if etype == EV_KEY and code in KEYS and value in (0, 1):
                        self._edge(KEYS[code], value == 1)
                    elif etype == EV_ABS:
                        self._abs(fd, code, value)

    def _abs(self, fd, code, value):
        if code in (16, 17):             # hat: -1/0/1
            a, b = ('left', 'right') if code == 16 else ('up', 'down')
            self._edge(a, value < 0)
            self._edge(b, value > 0)
        elif code in (0, 1):             # left stick, thresholds from the axis range
            rng = self.fds.setdefault(fd, {})
            if code not in rng:
                rng[code] = self._abs_range(fd, code) or (0, 255)
            lo, hi = rng[code]
            mid, span = (lo + hi) / 2.0, (hi - lo) / 2.0 or 1
            a, b = ('left', 'right') if code == 0 else ('up', 'down')
            self._edge(a, value < mid - span * 0.5)
            self._edge(b, value > mid + span * 0.5)

    def _edge(self, action, down):
        if action in DIRS:
            if down and action not in self.held:
                self.held[action] = time.monotonic() + REPEAT_FIRST
                self.q.put(action)
            elif not down:
                self.held.pop(action, None)
        elif down:
            self.q.put(action)

    # terminal -----------------------------------------------------------
    def _tty_loop(self):
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            return
        try:
            while not self.stop.is_set():
                if not select.select([fd], [], [], 0.2)[0]:
                    continue
                s = os.read(fd, 8).decode('latin-1')
                if s in TTY_KEYS:
                    self.q.put(TTY_KEYS[s])
                elif s.lower() in TTY_KEYS:
                    self.q.put(TTY_KEYS[s.lower()])
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
