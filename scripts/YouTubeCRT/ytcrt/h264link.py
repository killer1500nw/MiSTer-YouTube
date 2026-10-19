"""The player's link to the MiSTer H264 core (the FPGA decodes the video; see h264ring.py).

It offers the same surface the menu code already uses with the player's own core (`pump()`, `render()`, overlays, `health()`), so every
screen of the player works unchanged: the 320x240 YUV overlay the menus draw is converted to a 640x480 ARGB page in the Linux
framebuffer, which the H264 core draws over the picture (or over black when nothing plays). MiSTer's gamepads and keyboard arrive in
the core's input region and are presented to `inputs.Input` through the same four words it reads from the player's own core.
"""
import ctypes
import os
import time

from . import h264ring as hr
from . import transport as t
from . import yuv

OV_W, OV_H = 640, 480                         # the 320x240 menu doubled in both directions; centred by the core on its 720x480 raster
PAGE_BYTES = OV_W * OV_H * 4
PAGES = yuv.OVERLAYS
HEART_TIMEOUT = 3.0


class CoreGone(RuntimeError):
    pass


class Argb:
    """Planar 4:2:0 BT.709 (luma 0 = transparent) -> doubled ARGB8888 rows, with a cache of finished rows (menus repaint mostly the same rows)."""

    def __init__(self, w=320, h=240):
        self.w, self.h = w, h
        self.rows = {}
        self.pix = {}
        self.out = bytearray(OV_W * OV_H * 4)
        self.last_keys = [None] * h

    @staticmethod
    def _convert(y, u, v):
        if y == 0:
            return bytes(8)                                       # transparent
        yy, uu, vv = 1.164 * (y - 16), u - 128, v - 128
        r = max(0, min(255, int(round(yy + 1.793 * vv))))
        g = max(0, min(255, int(round(yy - 0.213 * uu - 0.533 * vv))))
        b = max(0, min(255, int(round(yy + 2.112 * uu))))
        px = bytes((b, g, r, 255))
        return px + px

    def row(self, ys, us, vs):
        key = ys + us + vs
        hit = self.rows.get(key)
        if hit is not None:
            return hit, key
        u2, v2 = bytearray(self.w), bytearray(self.w)
        u2[0::2], u2[1::2], v2[0::2], v2[1::2] = us, us, vs, vs
        pix, conv = self.pix, self._convert
        parts = []
        for k in zip(ys, u2, v2):
            p = pix.get(k)
            if p is None:
                p = pix[k] = conv(*k)
            parts.append(p)
        data = b''.join(parts)
        if len(self.rows) > 3000:
            self.rows.clear()
        self.rows[key] = data
        return data, key

    def render(self, y, u, v):
        """-> the page as a bytearray (the same object every time) and how many source rows changed."""
        w, cw = self.w, self.w // 2
        out, stride = self.out, OV_W * 4
        changed = 0
        for j in range(self.h):
            ys = bytes(y[j * w:(j + 1) * w])
            c0 = (j >> 1) * cw
            us, vs = bytes(u[c0:c0 + cw]), bytes(v[c0:c0 + cw])
            data, key = self.row(ys, us, vs)
            if key == self.last_keys[j]:
                continue
            self.last_keys[j] = key
            out[2 * j * stride:(2 * j + 1) * stride] = data
            out[(2 * j + 1) * stride:(2 * j + 2) * stride] = data
            changed += 1
        return out, changed

    def reset(self):
        self.last_keys = [None] * self.h


class H264Box:
    """What `inputs.Input` reads from the player's own core (words 40-55), made from the H264 core's input region."""

    def __init__(self, ring):
        self.ring = ring
        self.count = ring.input_snapshot()[1]
        self.seq = 0
        self.hist = [0, 0, 0]
        self.joy = 0

    def poll(self):
        ring = self.ring
        pads, _ = ring.input_snapshot()
        joy = 0
        for p in pads:
            joy |= p
        self.joy = joy & 0xFFF
        self.count, events = ring.input_events(self.count)
        for ev in events:
            if (ev >> 14) == 0:                                   # a key; the mouse and the menu's own events are not used
                self.hist = [ev & 0x3FF, self.hist[0], self.hist[1]]
                self.seq = (self.seq + 1) & 0xFFFF

    def read(self, offset):
        if offset == 40:
            self.poll()                                           # `Input._poll_core` reads 40 first: one snapshot per poll
            return self.joy
        if offset == 44:
            return 0
        if offset == 48:
            return self.hist[0] | (self.hist[1] << 16)
        if offset == 52:
            return self.hist[2] | (self.seq << 16)
        return 0


class H264Link:
    """Drop-in for core_link.Link for the menus; playback uses the ring through h264feed."""

    kind = 'h264'
    ident = 0x48323634                                   # "H264"
    interlaced = True
    slots = 8
    big_core = True
    queued = 99                                          # the menu loop never pushes pictures: the overlay is all it feeds
    sent = consumed = 0
    copy_total = copy_max = 0.0
    copy_count = 0
    copy_bytes = 0
    copy_report = {}
    full_total, full_count, repeats = 0.0, 0, 0

    def __init__(self, log=print, cancel=lambda: None):
        self.log, self.cancel = log, cancel
        self.mem = self.ring = self.fb = None
        self.box = None
        self.argb = Argb()
        self.shown = None                                # the page the core is told to show
        self.pending_gen = None
        self.heart, self.heart_time = 0, time.monotonic()
        self.last_check = 0.0
        self.busy_pages = set()
        self.console_detached = False

    # attaching ---------------------------------------------------------------------------------------------------------------
    def open(self):
        pass

    def close(self):
        self.end_session()

    def core_alive(self):
        if hr.core_name() != 'H264':
            return False
        mem = None
        try:
            mem = hr.DevMem()
            ring = hr.Ring(mem.map)
            ring.alive(1.0)
        except (hr.RingError, OSError, ValueError):
            if mem is not None:
                try:
                    ring.release()
                except UnboundLocalError:
                    pass
                mem.close()
            return False
        self.mem, self.ring = mem, ring
        return True

    def wait_for_core(self, timeout=None, poll=0.5, idle=None):
        """Block until the H264 core is loaded and no other program is feeding it."""
        end = None if timeout is None else time.monotonic() + timeout
        last_note = 0.0
        while True:
            self.cancel()
            if self.core_alive():
                return
            if idle:
                idle()
            now = time.monotonic()
            if now - last_note > 60:
                self.log('Waiting for the H264 core to be loaded (core now: %s)...' % (hr.core_name() or 'none'))
                last_note = now
            if end is not None and time.monotonic() > end:
                raise RuntimeError('No live H264 core detected.')
            time.sleep(poll)

    def start_session(self):
        ring = self.ring
        ring.new_session()
        self.fb = t.Framebuffer.open(overlay_only=True)
        if self.fb is None:
            raise RuntimeError('The framebuffer (/dev/fb0) is not available for the menu overlay.')
        names = t.console_bind(False)
        t.console_graphics(True)
        self.console_detached = bool(names)
        self.box = H264Box(ring)
        self.heart, self.heart_time = ring.lo(hr.W_HEARTBEAT), time.monotonic()
        self.shown, self.pending_gen = None, None
        self.argb.reset()
        self.log('H264 core session started (framebuffer %#x, %d bytes; raster %dx%d)' % (
            self.fb.start, self.fb.length, ring.raster()[0], ring.raster()[1]))

    def end_session(self):
        ring, self.ring = self.ring, None
        if ring is not None:
            try:
                ring.overlay_hide()
                ring.set_pause(False)
            except Exception:
                pass
            ring.close()
            ring.release()
        if self.console_detached:
            t.console_bind(True)
            self.console_detached = False
        t.console_graphics(False)
        if self.mem is not None:
            self.mem.close()
            self.mem = None
        if self.fb is not None:
            try:
                self.fb.close()
            except Exception:
                pass
        self.fb = None
        self.box = None

    def health(self):
        self.cancel()
        ring, now = self.ring, time.monotonic()
        if ring is None:
            raise CoreGone('No session.')
        if now - self.last_check > 0.5:
            self.last_check = now
            if hr.core_name() != 'H264':
                raise CoreGone('The H264 core was unloaded.')
        beat = ring.lo(hr.W_HEARTBEAT)
        if beat != self.heart:
            self.heart, self.heart_time = beat, now
        elif now - self.heart_time > HEART_TIMEOUT:
            raise CoreGone('The H264 core stopped (no heartbeat).')
        if ring.word(0) != hr.MAGIC:
            raise CoreGone('The H264 core was reloaded.')

    # the menu overlay --------------------------------------------------------------------------------------------------------
    def _page_phys(self, idx):
        return self.fb.start + self.fb.length - (PAGES - idx) * PAGE_BYTES

    def _page_virt(self, idx):
        return self.fb.base + self.fb.length - (PAGES - idx) * PAGE_BYTES

    def _acked(self):
        """Has the core read the last descriptor (so that the page shown before it can be drawn on again)?"""
        if self.pending_gen is None:
            return True
        gen, state, err = self.ring.overlay_seen()
        if state == 2:
            raise RuntimeError('The core refused the overlay (error %d).' % err)
        if (gen - self.pending_gen) & 0xFFFFFFFF < 0x80000000:
            self.pending_gen = None
            return True
        return False

    def free_overlay(self, keep=None):
        """A page the core is not reading and that is not `keep`, or None (the last page flip is not acknowledged yet)."""
        if not self._acked():
            return None
        for i in range(PAGES):
            if i != keep and i != self.shown:
                return i
        return None

    def upload_overlay(self, index, y, u, v):
        """Convert the finished 320x240 overlay, write it to page `index` and tell the core to show that page."""
        page, _ = self.argb.render(y, u, v)
        ctypes.memmove(self._page_virt(index), (ctypes.c_char * PAGE_BYTES).from_buffer(page), PAGE_BYTES)
        self.pending_gen = self.ring.overlay_show(self._page_phys(index), OV_W, OV_H, OV_W * 4, '8888')
        self.shown = index

    def hide_overlay(self):
        self.pending_gen = self.ring.overlay_hide()
        self.shown = None

    def push_picture(self, frame, audio, w, h, crop='Off', flags=0, overlay=None):
        """The menu loop's 'show the base picture with overlay N': the overlay is already shown, nothing else to do."""

    def push(self, record):
        pass

    def measure_copy(self):
        return 0.0, 0.0

    def measure_fast_copy(self, *a, **k):
        return {}

    def use_copy_method(self, method):
        pass
