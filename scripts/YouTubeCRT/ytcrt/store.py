"""Local state: settings, watch later, history/resume, subscriptions list.

Plain JSON/text files under one data directory, written atomically. No
account or login is involved; subscriptions are a text file of channel IDs.
"""
import json
import os
import re
import time

from . import profiles, urls

MAX_HISTORY = 100
RESUME_MIN_MS = 10000      # shorter than this: start from the beginning
RESUME_END_MS = 15000      # within this of the end: treat as finished

SETTINGS_DEFAULTS = {
    'resume': 'Ask',           # Ask | Always
    'decode_speed': 'Normal',  # Normal | Fast | Fastest
    'shorts': 'On',
    'thumbnails': 'On',
    'quality': 'Auto',         # Auto | 240p | 360p | 480p: Auto picks the best stream the ARM can decode in time
    'cal': dict(profiles.CAL_DEFAULT),   # measured by the Speed test: cost of one reference stream on this MiSTer
    'cost_mult': 1.0,          # learned: grows when a stream starved, shrinks slowly while streams play cleanly
    'speed_big': 'Fast',       # decoder setting used for larger streams (learned by the Speed test)
    'crop': 'Off',             # Off | 14:9 | Fill : starting crop for each video (More > Crop changes it live)
}

CHANNEL_ID = re.compile(r'UC[A-Za-z0-9_-]{22}')
PLAYLIST_ID = re.compile(r'(?:PL|UU|LL|FL|OL|RD)[A-Za-z0-9_-]{10,40}')


def _read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Store:
    def __init__(self, root):
        self.root = root
        self.p_settings = os.path.join(root, 'settings.json')
        self.p_history = os.path.join(root, 'history.json')
        self.p_later = os.path.join(root, 'watch_later.json')
        self.p_subs = os.path.join(root, 'subscriptions.txt')
        self.settings = dict(SETTINGS_DEFAULTS)
        self.settings.update({k: v for k, v in _read_json(self.p_settings, {}).items()
                              if k in SETTINGS_DEFAULTS})
        self.history = _read_json(self.p_history, {})
        self.later = _read_json(self.p_later, [])
        self._last_pos_save = 0.0

    # settings ---------------------------------------------------------
    def set(self, key, value):
        self.settings[key] = value
        _write_json(self.p_settings, self.settings)

    # subscriptions ----------------------------------------------------
    HEADER = (
        '# One entry per line. Channels, playlists and single videos can be mixed freely:\n'
        '#   a channel ID (UC...), an @handle or a youtube.com/channel/... link\n'
        '#   a playlist link (youtube.com/playlist?list=...) or playlist ID (PL...)\n'
        '#   a video link (watch?v=, youtu.be/, shorts/) or an 11-character video ID\n'
        '# Optional display name first:  Jawed Karim | UC4QobU6STFB0P71PMvOGN5A\n'
        '# (jNQXAC9IVRw is "Me at the zoo", the first video ever uploaded to YouTube)\n')

    def ensure_subscriptions_file(self):
        if not os.path.exists(self.p_subs):
            os.makedirs(self.root, exist_ok=True)
            with open(self.p_subs, 'w', encoding='utf-8') as f:
                f.write(self.HEADER)

    @staticmethod
    def classify(text):
        """What did someone type? -> ('playlist' | 'channel' | 'video', canonical ref) or (None, message)."""
        text = text.strip()
        if not text:
            return None, 'Type or paste something first.'
        m = re.search(r'[?&]list=((?:PL|UU|LL|FL|OL)[A-Za-z0-9_-]{10,40})', text)
        if m:                                             # also a watch?v=...&list=... link: the list is what's wanted here
            return 'playlist', m.group(1)
        if PLAYLIST_ID.fullmatch(text):
            return 'playlist', text
        c = CHANNEL_ID.search(text)
        if c:
            return 'channel', c.group(0)
        h = re.search(r'@([A-Za-z0-9._-]{3,30})', text)
        if h:
            return 'channel', '@' + h.group(1)
        try:
            return 'video', urls.normalise_url(text)[1]
        except ValueError:
            return None, 'Not a channel, playlist or video link or ID.'

    def entries(self):
        """Every usable line of subscriptions.txt -> [(kind, name or None, ref)], in file order, no duplicates."""
        out, seen = [], set()
        try:
            with open(self.p_subs, 'r', encoding='utf-8-sig') as f:
                lines = f.read().splitlines()
        except OSError:
            return out
        for line in lines:
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            name = None
            if '|' in line:
                name, line = [x.strip() for x in line.split('|', 1)]
            kind, ref = self.classify(line)
            if kind and (kind, ref) not in seen:
                seen.add((kind, ref))
                out.append((kind, name or None, ref))
        return out

    def subscriptions(self):
        """Channels only -> [(name_or_None, channel_id_or_handle)]."""
        return [(n, r) for k, n, r in self.entries() if k == 'channel']

    def playlists(self):
        return [(n, r) for k, n, r in self.entries() if k == 'playlist']

    def video_list(self):
        return [(n, r) for k, n, r in self.entries() if k == 'video']

    def add_entry(self, kind, ref):
        """Append to subscriptions.txt. -> False if that exact entry was already there."""
        if any(k == kind and r == ref for k, _, r in self.entries()):
            return False
        self.ensure_subscriptions_file()
        with open(self.p_subs, 'rb') as f:               # a hand-edited file may not end with a newline
            data = f.read()
        lead = '' if not data or data.endswith(b'\n') else '\n'
        with open(self.p_subs, 'a', encoding='utf-8') as f:
            f.write(lead + ref + '\n')
            f.flush()
            os.fsync(f.fileno())
        return True

    @staticmethod
    def parse_channel_ref(text):
        """A channel ID (UC...), an @handle, or a channel/handle link -> 'UC...' or '@handle'; else None."""
        kind, ref = Store.classify(text)
        return ref if kind == 'channel' else None

    def add_subscription(self, ref):
        return self.add_entry('channel', ref)

    # watch later ------------------------------------------------------
    def in_later(self, vid):
        return any(v['id'] == vid for v in self.later)

    def toggle_later(self, video):
        if self.in_later(video['id']):
            self.later = [v for v in self.later if v['id'] != video['id']]
            added = False
        else:
            self.later.insert(0, {k: video.get(k) for k in ('id', 'title', 'channel', 'published')})
            added = True
        _write_json(self.p_later, self.later)
        return added

    def clear_later(self):
        self.later = []
        _write_json(self.p_later, self.later)

    # history ----------------------------------------------------------
    def saved_pos(self, vid):
        e = self.history.get(vid)
        return int(e.get('pos', 0)) if e else 0

    def record_play(self, video, pos_ms, dur_ms=0, finished=False, force=True):
        """Remember a video and where playback stopped. `finished` resets to 0."""
        now = time.time()
        if not force and now - self._last_pos_save < 10:
            return
        self._last_pos_save = now
        if dur_ms and dur_ms - pos_ms < RESUME_END_MS:
            finished = True
        self.history[video['id']] = {
            'id': video['id'], 'title': video.get('title') or '', 'channel': video.get('channel') or '',
            'published': video.get('published'), 'pos': 0 if finished else int(pos_ms),
            'dur': int(dur_ms), 'ts': int(now)}
        if len(self.history) > MAX_HISTORY:
            keep = sorted(self.history.values(), key=lambda e: e.get('ts', 0), reverse=True)[:MAX_HISTORY]
            self.history = {e['id']: e for e in keep}
        _write_json(self.p_history, self.history)

    def history_list(self):
        return sorted(self.history.values(), key=lambda e: e.get('ts', 0), reverse=True)

    def clear_history(self):
        self.history = {}
        _write_json(self.p_history, self.history)
