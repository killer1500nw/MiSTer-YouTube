#!/usr/bin/env python3
"""Native ARM copy helper and CPU-owned DDR mailbox access.

Two cores speak this protocol: v0.14-auto (RGB565 records, identity 0x34445459) and v0.15-yuv (YUV pictures scaled by the
FPGA, identity 0x35445459, see docs/FPGA-YUV-v0.15.md). The mailbox words are the same; the slot layout differs.
"""
from pathlib import Path
import ctypes
import os
from .memory_layout import check_layout, BASE
from . import yuv

IDENT_RGB = 0x34445459   # v0.14-auto core; v0.13 used 0x33445459
IDENT_YUV = 0x35445459   # v0.15-yuv core
IDENT = IDENT_RGB        # kept for older callers
IDENTS = (IDENT_YUV, IDENT_RGB)
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


def address_of(buf):
    """Address of the first byte of a bytes / bytearray / memoryview (writable or not) or a ctypes array."""
    if isinstance(buf, ctypes.Array):
        return ctypes.addressof(buf)
    if isinstance(buf, (bytearray, memoryview)) and not (isinstance(buf, memoryview) and buf.readonly):
        return ctypes.addressof(ctypes.c_char.from_buffer(buf))
    if isinstance(buf, memoryview):
        buf = buf.tobytes()
    return ctypes.cast(ctypes.c_char_p(buf), ctypes.c_void_p).value


COPY_METHODS = ('memmove', 'pwrite')


class Mailbox:
    def __init__(self, memory, fd=None):
        self.words = (ctypes.c_uint32 * (len(memory) // 4)).from_buffer(memory)
        self.base = ctypes.addressof(self.words)
        self.fd = fd                    # /dev/mem opened O_SYNC: lets copy_in write with one system call instead of memmove
        self.method = 'memmove'

    def use_method(self, method):
        """Pick how bulk copies are made ('memmove' into the mapping, or 'pwrite' to /dev/mem); ignores what it cannot do."""
        if method == 'pwrite' and self.fd is not None and hasattr(os, 'pwrite'):
            self.method = 'pwrite'
        elif method == 'memmove':
            self.method = 'memmove'

    def close(self):
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
        if self.method == 'pwrite':
            view = memoryview(src).cast('B')[:n]
            done = 0
            while done < n:
                wrote = os.pwrite(self.fd, view[done:], BASE + offset + done)
                if wrote <= 0:                      # /dev/mem takes no writes here: fail instead of looping forever
                    raise OSError('pwrite to /dev/mem wrote nothing')
                done += wrote
        else:
            ctypes.memmove(self.base + offset, addr, n)
        k = min(n, 16)
        if ctypes.string_at(self.base + offset, k) != ctypes.string_at(addr, k) or \
                ctypes.string_at(self.base + offset + n - k, k) != ctypes.string_at(addr + n - k, k):
            raise RuntimeError('Bulk copy verification failed.')

    def picture(self, number, header, frame, ysize, audio):
        """v0.15 core: header, Y/U/V planes (`frame` = Y then U then V, ysize bytes of Y) and PCM into slot `number`."""
        if len(audio) != yuv.AUDIO_BYTES or number < 0:
            raise ValueError('Incorrect audio length or index.')
        base = yuv.slot_offset(number)
        csize = ysize // 4
        if len(frame) != ysize + 2 * csize:
            raise ValueError('Incorrect picture size.')
        view = memoryview(frame)
        self.copy_in(base + yuv.Y_OFF, view[:ysize])
        self.copy_in(base + yuv.U_OFF, view[ysize:ysize + csize])
        self.copy_in(base + yuv.V_OFF, view[ysize + csize:])
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
