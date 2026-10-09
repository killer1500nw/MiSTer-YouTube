import platform
import re
import struct
from pathlib import Path
BASE=0x30000000

def check_layout():
    if platform.machine() != 'armv7l':
        raise RuntimeError('This check only runs on the verified MiSTer ARMv7 platform.')
    root = Path('/sys/firmware/devicetree/base')
    if not root.is_dir():
        root = Path('/proc/device-tree')
    if (root/'memory/reg').read_bytes() != struct.pack('>II', 0, 0x40000000):
        raise RuntimeError('Physical RAM layout differs from the supplied report.')
    if 'mem=511M' not in Path('/proc/cmdline').read_text().split():
        raise RuntimeError('Expected mem=511M Linux limit is missing.')
    ranges = []
    for line in Path('/proc/iomem').read_text().splitlines():
        match = re.match(r'\s*([0-9a-f]+)-([0-9a-f]+)\s*:\s*(.*)', line)
        if match:
            low, high = int(match[1], 16), int(match[2], 16)
            if low <= BASE+0x201000-1 and high >= BASE:
                raise RuntimeError('Mailbox overlaps a reported memory resource: ' + match[3])
            if match[3] == 'System RAM':
                ranges.append((low, high))
    if ranges != [(0, 0x1fefffff)]:
        raise RuntimeError('Linux RAM map differs from the supplied report.')
    fb = (root/'MiSTer_fb/reg').read_bytes()
    if fb != struct.pack('>II', 0x22000000, 0x00800000):
        raise RuntimeError('Framebuffer reservation differs from the supplied report.')

