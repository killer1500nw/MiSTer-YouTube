"""Exercise the real framebuffer opener without MiSTer devices or physical memory."""
import mmap
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'YouTubeCRT'))
from ytcrt import h264link, transport, yuv


class FramebufferOpen(unittest.TestCase):
    def open_framebuffer(self, length, overlay_only=False, start=0x22001000):
        backing = tempfile.TemporaryFile()
        self.addCleanup(backing.close)
        opened, mappings = [], []
        real_mmap = mmap.mmap

        def open_device(path, flags):
            fd = os.dup(backing.fileno())
            opened.append((path, fd))
            return fd

        def info(fd, request, data):
            self.assertEqual(request, 0x4602)
            struct.pack_into('<II', data, 16, start, length)

        def map_device(fd, size, *args, **kwargs):
            region = real_mmap(-1, size)
            mappings.append((size, kwargs.get('offset', 0), region))
            self.addCleanup(region.close)
            return region

        with mock.patch('os.open', side_effect=open_device), \
                mock.patch('fcntl.ioctl', side_effect=info), \
                mock.patch('mmap.mmap', side_effect=map_device), \
                mock.patch.object(transport, 'quiet_console'):
            fb = transport.Framebuffer.open(overlay_only=overlay_only)
        if fb is not None:
            self.addCleanup(fb.close)
        return fb, opened, mappings

    def test_reported_6220800_byte_framebuffer_accepts_h264_overlay(self):
        fb, opened, mappings = self.open_framebuffer(6220800, overlay_only=True)
        self.assertIsNotNone(fb)
        self.assertEqual([path for path, fd in opened], ['/dev/fb0'])
        self.assertEqual([size for size, offset, region in mappings], [6220800])
        # Exercise all three physical/virtual pages, including the last byte of each.
        link = h264link.H264Link()
        link.fb = fb
        for i in range(yuv.OVERLAYS):
            offset = link._page_phys(i) - fb.start
            self.assertGreaterEqual(offset, 0)
            self.assertLessEqual(offset + h264link.PAGE_BYTES, fb.length)
            self.assertEqual(link._page_virt(i) - fb.base, offset)
            fb.fb_map[offset + h264link.PAGE_BYTES - 1] = i + 1

    def test_overlay_exact_minimum_is_accepted(self):
        fb, _, _ = self.open_framebuffer(h264link.PAGE_BYTES * yuv.OVERLAYS, overlay_only=True)
        self.assertIsNotNone(fb)

    def test_overlay_one_byte_too_small_is_rejected_and_fd_closed(self):
        fb, opened, mappings = self.open_framebuffer(h264link.PAGE_BYTES * yuv.OVERLAYS - 1, overlay_only=True)
        self.assertIsNone(fb)
        self.assertEqual(mappings, [])
        with self.assertRaises(OSError):
            os.fstat(opened[0][1])

    def test_unaligned_address_is_still_rejected(self):
        fb, _, mappings = self.open_framebuffer(6220800, overlay_only=True, start=0x22001001)
        self.assertIsNone(fb)
        self.assertEqual(mappings, [])

    def test_video_slots_still_reject_the_reported_small_framebuffer(self):
        fb, _, mappings = self.open_framebuffer(6220800)
        self.assertIsNone(fb)
        self.assertEqual(mappings, [])

    def test_video_slots_keep_their_uncached_readback_mapping(self):
        fb, opened, mappings = self.open_framebuffer(yuv.FB_MIN_LENGTH)
        self.assertIsNotNone(fb)
        self.assertEqual([path for path, fd in opened], ['/dev/fb0', '/dev/mem'])
        self.assertEqual(mappings[1][:2], (8 * yuv.FB_SLOT_STRIDE, fb.start + fb.slots_offset))
        self.assertIsNot(fb.fb_map, fb.view_map)

    def test_overlay_close_releases_shared_mapping_once_and_fd(self):
        fb, opened, mappings = self.open_framebuffer(6220800, overlay_only=True)
        region = mappings[0][2]
        fb.fb_map = fb.view_map = mock.Mock(wraps=region)
        fb.close()
        fb.fb_map.close.assert_called_once_with()
        self.assertTrue(region.closed)
        with self.assertRaises(OSError):
            os.fstat(opened[0][1])

    def test_h264_session_requests_overlay_only(self):
        link = h264link.H264Link()
        link.ring = mock.Mock()
        link.ring.raster.return_value = (720, 480)
        link.ring.input_snapshot.return_value = ([0] * 6, 0)
        fb = mock.Mock(start=0x22001000, length=6220800)
        with mock.patch.object(transport.Framebuffer, 'open', return_value=fb) as opener, \
                mock.patch.object(transport, 'console_bind', return_value=[]), \
                mock.patch.object(transport, 'console_graphics'):
            link.start_session()
        opener.assert_called_once_with(overlay_only=True)


if __name__ == '__main__':
    unittest.main()
