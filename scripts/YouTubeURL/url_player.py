#!/usr/bin/env python3
"""On-device YouTube URL -> FFmpeg pipes -> bounded DDR ring. No media files."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import mmap
import os
from pathlib import Path
import re
import secrets
import select
import queue
import threading
import signal
import subprocess
import sys
import time
from urls import normalise_url
import transport as t

HERE = Path(__file__).resolve().parent
ROOT = Path('/media/fat/YouTubeNative')
REPORT = ROOT / 'YouTube-url-13-7.txt'
STOP = Path('/tmp/YouTubeURL.stop')
LOCK = Path('/tmp/YouTubeStreamTest.lock')
VIDEO_BYTES, AUDIO_BYTES = 153600, 6400


def cancelled():
    if STOP.exists():
        raise RuntimeError('Stopped by the stop script.')


def stop_process(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
        except ProcessLookupError:
            pass
    for stream in (proc.stdout, proc.stderr):
        if stream:
            stream.close()


def safe_text(text):
    text = re.sub(r'https?://[^\s]+', '[URL]', text)
    return ''.join(c for c in text if c in '\n\t' or ord(c) >= 32)[:4096]


def capture(command, env, timeout=180):
    proc = subprocess.Popen([str(x) for x in command], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=env)
    chunks = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
    active = list(chunks)
    deadline = time.monotonic() + timeout
    try:
        while active:
            cancelled()
            if time.monotonic() > deadline:
                raise RuntimeError('YouTube URL resolution timed out.')
            ready, _, _ = select.select(active, [], [], 0.2)
            for fd in ready:
                data = os.read(fd, 65536)
                if not data:
                    active.remove(fd)
                    continue
                chunks[fd].extend(data)
                if len(chunks[fd]) > 16*1024*1024:
                    raise RuntimeError('Resolver output exceeded its bounded metadata limit.')
        code = proc.wait(timeout=2)
        errors = chunks[proc.stderr.fileno()].decode(errors='replace')
        if errors.strip():
            print('Resolver: ' + safe_text(errors[-4096:]), flush=True)
        if code:
            raise RuntimeError('YouTube resolver failed (status %d).' % code)
        return bytes(chunks[proc.stdout.fileno()])
    finally:
        stop_process(proc)


def resolve_command(resolver, qjs, ffdir, url):
    v = '[ext=mp4][vcodec^=avc1][height<=240][width<=426][fps<=30]'
    selector = 'bv'+v+'+ba[ext=m4a][acodec^=mp4a]/b'+v+'/bv'+v
    return [resolver, '--ignore-config', '--no-playlist', '--no-progress',
            '--no-cache-dir', '--skip-download', '--dump-single-json',
            '--socket-timeout', '20', '--retries', '1', '--fragment-retries', '1',
            '--js-runtimes', 'quickjs:'+str(qjs), '--compat-options', 'no-certifi',
            '--match-filters', '!is_live & !is_upcoming', '--ffmpeg-location', ffdir,
            '-f', selector, '--', url]


def selected_sources(info):
    if info.get('is_live') or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        raise ValueError('This first streaming version supports completed videos, not live broadcasts.')
    formats = info.get('requested_formats') or [info]
    videos = [f for f in formats if f.get('vcodec') not in (None, 'none')]
    audios = [f for f in formats if f.get('acodec') not in (None, 'none')]
    if not videos:
        raise ValueError('No supported video stream was resolved.')
    def source(fmt):
        url = fmt.get('url', '')
        if not url.startswith(('https://', 'http://')):
            raise ValueError('Resolver returned an unsupported stream URL.')
        if fmt.get('protocol') not in ('https', 'http', 'm3u8', 'm3u8_native'):
            raise ValueError('Unsupported media protocol: %s.' % fmt.get('protocol'))
        headers = dict(info.get('http_headers') or {})
        headers.update(fmt.get('http_headers') or {})
        clean = {}
        for key, value in headers.items():
            if not re.fullmatch(r'[A-Za-z0-9-]+', str(key)) or any(c in str(value) for c in '\r\n\x00'):
                raise ValueError('Invalid media HTTP header.')
            clean[str(key)] = str(value)
        return {'url': url, 'headers': clean, 'format_id': str(fmt.get('format_id', '?')), 'width': fmt.get('width'), 'height': fmt.get('height'), 'fps': fmt.get('fps')}
    return source(videos[0]), source(audios[0]) if audios else None


def decoder_command(ffmpeg, source, kind, ca):
    args = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-threads', '2', '-filter_threads', '1', '-rw_timeout', '20000000',
            '-protocol_whitelist', 'http,https,tcp,tls,crypto']
    if source['url'].startswith('https://'):
        args += ['-tls_verify', '1', '-ca_file', str(ca)]
    if source['headers']:
        args += ['-headers', ''.join(k+': '+v+'\r\n' for k,v in source['headers'].items())]
    args += ['-i', source['url']]
    if kind == 'video':
        args += ['-map', '0:v:0', '-an', '-vf',
                 'fps=30:start_time=0,scale=320:240:force_original_aspect_ratio=decrease:flags=fast_bilinear,pad=320:240:(ow-iw)/2:(oh-ih)/2,setsar=1',
                 '-pix_fmt', 'rgb565le', '-threads', '1', '-f', 'rawvideo']
    else:
        args += ['-map', '0:a:0', '-vn', '-af', 'aresample=48000:async=1:first_pts=0',
                 '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', '-f', 's16le']
    return args + ['-blocksize', '32768', 'pipe:1']


class Decoders:
    """Drain both streams independently; userspace holds at most one A/V pair."""
    def __init__(self, commands):
        self.procs = {}
        self.data = {'video': bytearray(), 'audio': bytearray()}
        self.eof = {'video': False, 'audio': 'audio' not in commands}
        self.errors = {}
        self.last_data = time.monotonic()
        try:
            for kind, cmd in commands.items():
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
                self.procs[kind] = p
                self.errors[kind] = bytearray()
        except BaseException:
            self.close()
            raise

    def close(self):
        for proc in self.procs.values():
            stop_process(proc)

    def next_record(self, health):
        limits = {'video': VIDEO_BYTES, 'audio': AUDIO_BYTES}
        started = time.monotonic()
        while True:
            health()
            if self.eof['video'] and not self.data['video']:
                return None
            if self.eof['video'] and len(self.data['video']) != VIDEO_BYTES:
                raise RuntimeError('Decoder ended with a partial video frame.')
            if len(self.data['video']) == VIDEO_BYTES and (len(self.data['audio']) == AUDIO_BYTES or self.eof['audio']):
                if len(self.data['audio']) % 4:
                    raise RuntimeError('Decoder ended with a partial stereo sample.')
                record = bytes(self.data['video']) + bytes(self.data['audio']).ljust(AUDIO_BYTES,b'\x00')
                self.data['video'].clear();self.data['audio'].clear()
                return record
            if time.monotonic()-started > 45:
                raise RuntimeError('No complete video/audio record within 45 seconds. Network or decoder stalled.')
            readers = {}
            for kind, proc in self.procs.items():
                if not self.eof[kind] and len(self.data[kind]) < limits[kind]:
                    readers[proc.stdout.fileno()] = (kind, False)
                if not proc.stderr.closed:
                    readers[proc.stderr.fileno()] = (kind, True)
            ready, _, _ = select.select(list(readers), [], [], .05)
            for fd in ready:
                kind, is_error = readers[fd]
                buf = self.errors[kind] if is_error else self.data[kind]
                count = 4096 if is_error else limits[kind]-len(buf)
                chunk = os.read(fd, count)
                if is_error:
                    if chunk:
                        buf.extend(chunk)
                        if len(buf)>8192: del buf[:-8192]
                    else:
                        self.procs[kind].stderr.close()
                elif chunk:
                    buf.extend(chunk)
                    self.last_data = time.monotonic()
                else:
                    self.eof[kind] = True
                    code = self.procs[kind].wait(timeout=2)
                    if code:
                        message = safe_text(self.errors[kind].decode(errors='replace'))
                        raise RuntimeError('%s decoder failed (%d): %s' % (kind,code,message))


class RecordPipeline:
    """One pipe reader, bounded A/V records, one main-thread DDR writer.

    The reader never accesses the mailbox. ctypes.CDLL releases the GIL
    during native copies, allowing the reader to drain FFmpeg concurrently.
    """
    CAPACITY = 4

    def __init__(self, decoder):
        self.decoder = decoder
        self.ready = queue.Queue(maxsize=self.CAPACITY)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._produce, name='youtube-pipe-reader', daemon=True)
        self.thread.start()

    def _check(self):
        if self.stopping.is_set():
            raise RuntimeError('Pipe reader stopped.')
        cancelled()

    def _put(self, item):
        while not self.stopping.is_set():
            try:
                self.ready.put(item, timeout=.05)
                return
            except queue.Full:
                pass

    def _produce(self):
        try:
            while not self.stopping.is_set():
                record = self.decoder.next_record(self._check)
                self._put((record, None))
                if record is None:
                    return
        except Exception as exc:
            self._put((None, exc))

    def next_record(self, health):
        while True:
            health()
            try:
                record, error = self.ready.get(timeout=.05)
            except queue.Empty:
                if not self.thread.is_alive():
                    raise RuntimeError('Pipe reader exited without an end marker.')
                continue
            if error is not None:
                raise error
            return record

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('Pipe reader did not stop within five seconds.')
        self.decoder.close()


def wait_until(test, timeout, message):
    end = time.monotonic()+timeout
    while time.monotonic()<end:
        cancelled()
        if test(): return
        time.sleep(.01)
    raise RuntimeError(message)


def prepare_core(box):
    print('Waiting for v0.13-smooth > RAM streaming On.', flush=True)
    previous = [None]
    def live():
        identity, heart = box.read(32), box.read(36)
        ok = identity == t.IDENT and previous[0] is not None and heart != previous[0]
        previous[0] = heart if identity == t.IDENT else None
        return ok
    wait_until(live,120,'No live v0.13-smooth core detected.')
    box.command(0)
    heart = box.read(36)
    wait_until(lambda: box.read(20)==0 and box.read(36)!=heart,5,'Core did not acknowledge stop.')
    box.write(8,0);box.write(12,0)
    # v0.10 latches errors from stale producer counts across core reloads.
    # Clear CPU controls first, then require one Off/On cycle before playback.
    print('Controls cleared. Set RAM streaming Off for TWO seconds, then On.', flush=True)
    heartbeat = box.read(36);changed = time.monotonic();paused = False
    def reset_seen():
        nonlocal heartbeat,changed,paused
        now=time.monotonic();current=box.read(36)
        if current!=heartbeat:
            resumed=paused
            heartbeat=current;changed=now
            return resumed and box.read(28)==0 and box.read(20)==0 and box.read(32)==t.IDENT
        if now-changed>.5:paused=True
        return False
    wait_until(reset_seen,120,'Startup Off/On cycle not detected. Follow the startup sequence in README.')
    session=secrets.randbelow(0xfffffffe)+1
    box.command(session)
    wait_until(lambda: box.read(20)==session,5,'Core refused the new session.')
    return session


def feed(box, session, decoders):
    max_copy=total_copy=max_decode_wait=total_decode_wait=0.0
    slow_copies=0
    sent=consumed=0;first=None;last_progress=time.monotonic();next_log=last_progress+5
    heart=box.read(36);heart_time=last_progress
    def health():
        nonlocal consumed,first,last_progress,next_log,heart,heart_time
        cancelled();now=time.monotonic()
        if box.read(32)!=t.IDENT:raise RuntimeError('Core identity changed. Playback stopped.')
        h=box.read(36)
        if h!=heart:heart=h;heart_time=now
        if now-heart_time>3:raise RuntimeError('Core heartbeat stopped. RAM streaming may be Off.')
        current=box.status(session)
        if current<consumed or current>sent:raise RuntimeError('Invalid consumer sequence.')
        if current!=consumed:
            consumed=current;last_progress=now
            if first is None:first=now
        if sent-consumed>=4 and now-last_progress>5:
            raise RuntimeError('Core stopped consuming a nonempty buffer.')
        if now>=next_log:
            print('Progress: sent=%d played=%d queued=%d elapsed=%.2fs' %
                  (sent,consumed,sent-consumed,0 if first is None else now-first),flush=True)
            if sent:
                print('Timing: copy_mean_ms=%.3f copy_max_ms=%.3f copy_over_budget=%d ready_wait_mean_ms=%.3f ready_wait_max_ms=%.3f budget_ms=%.3f' %
                      (total_copy/sent*1000,max_copy*1000,slow_copies,
                       total_decode_wait/sent*1000,max_decode_wait*1000,t.FRAME_SECONDS*1000),flush=True)
            print('Pipeline: ready_records=%d capacity=%d' % (decoders.ready.qsize(), decoders.CAPACITY),flush=True)
            next_log=now+5
    while True:
        health()
        if sent-consumed>=t.SLOTS:
            time.sleep(.002);continue
        decode_start=time.monotonic()
        record=decoders.next_record(health)
        decode_wait=time.monotonic()-decode_start
        max_decode_wait=max(max_decode_wait,decode_wait)
        total_decode_wait+=decode_wait
        if record is None:break
        health()
        if sent>=0xffffff00:raise RuntimeError('Session record limit reached.')
        copy_start=time.monotonic()
        box.record(sent,record)
        copy_time=time.monotonic()-copy_start
        max_copy=max(max_copy,copy_time);total_copy+=copy_time
        slow_copies+=int(copy_time>t.FRAME_SECONDS)
        if sent-consumed == 3:last_progress=time.monotonic()
        sent+=1;box.write(8,sent)
    if not sent:raise RuntimeError('The video decoder produced no frames.')
    box.write(12,sent)
    while consumed<sent:
        health();time.sleep(.002)
    time.sleep(t.FRAME_SECONDS+.05)
    duration=0 if first is None else time.monotonic()-first
    print('COMPLETE: %d A/V records streamed; nominal content %.2fs; playback elapsed %.2fs.' %
          (sent,sent/30,duration),flush=True)
    print('Native copy: mean=%.6fs max=%.6fs budget=%.6fs over_budget=%d.' % (total_copy/sent,max_copy,t.FRAME_SECONDS,slow_copies),flush=True)
    print('Ready-record wait: mean=%.6fs max=%.6fs (consumer wait for queued A/V; not decoder CPU time).' % (total_decode_wait/sent,max_decode_wait),flush=True)
    print('No movie file was downloaded or saved. Report any pauses or audio/video mismatch.',flush=True)


def worker(url, lock_fd):
    # Own the inherited locked descriptor for the whole job. Decoder children
    # do not inherit it; duplicate launchers cannot truncate this report.
    with os.fdopen(lock_fd,'a'):
        print('YouTube native URL streaming v0.13.7 URL; core v0.13-smooth.',flush=True)
        print('Run started (MiSTer clock, UTC): '+datetime.now(timezone.utc).isoformat(),flush=True)
        print('Worker script: '+str(Path(__file__).resolve()),flush=True)
        print('Report: '+str(REPORT),flush=True)
        print('320x240 RGB565, 30fps source, stereo PCM; 8 x 256KiB reusable slots.',flush=True)
        runtime=ROOT/'resolver-2026.08.19-qjs-0.17.0'
        resolver=runtime/'bundle/yt-dlp_linux_armv7l';qjs=runtime/'qjs'
        ffdir=ROOT/'ffmpeg-7.0.2-armhf-static';ca=HERE/'cacert.pem'
        for p in (resolver,qjs,ffdir/'ffmpeg',ca,HERE/'libyt_sender.so'):
            if not p.is_file():raise RuntimeError('Required installed file missing: '+str(p))
        t.check_layout();t.load_native()
        fd=os.open('/dev/mem',os.O_RDWR|os.O_SYNC)
        try:
            with mmap.mmap(fd,t.MAP_BYTES,flags=mmap.MAP_SHARED,
                           prot=mmap.PROT_READ|mmap.PROT_WRITE,offset=t.BASE) as memory:
                box=t.Mailbox(memory);session=None;decoders=None
                try:
                    session=prepare_core(box)
                    print('Resolving '+url+' (metadata only)...',flush=True)
                    env=os.environ.copy()
                    env.update(SSL_CERT_FILE=str(ca),REQUESTS_CA_BUNDLE=str(ca),CURL_CA_BUNDLE=str(ca))
                    info=json.loads(capture(resolve_command(resolver,qjs,ffdir,url),env))
                    video,audio=selected_sources(info)
                    print('Title: '+safe_text(str(info.get('title',''))),flush=True)
                    print('Formats: video=%s audio=%s' % (video['format_id'],audio['format_id'] if audio else 'silence'),flush=True)
                    print('Selected video: %sx%s at %s fps; fast bilinear scaling.' % (video['width'],video['height'],video['fps']),flush=True)
                    print('Full-clip playback: runs to the end of the video.',flush=True)
                    # Recheck session after resolver wait; no payload goes to a changed core.
                    heart=box.read(36)
                    wait_until(lambda: box.read(32)==t.IDENT and box.read(36)!=heart,
                               3,'Core stopped responding during URL resolution.')
                    box.status(session)
                    commands={'video':decoder_command(ffdir/'ffmpeg',video,'video',ca)}
                    if audio:commands['audio']=decoder_command(ffdir/'ffmpeg',audio,'audio',ca)
                    decoders=RecordPipeline(Decoders(commands))
                    print('Pipeline: four RAM records; pipe reading overlaps native FPGA copies. No media files.',flush=True)
                    feed(box,session,decoders)
                finally:
                    try:
                        if decoders:decoders.close()
                        # Stop only our acknowledged session on a responsive core.
                        if session is not None and box.read(32)==t.IDENT and box.read(20)==session:
                            h=box.read(36);until=time.monotonic()+.2
                            while time.monotonic()<until and box.read(36)==h:time.sleep(.005)
                            if box.read(36)!=h:
                                box.command(0)
                    finally:box.close()
        finally:os.close(fd)


def choose_url():
    print('Enter a YouTube video URL or 11-character video ID.')
    print('Press Enter for the usual 15-minute talking clip, or Q to cancel.')
    while True:
        try:
            value=input('YouTube URL: ').strip()
        except EOFError:
            return None
        if value.lower() == 'q':return None
        if not value:value='https://www.youtube.com/watch?v=M7kB0lis3xg'
        try:
            return normalise_url(value)[0]
        except ValueError as exc:
            print(str(exc),flush=True)


def launch():
    global REPORT
    REPORT=ROOT/('YouTube-url-13-7-'+secrets.token_hex(4)+'.txt')
    print('=== YOUTUBE URL v0.13.7 ===',flush=True)
    print('Script: '+str(Path(__file__).resolve()),flush=True)
    print('Report: '+str(REPORT),flush=True)
    ROOT.mkdir(parents=True,exist_ok=True)
    with LOCK.open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('A RAM test or stream is already running. Its report has been left untouched.')
            return
        url=choose_url()
        if url is None:return
        print('Selected: '+url,flush=True)
        STOP.unlink(missing_ok=True)
        with REPORT.open('wb') as report:
            subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',url,'--lock-fd',str(lock.fileno()),'--report-name',REPORT.name],
                             pass_fds=(lock.fileno(),),stdin=subprocess.DEVNULL,stdout=report,stderr=report,
                             start_new_session=True)
    print('Started. Return to the menu and load v0.13-smooth within two minutes.')
    print('RAM streaming On for 5 seconds, Off for 2 seconds, then On again.')
    print('Keep Display set to Loaded frame. YouTube resolution/buffering follows.')
    print('To stop early: return to the main menu, then run Stop_YouTube_URL if needed.')
    print('Report: '+str(REPORT))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--report-name');parser.add_argument('--worker');parser.add_argument('--lock-fd',type=int);parser.add_argument('--stop',action='store_true')
    args=parser.parse_args()
    try:
        if args.stop:
            STOP.touch();print('Stop requested. Return to the core or wait for the worker to exit.')
        elif args.worker:
            if not args.report_name or not re.fullmatch(r'YouTube-url-13-7-[0-9a-f]{8}\.txt',args.report_name):
                raise ValueError('Invalid report filename.')
            REPORT=ROOT/args.report_name
            worker(args.worker,args.lock_fd)
        else:launch()
    except (Exception,KeyboardInterrupt) as exc:
        print('FAIL: '+safe_text(str(exc)),flush=True)
        sys.exit(1)
