#!/usr/bin/env python3
"""Native ARM copy helper and CPU-owned DDR mailbox access.

Two cores speak this protocol: v0.14-auto (RGB565 records, identity 0x34445459) and v0.15-yuv (YUV pictures scaled by the
FPGA, identity 0x35445459, see docs/FPGA-YUV-v0.15.md). The mailbox words are the same; the slot layout differs.
"""
from pathlib import Path
import ctypes
import os
import queue
import threading
import time
from .memory_layout import check_layout, BASE
from . import yuv

IDENT_RGB = 0x34445459   # v0.14-auto core; v0.13 used 0x33445459
IDENT_YUV = 0x35445459   # v0.15-yuv core
IDENT_FB6 = 0x36445459   # first v0.16 core: header word 6 (the picture planes can be read from the Linux framebuffer)
IDENT_FB = 0x37445459    # v0.16b core: the same plus word 7 (plane offsets), so pictures up to 864 x 480 are possible
IDENT = IDENT_RGB        # kept for older callers
IDENTS = (IDENT_FB, IDENT_FB6, IDENT_YUV, IDENT_RGB)
FB_IDENTS = (IDENT_FB, IDENT_FB6)         # cores that can read pictures from the framebuffer
RUN = 0x33515459
MAP_BYTES = yuv.MAP_BYTES        # one mapping serves both cores (the RGB core simply never touches the upper part)
RGB_MAP_BYTES = 0x201000
SLOTS = 8
RECORD_BYTES = 160000
FRAME_SECONDS = 666528 / 20000000
YUV_FRAME_SECONDS = 1333056 / 40000000


NATIVE = None

def load_native(path=None):
    global NATIVE
    if NATIVE is None:
        location = Path(path) if path else Path(__file__).resolve().parent.parent / 'libyt_sender.so'
        lib = ctypes.CDLL(str(location))
        lib.yt_native_version.argtypes = []
        lib.yt_native_version.restype = ctypes.c_int
        lib.yt_copy_slot.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        lib.yt_copy_slot.restype = ctypes.c_int
        if lib.yt_native_version() != 130:
            raise RuntimeError('Incorrect native helper version; copy the complete YouTubeStreamSmooth folder.')
        NATIVE = lib
    return NATIVE


import collections
_KEEP = collections.deque(maxlen=32)       # temporary copies of read-only buffers, kept alive while their addresses are in use


def address_of(buf):
    """Address of the first byte of a bytes / bytearray / memoryview (writable or not) or a ctypes array."""
    if isinstance(buf, ctypes.Array):
        return ctypes.addressof(buf)
    if isinstance(buf, (bytearray, memoryview)) and not (isinstance(buf, memoryview) and buf.readonly):
        return ctypes.addressof(ctypes.c_char.from_buffer(buf))
    if isinstance(buf, memoryview):
        buf = buf.tobytes()
        _KEEP.append(buf)                       # the copy must outlive this call: its address is what the caller uses
    return ctypes.cast(ctypes.c_char_p(buf), ctypes.c_void_p).value


COPY_METHODS = ('fb', 'memmove', 'parallel', 'cached')
PARALLEL_MIN = 65536               # a piece at least this big is split between the two CPU cores
ARM_NR_CACHEFLUSH = 0x0F0002       # ARM Linux: cacheflush(start, end, 0) writes the data cache back to memory


def cacheflush(start, end):
    """Write the CPU's cached copy of [start, end) back to DDR so the FPGA sees it. False if the system call is not there."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.argtypes = [ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        return libc.syscall(ARM_NR_CACHEFLUSH, start, end, 0) == 0
    except Exception:                                   # no such system call (not an ARM Linux): the caller carries on without it
        return False


def console_bind(on, root='/sys/class/vtconsole'):
    """Attach (on=True) or detach (False) the Linux text console from the framebuffer. While it is attached its cursor blinks and any
    text it prints is drawn into /dev/fb0, which is where this player writes pictures (dashes through the video, one per blink).
    -> the names of the console drivers that were switched ('vtcon1'), [] when there is nothing to switch."""
    changed = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return changed
    for name in names:
        try:
            with open(os.path.join(root, name, 'name'), encoding='utf-8', errors='replace') as f:
                if 'frame buffer' not in f.read().lower():
                    continue
            with open(os.path.join(root, name, 'bind'), 'w') as f:
                f.write('1' if on else '0')
            changed.append(name)
        except OSError:
            continue
    return changed


def console_graphics(on, tty_paths=('/dev/tty0', '/dev/tty1')):
    """Put the text console into graphics mode (KDSETMODE, what X does): the framebuffer console then stops drawing its text and
    cursor altogether. A second way to keep it out of the pictures if it cannot be unbound. -> True if a console took it."""
    try:
        import fcntl
    except ImportError:
        return False
    for path in tty_paths:
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        except OSError:
            continue
        try:
            fcntl.ioctl(fd, 0x4B3A, 1 if on else 0)           # KDSETMODE: KD_GRAPHICS (1) / KD_TEXT (0)
            return True
        except OSError:
            continue
        finally:
            os.close(fd)
    return False


def quiet_console(tty_paths=('/dev/tty1', '/dev/console'), blink='/sys/class/graphics/fbcon/cursor_blink'):
    """Stop the Linux text console's cursor from blinking in the framebuffer (it flashes dashes through a picture that sits where the
    cursor happens to be). Hides the cursor (escape code) and switches the blink off; best effort, -> how many things worked."""
    done = 0
    for path in tty_paths:
        try:
            with open(path, 'w') as tty:
                tty.write(chr(27) + '[?25l')
            done += 1
            break
        except OSError:
            continue
    try:
        with open(blink, 'w') as f:
            f.write('0')
        done += 1
    except OSError:
        pass
    return done


def split_pieces(pieces):
    """[(destination address, source address, bytes)] cut into two lists of about equal size (big pieces are halved)."""
    flat = []
    for dst, src, n in pieces:
        if n >= PARALLEL_MIN:
            half = (n // 2) & ~7
            flat += [(dst, src, half), (dst + half, src + half, n - half)]
        else:
            flat.append((dst, src, n))
    flat.sort(key=lambda p: -p[2])
    mine, theirs, a, b = [], [], 0, 0
    for p in flat:
        if a <= b:
            mine.append(p)
            a += p[2]
        else:
            theirs.append(p)
            b += p[2]
    return mine, theirs


class Framebuffer:
    """The Linux framebuffer (/dev/fb0, the MiSTer_fb driver) as the place pictures are written for the v0.16 core.

    The kernel maps it for fast merged writes (a 345 KB picture in about 1 ms against 8 to 28 ms for the uncached region at 0x30000000)
    and the FPGA reads the same DDR. Slot n sits at yuv.fb_pic_base(start, n). After the writes a cacheflush call acts as the memory
    barrier, and a few bytes of each piece are read back through an uncached /dev/mem window before the record is published."""

    def __init__(self, start, length, fb_map, view_map, slots_offset=None):
        self.start, self.length = start, length
        # The Linux text console draws into this buffer from the top (and its cursor blinks there): the pictures go at the far end,
        # 64 KB aligned, so a console line printed by a script or the kernel cannot land in one.
        self.slots_offset = yuv.FB_SLOTS_OFFSET if slots_offset is None else slots_offset
        self.fb_map, self.view_map = fb_map, view_map
        self.stats = [0.0, 0.0, 0.0, 0]              # seconds spent copying, in the barrier, in the read-back check; pictures written
        self.writes = 0
        self.parallel = True                         # a second thread writes half of every big picture (both CPU cores' write buffers)
        self.jobs = self.done = self.worker = None
        self.base = ctypes.addressof(ctypes.c_char.from_buffer(fb_map))
        self.view = ctypes.addressof(ctypes.c_char.from_buffer(view_map))

    @classmethod
    def open(cls, fb_path='/dev/fb0', mem_path='/dev/mem', overlay_only=False):
        """Open video slots with an uncached read-back window, or just the H264 menu's overlay pages.

        Overlay-only users need three 640x480 ARGB pages, not the eight video slots or /dev/mem read-back mapping.
        Returns None when the framebuffer cannot provide the requested layout.
        """
        import fcntl
        import mmap
        import struct
        fd = fdm = None
        try:
            fd = os.open(fb_path, os.O_RDWR)
            info = bytearray(160)
            fcntl.ioctl(fd, 0x4602, info)                         # FBIOGET_FSCREENINFO
            start, length = struct.unpack('<II', bytes(info[16:24]))
            minimum = 640 * 480 * 4 * yuv.OVERLAYS if overlay_only else yuv.FB_MIN_LENGTH
            if length < minimum or start % 4096:
                os.close(fd)
                return None
            slots_offset = max(yuv.FB_SLOTS_OFFSET, (length - 8 * yuv.FB_SLOT_STRIDE) & ~0xFFFF)
            fb_map = mmap.mmap(fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            if overlay_only:
                # No video planes are copied or read back in this mode. Share the mapping; close it only once.
                fb = cls(start, length, fb_map, fb_map, slots_offset=0)
                fb.fds = (fd,)
                return fb
            fdm = os.open(mem_path, os.O_RDWR | os.O_SYNC)
            view_map = mmap.mmap(fdm, 8 * yuv.FB_SLOT_STRIDE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
                                 offset=start + slots_offset)
            fb = cls(start, length, fb_map, view_map, slots_offset)
            quiet_console()
            fb.fds = (fd, fdm)
            return fb
        except (OSError, ValueError, ImportError, struct.error):
            for f in (fd, fdm):
                if f is not None:
                    try:
                        os.close(f)
                    except OSError:
                        pass
            return None

    def pic_base(self, number):
        return self.start + self.slots_offset + (number % 8) * yuv.FB_SLOT_STRIDE

    def write_planes(self, number, pieces):
        """pieces = [(offset inside the picture slot, buffer)]: copy them into slot `number` and check that they arrived."""
        slot_off = self.slots_offset + (number % 8) * yuv.FB_SLOT_STRIDE
        done = []
        t0 = time.monotonic()
        jobs = []
        for off, src in pieces:
            n = len(src)
            if n <= 0:
                continue
            if off < 0 or off + n > yuv.FB_SLOT_STRIDE:
                raise ValueError('picture piece outside its framebuffer slot')
            addr = address_of(src)
            jobs.append((self.base + slot_off + off, addr, n))
            done.append((off, n, addr))
        if self.parallel and sum(j[2] for j in jobs) >= 2 * PARALLEL_MIN:
            if self.worker is None:
                self.jobs, self.done = queue.SimpleQueue(), queue.SimpleQueue()
                self.worker = threading.Thread(target=self._worker_loop, name='fb-helper', daemon=True)
                self.worker.start()
            mine, theirs = split_pieces(jobs)
            self.jobs.put(theirs)
            for dst, src, n in mine:
                ctypes.memmove(dst, src, n)
            try:
                result = self.done.get(timeout=5)
            except queue.Empty:
                raise RuntimeError('The framebuffer helper did not finish.')
            if result is not None:
                raise result
        else:
            for dst, src, n in jobs:
                ctypes.memmove(dst, src, n)
        t1 = time.monotonic()
        last = done[-1] if done else None
        if last:                                                    # the barrier: the merged writes are drained before the header goes out
            cacheflush(self.base + slot_off + last[0] + last[1] - 64, self.base + slot_off + last[0] + last[1])
        t2 = time.monotonic()
        view_off = (number % 8) * yuv.FB_SLOT_STRIDE
        self.writes += 1
        check = self.writes <= 16 or self.writes % 16 == 0         # the read-back costs more than the copy itself (3.5 ms against 0.4)
        for off, n, addr in (done if check else ()):
            for p in {0, max(0, n // 2 - 8), max(0, n - 16)}:
                k = min(16, n - p)
                if ctypes.string_at(self.view + view_off + off + p, k) != ctypes.string_at(addr + p, k):
                    raise RuntimeError('Framebuffer write verification failed.')
        t3 = time.monotonic()
        self.stats[0] += t1 - t0
        self.stats[1] += t2 - t1
        self.stats[2] += t3 - t2
        self.stats[3] += 1

    def _worker_loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                for dst, src, n in job:
                    ctypes.memmove(dst, src, n)
                self.done.put(None)
            except Exception as exc:                     # reported to the thread that waits for it
                self.done.put(exc)

    def close(self):
        if self.jobs is not None:
            self.jobs.put(None)
        maps = (self.fb_map,) if self.view_map is self.fb_map else (self.fb_map, self.view_map)
        for m in maps:
            try:
                m.close()
            except (OSError, ValueError, BufferError):
                pass
        for f in getattr(self, 'fds', ()):
            try:
                os.close(f)
            except OSError:
                pass


class Mailbox:
    def __init__(self, memory, fd=None, cached=None):
        self.words = (ctypes.c_uint32 * (len(memory) // 4)).from_buffer(memory)
        self.base = ctypes.addressof(self.words)
        self.fd = fd                    # kept for older callers
        # A second mapping of the same memory WITHOUT O_SYNC: if the kernel makes it cacheable, writes through it run at RAM
        # speed instead of one slow uncached store at a time; copy_in then flushes the cache and checks the result through
        # the uncached mapping above, and drops back to the plain copy if anything is off.
        self.cached = cached
        self.cached_base = ctypes.addressof(ctypes.c_char.from_buffer(cached)) if cached is not None else None
        self.method = 'memmove'
        self.parts = [0.0, 0.0, 0.0, 0]  # seconds in a full picture's planes, audio and header writes; pictures counted
        self.fb = None                  # a Framebuffer, when the v0.16 core and /dev/fb0 allow pictures to go there
        self.jobs = self.done = self.worker = None

    def use_method(self, method):
        """Pick how bulk copies are made ('memmove' into the uncached mapping, 'parallel' = the same on both CPU cores at once,
        or 'cached'); ignores what it cannot do."""
        if method == 'cached' and self.cached_base is not None:
            self.method = 'cached'
        elif method == 'fb' and self.fb is not None:
            self.method = 'fb'
        elif method in ('memmove', 'parallel'):
            self.method = method

    @property
    def fb_active(self):
        return self.method == 'fb' and self.fb is not None

    # Stores to this memory are uncached and wait for the memory system one at a time, so one CPU core copies at only 15 to 50 MB/s
    # while the other core sits idle (the CPU log showed 45% idle during playback). A second thread copies the other half; ctypes
    # releases the interpreter lock during memmove, so both really run at the same time.
    def _worker_loop(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                for dst, src, n in job:
                    ctypes.memmove(dst, src, n)
                self.done.put(None)
            except Exception as exc:                     # reported to the thread that waits for it
                self.done.put(exc)

    def _split(self, pieces):
        return split_pieces(pieces)

    def copy_pieces(self, pieces):
        """Copy several (offset, buffer) pieces into the mapping and verify the ends of each. With method 'parallel' the work is
        shared between this thread and a helper thread."""
        checked = []
        for offset, src in pieces:
            n = len(src)
            if n <= 0:
                continue
            if offset < 0 or offset + n > len(self.words) * 4 or offset % 4:
                raise ValueError('copy outside the mapping')
            checked.append((offset, src, n, address_of(src)))
        if self.method != 'parallel' or len(checked) == 0:
            for offset, src, n, addr in checked:
                self.copy_in(offset, src, n)
            return
        if self.worker is None:
            self.jobs, self.done = queue.SimpleQueue(), queue.SimpleQueue()
            self.worker = threading.Thread(target=self._worker_loop, name='copy-helper', daemon=True)
            self.worker.start()
        mine, theirs = self._split([(self.base + o, addr, n) for o, s, n, addr in checked])
        self.jobs.put(theirs)
        for dst, src, n in mine:
            ctypes.memmove(dst, src, n)
        try:
            result = self.done.get(timeout=5)
        except queue.Empty:
            raise RuntimeError('The copy helper did not finish.')
        if result is not None:
            raise result
        for offset, src, n, addr in checked:
            k = min(n, 16)
            if ctypes.string_at(self.base + offset, k) != ctypes.string_at(addr, k) or \
                    ctypes.string_at(self.base + offset + n - k, k) != ctypes.string_at(addr + n - k, k) or not self._intact(offset, addr, n):
                raise RuntimeError('Bulk copy verification failed.')

    def close(self):
        if self.jobs is not None:
            self.jobs.put(None)
        if self.fb is not None:
            self.fb.close()
            self.fb = None
        del self.words

    def read(self, offset):
        return self.words[offset // 4]

    def write(self, offset, value):
        if offset not in (0, 4, 8, 12):
            raise ValueError('Control write outside CPU-owned words.')
        self.words[offset // 4] = value
        if self.read(offset) != value:
            raise RuntimeError('Control readback failed.')

    def command(self, session):
        self.write(0, 0)
        self.write(4, session)
        self.write(0, RUN if session else 0)

    def record(self, number, data):
        """v0.14 core: one 160000-byte RGB565 + PCM record into slot `number`."""
        if len(data) != RECORD_BYTES or number < 0:
            raise ValueError('Incorrect record length or index.')
        source = ctypes.create_string_buffer(data, RECORD_BYTES)
        result = load_native().yt_copy_slot(ctypes.addressof(self.words), number % SLOTS,
                                            ctypes.addressof(source), RECORD_BYTES)
        if result:
            raise RuntimeError('Native record copy failed (%d).' % result)

    # v0.15 core: bulk copies (libc memmove into the mapping, checked by reading the ends back) ----------------------
    def copy_in(self, offset, src, nbytes=None):
        """Copy a buffer (bytes / bytearray / memoryview, 4-byte aligned) to `offset` of the mapping and verify its ends."""
        n = len(src) if nbytes is None else nbytes
        if n <= 0:
            return
        if offset < 0 or offset + n > len(self.words) * 4 or offset % 4:
            raise ValueError('copy outside the mapping')
        addr = address_of(src)
        if self.method == 'cached':
            ctypes.memmove(self.cached_base + offset, addr, n)
            flushed = cacheflush(self.cached_base + offset, self.cached_base + offset + n)
            if not (flushed and self._intact(offset, addr, n)):
                self.method = 'memmove'             # the cached mapping does not behave here: never use it again
                ctypes.memmove(self.base + offset, addr, n)
        else:
            ctypes.memmove(self.base + offset, addr, n)
        k = min(n, 16)
        if ctypes.string_at(self.base + offset, k) != ctypes.string_at(addr, k) or \
                ctypes.string_at(self.base + offset + n - k, k) != ctypes.string_at(addr + n - k, k):
            raise RuntimeError('Bulk copy verification failed.')

    def _intact(self, offset, addr, n):
        """Did the flushed data reach memory? Compare a few spots through the uncached mapping."""
        spots = {0, max(0, n // 3 - 8), max(0, 2 * n // 3 - 8), max(0, n - 16)}
        return all(ctypes.string_at(self.base + offset + p, min(16, n - p)) == ctypes.string_at(addr + p, min(16, n - p))
                   for p in spots)

    def picture(self, number, header, frame, ysize, audio, in_fb=False):
        """v0.15/v0.16 core: header, Y/U/V planes (`frame` = Y then U then V, ysize bytes of Y) and PCM into slot `number`.
        With `in_fb` the planes go to the framebuffer (the header must carry that address, see yuv.header_for pic_base)."""
        if len(audio) != yuv.AUDIO_BYTES or number < 0:
            raise ValueError('Incorrect audio length or index.')
        base = yuv.slot_offset(number)
        csize = ysize // 4
        if len(frame) != ysize + 2 * csize:
            raise ValueError('Incorrect picture size.')
        view = memoryview(frame)
        t0 = time.monotonic()
        if in_fb:
            head = yuv.parse_header(header)
            self.fb.write_planes(number, [(yuv.Y_OFF, view[:ysize]), (head['u_off'], view[ysize:ysize + csize]),
                                          (head['v_off'], view[ysize + csize:])])
        else:
            self.copy_pieces([(base + yuv.Y_OFF, view[:ysize]), (base + yuv.U_OFF, view[ysize:ysize + csize]),
                              (base + yuv.V_OFF, view[ysize + csize:])])
        t1 = time.monotonic()
        self.copy_in(base + yuv.AUDIO_OFF, ctypes.create_string_buffer(bytes(audio), yuv.AUDIO_BYTES))
        t2 = time.monotonic()
        self.copy_in(base, ctypes.create_string_buffer(bytes(header), yuv.HEADER_BYTES))     # the header last
        t3 = time.monotonic()
        self.parts[0] += t1 - t0                      # where a full picture's time goes: planes, audio, header
        self.parts[1] += t2 - t1
        self.parts[2] += t3 - t2
        self.parts[3] += 1

    def picture_again(self, number, header, audio):
        """v0.15 core: a record that shows the picture of the slot named in `header` again: only the audio and the header
        are written (the planes are not copied)."""
        if len(audio) != yuv.AUDIO_BYTES or number < 0:
            raise ValueError('Incorrect audio length or index.')
        base = yuv.slot_offset(number)
        self.copy_in(base + yuv.AUDIO_OFF, ctypes.create_string_buffer(bytes(audio), yuv.AUDIO_BYTES))
        self.copy_in(base, ctypes.create_string_buffer(bytes(header), yuv.HEADER_BYTES))     # the header last

    def overlay(self, index, y, u, v):
        base = yuv.overlay_offset(index)
        self.copy_in(base + yuv.OV_Y_OFF, y)
        self.copy_in(base + yuv.OV_U_OFF, u)
        self.copy_in(base + yuv.OV_V_OFF, v)

    def status(self, session):
        actual = self.read(20)
        consumed = self.read(16)
        error = self.read(28) & 1          # bit 1 of that word is the interlace flag of the v0.15 core
        if actual != session:
            raise RuntimeError('Core session changed. The core was reloaded.')
        if error:
            raise RuntimeError('FPGA reported a transfer or buffer error (%d).' % error)
        return consumed
