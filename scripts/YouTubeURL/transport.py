#!/usr/bin/env python3
"""Native ARM copy helper and CPU-owned DDR mailbox access."""
from pathlib import Path
import ctypes
from memory_layout import check_layout, BASE

IDENT = 0x33445459
RUN = 0x33515459
MAP_BYTES = 0x201000
SLOTS = 8
RECORD_BYTES = 160000
FRAME_SECONDS = 666528 / 20000000


NATIVE = None

def load_native(path=None):
    global NATIVE
    if NATIVE is None:
        location = Path(path) if path else Path(__file__).with_name('libyt_sender.so')
        lib = ctypes.CDLL(str(location))
        lib.yt_native_version.argtypes = []
        lib.yt_native_version.restype = ctypes.c_int
        lib.yt_copy_slot.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
        lib.yt_copy_slot.restype = ctypes.c_int
        if lib.yt_native_version() != 130:
            raise RuntimeError('Incorrect native helper version; copy the complete YouTubeStreamSmooth folder.')
        NATIVE = lib
    return NATIVE


class Mailbox:
    def __init__(self, memory):
        self.words = (ctypes.c_uint32 * (MAP_BYTES // 4)).from_buffer(memory)

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
        if len(data) != RECORD_BYTES or number < 0:
            raise ValueError('Incorrect record length or index.')
        source = ctypes.create_string_buffer(data, RECORD_BYTES)
        result = load_native().yt_copy_slot(ctypes.addressof(self.words), number % SLOTS,
                                            ctypes.addressof(source), RECORD_BYTES)
        if result:
            raise RuntimeError('Native record copy failed (%d).' % result)

    def status(self, session):
        actual = self.read(20)
        consumed = self.read(16)
        error = self.read(28)
        if actual != session:
            raise RuntimeError('Core session changed. Keep v0.13-smooth loaded and RAM streaming On.')
        if error:
            raise RuntimeError('FPGA reported a transfer or buffer error (%d).' % error)
        return consumed

