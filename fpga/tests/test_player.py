import contextlib
import functools
import http.server
import importlib.util
import io
import mmap
import os
from pathlib import Path
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MiSTer-Scripts'/'YouTubeStreamSmooth'))
import player as p
import transport as t

class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args):pass

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        subprocess.run(['gcc','-O2','-shared','-fPIC','-nostdlib','-fno-strict-aliasing',
                        str(p.HERE/'native_sender.c'),'-o',str(cls.root/'helper.so')],check=True)
        t.load_native(cls.root/'helper.so')
        cls.stop_patch=patch.object(p,'STOP',cls.root/'stop');cls.stop_patch.start()
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x240:rate=30',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                        '-t','1','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',
                        '-c:a','aac','-movflags','+faststart',str(cls.root/'sample.mp4')],check=True)
        handler=functools.partial(QuietHandler,directory=str(cls.root))
        cls.server=socketserver.ThreadingTCPServer(('127.0.0.1',0),handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.source={'url':'http://127.0.0.1:%d/sample.mp4'%cls.server.server_address[1], 'headers':{}, 'format_id':'test'}
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()
        cls.stop_patch.stop();cls.temp.cleanup()
    def test_urls_and_formats(self):
        self.assertEqual(p.normalise_url('jNQXAC9IVRw')[1],'jNQXAC9IVRw')
        with self.assertRaises(ValueError):p.normalise_url('https://example.org/video')
        v={'url':'https://example.org/v','protocol':'https','vcodec':'avc1','acodec':'none'}
        a={'url':'https://example.org/a','protocol':'https','vcodec':'none','acodec':'mp4a'}
        video,audio=p.selected_sources({'requested_formats':[v,a]})
        self.assertEqual(video['url'],v['url']);self.assertEqual(audio['url'],a['url'])
        self.assertIsNone(p.selected_sources(v)[1])
        with self.assertRaises(ValueError):p.selected_sources(dict(v,is_live=True))
        with self.assertRaises(ValueError):p.selected_sources(dict(v,http_headers={'X':'bad\r\nInjected: yes'}))
        command=p.resolve_command('yt-dlp','qjs','ffdir','https://youtube.com/watch?v=jNQXAC9IVRw')
        self.assertIn('--skip-download',command);self.assertIn('--dump-single-json',command)
        self.assertNotIn('--download-sections',command);self.assertNotIn('-o',command)
    def commands(self,audio=True):
        commands={'video':p.decoder_command('/usr/bin/ffmpeg',self.source,'video',p.HERE/'cacert.pem')}
        if audio:commands['audio']=p.decoder_command('/usr/bin/ffmpeg',self.source,'audio',p.HERE/'cacert.pem')
        return commands
    def test_http_ffmpeg_stream_to_mapped_ring(self):
        with tempfile.TemporaryFile() as f:
            f.truncate(t.MAP_BYTES)
            with mmap.mmap(f.fileno(),t.MAP_BYTES) as mem:
                box=t.Mailbox(mem);session=123
                struct.pack_into('<IIII',mem,16,0,session,0,0)
                struct.pack_into('<II',mem,32,t.IDENT,1)
                stop=threading.Event();errors=[];seen=[]
                def core():
                    count=0;heart=1;next_frame=0;started=False
                    try:
                        while not stop.is_set():
                            now=time.monotonic();heart+=1
                            struct.pack_into('<I',mem,36,heart)
                            published,end=struct.unpack_from('<II',mem,8)
                            assert 0<=published-count<=8
                            if published>count and (started or published-count>=4 or end==published) and now>=next_frame:
                                off=4096+(count%8)*262144
                                data=bytes(mem[off:off+160000]);assert len(data)==160000
                                seen.append(data);count+=1;started=True;next_frame=now+t.FRAME_SECONDS
                                struct.pack_into('<III',mem,16,count,session,count)
                            time.sleep(.001)
                    except BaseException as e:errors.append(e)
                thread=threading.Thread(target=core);thread.start();dec=p.Decoders(self.commands())
                try:
                    with contextlib.redirect_stdout(io.StringIO()):p.feed(box,session,dec)
                    self.assertFalse(errors,errors);self.assertEqual(len(seen),30)
                    self.assertTrue(any(x!=0 for x in seen[5][153600:]))
                    self.assertNotEqual(seen[0][:153600],seen[-1][:153600])
                finally:stop.set();thread.join();dec.close();box.close()
    def test_video_only_and_bounded_pipes(self):
        dec=p.Decoders(self.commands(False));count=0
        try:
            while True:
                data=dec.next_record(lambda:None)
                if data is None:break
                self.assertEqual(data[153600:],bytes(6400));count+=1
                self.assertLessEqual(len(dec.data['video']),153600)
                self.assertLessEqual(len(dec.data['audio']),6400)
                time.sleep(.005)
            self.assertEqual(count,30)
        finally:dec.close()
    def test_partial_frame_and_decoder_failure(self):
        for code in ("import os;os.write(1,b'x')", "import sys;sys.exit(7)"):
            dec=p.Decoders({'video':[sys.executable,'-c',code]})
            try:
                with self.assertRaises(RuntimeError):dec.next_record(lambda:None)
            finally:dec.close()
    def test_cancellation(self):
        dec=p.Decoders({'video':[sys.executable,'-c','import time;time.sleep(60)']})
        try:
            p.STOP.touch()
            with self.assertRaisesRegex(RuntimeError,'Stopped'):dec.next_record(p.cancelled)
        finally:p.STOP.unlink();dec.close()
        self.assertIsNotNone(dec.procs['video'].poll())
    def test_startup_clears_stale_controls_before_off_on(self):
        class Clock:
            now=0
            def tick(self,seconds):self.now+=seconds
        clock=Clock()
        class Box:
            session=999
            writes=[]
            def read(self,off):
                if off==32:return t.IDENT
                if off==36:return int(min(clock.now,.2)*1000) if clock.now<1.2 else int(clock.now*1000)
                if off==20:return self.session
                if off==28:return 1 if clock.now<1.2 else 0
                return 0
            def command(self,session):
                self.session=session;self.writes.append((clock.now,'session',session))
            def write(self,offset,value):self.writes.append((clock.now,offset,value))
        box=Box()
        with patch.object(p.time,'monotonic',lambda:clock.now),patch.object(p.time,'sleep',clock.tick),contextlib.redirect_stdout(io.StringIO()):
            session=p.prepare_core(box)
        self.assertGreater(session,0)
        self.assertEqual([(o,v) for _,o,v in box.writes[:3]],[('session',0),(8,0),(12,0)])
        self.assertLess(box.writes[2][0],.2)
        self.assertGreaterEqual(box.writes[-1][0],1.2)
    def test_https_verified_ffmpeg_stream(self):
        import ssl
        cert=self.root/'cert.pem';key=self.root/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
                        '-keyout',str(key),'-out',str(cert),'-days','1',
                        '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        handler=functools.partial(QuietHandler,directory=str(self.root))
        server=socketserver.ThreadingTCPServer(('127.0.0.1',0),handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key)
        server.socket=ctx.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        source=dict(self.source,url='https://localhost:%d/sample.mp4'%server.server_address[1])
        dec=None
        try:
            dec=p.Decoders({'video':p.decoder_command('/usr/bin/ffmpeg',source,'video',cert)})
            count=0
            while dec.next_record(lambda:None) is not None:count+=1
            self.assertEqual(count,30)
        finally:
            if dec:dec.close()
            server.shutdown();server.server_close();thread.join()
    def test_duplicate_launch_preserves_report(self):
        report=self.root/'report.txt';report.write_text('existing report')
        lockpath=self.root/'lock'
        import fcntl
        with lockpath.open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(p,'ROOT',self.root),patch.object(p,'REPORT',report),patch.object(p,'LOCK',lockpath),contextlib.redirect_stdout(io.StringIO()):
                p.launch()
        self.assertEqual(report.read_text(),'existing report')

if __name__=='__main__':unittest.main()
