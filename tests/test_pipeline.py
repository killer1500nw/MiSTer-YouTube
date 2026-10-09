import sys, time, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'/'YouTubeURL'))
import url_player as p

class Fake:
    def __init__(self, count=20, error=False, stall=False):
        self.n=0; self.count=count; self.error=error; self.stall=stall; self.closed=False
    def next_record(self, health):
        while self.stall:
            health();time.sleep(.005)
        health()
        if self.n==self.count:
            if self.error:raise ValueError('decoder failure')
            return None
        n=self.n;self.n+=1;return n.to_bytes(4,'little')
    def close(self):self.closed=True

class Tests(unittest.TestCase):
    def test_order_eof_and_read_ahead(self):
        d=Fake();q=p.RecordPipeline(d)
        try:
            deadline=time.monotonic()+1
            while q.ready.qsize()<4 and time.monotonic()<deadline:time.sleep(.005)
            self.assertEqual(q.ready.qsize(),4)
            self.assertLessEqual(d.n,5) # four queued + one blocked producer record
            self.assertEqual([q.next_record(lambda:None) for _ in range(20)],
                             [n.to_bytes(4,'little') for n in range(20)])
            self.assertIsNone(q.next_record(lambda:None))
        finally:q.close()
        self.assertTrue(d.closed)
    def test_decoder_error_order(self):
        q=p.RecordPipeline(Fake(2,error=True))
        try:
            self.assertEqual(q.next_record(lambda:None),bytes(4))
            self.assertEqual(q.next_record(lambda:None),(1).to_bytes(4,'little'))
            with self.assertRaisesRegex(ValueError,'decoder failure'):q.next_record(lambda:None)
        finally:q.close()
    def test_stop_full_and_stalled(self):
        for stall in (False,True):
            d=Fake(stall=stall);q=p.RecordPipeline(d);time.sleep(.04)
            q.close();self.assertFalse(q.thread.is_alive());self.assertTrue(d.closed)
    def test_health_failure(self):
        q=p.RecordPipeline(Fake(stall=True))
        def failed():raise RuntimeError('core changed')
        try:
            with self.assertRaisesRegex(RuntimeError,'core changed'):q.next_record(failed)
        finally:q.close()
    def test_real_pipes_pairing_and_audio_padding(self):
        # Actual child pipes with different chunk sizes and a short final audio block.
        commands={}
        for kind,size,count,chunk in [('video',p.VIDEO_BYTES,12,7777),('audio',p.AUDIO_BYTES,11,997)]:
            code=('import os\n'
                  'data=b"".join(bytes([i])*%d for i in range(%d))\n' % (size,count))
            if kind=='audio':code+='data+=bytes([11])*3200\n'
            code+='for i in range(0,len(data),%d):\n os.write(1,data[i:i+%d])\n' % (chunk,chunk)
            commands[kind]=[sys.executable,'-c',code]
        q=p.RecordPipeline(p.Decoders(commands))
        try:
            for i in range(12):
                r=q.next_record(lambda:None)
                self.assertEqual(r[:p.VIDEO_BYTES],bytes([i])*p.VIDEO_BYTES)
                self.assertEqual(r[p.VIDEO_BYTES:],bytes([i])*(p.AUDIO_BYTES if i<11 else 3200)+(b'\0'*3200 if i==11 else b''))
            self.assertIsNone(q.next_record(lambda:None))
        finally:q.close()

if __name__=='__main__':unittest.main()
