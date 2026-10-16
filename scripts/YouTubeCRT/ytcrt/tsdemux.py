"""A small MPEG-TS reader: the H.264 access units of a transport stream, each with its time stamps.

`ffmpeg -c:v copy -f mpegts` repackages YouTube's H.264 without decoding it (almost no CPU); the muxer writes one access unit per PES
packet, so a PES packet is what the H264 core's ring wants as one VIDEO_AU record. Time stamps are 90 kHz, 33 bits.
"""

TS_SIZE = 188
STREAM_H264 = 0x1B


def _pts(b):
    """The 33-bit time stamp in a PES header's 5-byte field."""
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


class TsVideo:
    """Feed bytes of a transport stream; get (pts, dts, access_unit) for every complete H.264 access unit.

    The access unit that is still arriving is held back until the next one starts (or `flush()`)."""

    def __init__(self):
        self.buf = bytearray()
        self.pmt_pid = None
        self.video_pid = None
        self.cur = None                       # [pts, dts, bytearray] of the access unit being collected
        self.packets = 0
        self.skipped = 0

    def feed(self, data):
        self.buf += data
        out = []
        buf = self.buf
        pos = 0
        n = len(buf)
        while n - pos >= TS_SIZE:
            if buf[pos] != 0x47:
                nxt = buf.find(b'\x47', pos + 1)
                self.skipped += 1
                if nxt < 0:
                    pos = n
                    break
                pos = nxt
                continue
            self._packet(buf[pos:pos + TS_SIZE], out)
            pos += TS_SIZE
        del buf[:pos]
        return out

    def flush(self):
        out = []
        if self.cur is not None and self.cur[2]:
            out.append((self.cur[0], self.cur[1], bytes(self.cur[2])))
        self.cur = None
        return out

    # ------------------------------------------------------------------------------------------------------------------------
    def _packet(self, p, out):
        self.packets += 1
        pusi = bool(p[1] & 0x40)
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        if not afc & 1:
            return                             # no payload
        start = 4
        if afc & 2:
            start += 1 + p[4]
        if start >= TS_SIZE:
            return
        payload = p[start:]
        if pid == 0 and self.pmt_pid is None:
            self._pat(payload, pusi)
        elif pid == self.pmt_pid and self.video_pid is None:
            self._pmt(payload, pusi)
        elif pid == self.video_pid:
            if pusi:
                if self.cur is not None and self.cur[2]:
                    out.append((self.cur[0], self.cur[1], bytes(self.cur[2])))
                self.cur = self._pes_start(payload)
            elif self.cur is not None:
                self.cur[2] += payload

    def _pat(self, payload, pusi):
        if not pusi:
            return
        s = payload[1 + payload[0]:]          # after the pointer field
        if len(s) < 12 or s[0] != 0:
            return
        length = ((s[1] & 0x0F) << 8) | s[2]
        i = 8
        while i + 4 <= min(len(s) - 4, 3 + length - 4):
            program = (s[i] << 8) | s[i + 1]
            if program != 0:
                self.pmt_pid = ((s[i + 2] & 0x1F) << 8) | s[i + 3]
                return
            i += 4

    def _pmt(self, payload, pusi):
        if not pusi:
            return
        s = payload[1 + payload[0]:]
        if len(s) < 16 or s[0] != 2:
            return
        length = ((s[1] & 0x0F) << 8) | s[2]
        info = ((s[10] & 0x0F) << 8) | s[11]
        i = 12 + info
        end = min(len(s), 3 + length - 4)
        while i + 5 <= end:
            stype = s[i]
            pid = ((s[i + 1] & 0x1F) << 8) | s[i + 2]
            es_info = ((s[i + 3] & 0x0F) << 8) | s[i + 4]
            if stype == STREAM_H264:
                self.video_pid = pid
                return
            i += 5 + es_info

    @staticmethod
    def _pes_start(payload):
        if len(payload) < 9 or payload[0] != 0 or payload[1] != 0 or payload[2] != 1:
            return [None, None, bytearray()]
        flags = payload[7]
        header_len = payload[8]
        pts = dts = None
        if flags & 0x80 and len(payload) >= 14:
            pts = _pts(payload[9:14])
            dts = pts
            if flags & 0x40 and len(payload) >= 19:
                dts = _pts(payload[14:19])
        return [pts, dts, bytearray(payload[9 + header_len:])]
