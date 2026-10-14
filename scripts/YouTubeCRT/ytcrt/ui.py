"""CRT-friendly screens drawn at 320x240 (title-safe margins, no pure white,
no thin one-pixel details that flicker on interlaced sets)."""
import time

from .feeds import THUMB_H, THUMB_W, age_text
from .gfx import Canvas, fold, rgb

BG = rgb(14, 16, 30)
PANEL = rgb(24, 28, 50)
TEXT = rgb(226, 228, 235)
DIM = rgb(138, 144, 168)
RED = rgb(206, 34, 34)
SPIN = '|/-' + chr(92)          # a turning bar for the loading indicators
SEL = rgb(38, 74, 150)
WHITE = rgb(244, 244, 244)
GREEN = rgb(96, 204, 118)
AMBER = rgb(236, 184, 64)
BLACK = rgb(0, 0, 0)

MX, MY = 16, 12              # title-safe margins
W, H = 320, 240
ROW_H = 18
LIST_Y = 40


def mmss(ms):
    s = max(0, int(ms // 1000))
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


def wrap(text, width, lines):
    words, out, cur = fold(text).split(), [], ''
    for w in words:
        while len(w) > width:
            if cur:
                out.append(cur)
                cur = ''
            out.append(w[:width])
            w = w[width:]
        if len(cur) + len(w) + (1 if cur else 0) <= width:
            cur = (cur + ' ' + w) if cur else w
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1][:width - 2] + '..' if len(out[-1]) > width - 2 else out[-1] + '..'
    return out


def clip(text, width):
    text = fold(text)
    return text if len(text) <= width else text[:width - 2] + '..'


def chrome(c, title, right='', hints=''):
    c.fill(BG)
    # logo: red tab with a play triangle
    c.rect(MX, MY, 26, 18, RED)
    for i in range(5):
        c.rect(MX + 10 + i * 2, MY + 4 + i, 2, 10 - 2 * i, WHITE)
    c.text(MX + 34, MY + 1, title, TEXT, BG, 1, 2, maxchars=22)
    if right:
        c.text(W - MX - len(right) * 8, MY + 5, right, DIM, BG, 1, 1)
    c.rect(MX, MY + 22, W - 2 * MX, 2, RED)
    if hints:
        c.text(MX, 217, clip(hints, 38), DIM, BG, 1, 1)


class Screen:
    title = ''
    hints = ''

    def draw(self, app, c):
        chrome(c, self.title, '', self.hints)

    def handle(self, app, act):
        if act == 'back':
            app.pop()

    def animated(self):
        return False

    def busy(self, app):
        """Redraw continuously while something is loading in the background (spinners)."""
        return False


class Message(Screen):
    def __init__(self, title, lines, color=TEXT, spinner=False, on_back=None):
        self.title, self.lines, self.color, self.spinner, self.on_back = title, lines, color, spinner, on_back
        self.hints = 'B: Back' if on_back is not False else ''

    def animated(self):
        return self.spinner

    def draw(self, app, c):
        chrome(c, self.title, '', self.hints)
        y = 70
        for line in (self.lines() if callable(self.lines) else self.lines):
            for part in wrap(line, 34, 6):
                c.text(MX + 8, y, part, self.color, BG, 1, 2)
                y += 20
            y += 4
        if self.spinner:
            n = int(time.monotonic() * 6) % 8
            for i in range(8):
                c.rect(MX + 8 + i * 14, min(max(190, y + 4), 212), 10, 8, RED if i == n else PANEL)

    def handle(self, app, act):
        if act == 'back' and self.on_back is not False:
            (self.on_back or app.pop)()


class TextView(Screen):
    """Scrollable plain text (diagnostics, long errors). X refreshes when `refresh` is given."""
    rows = 14

    def __init__(self, title, lines, refresh=None):
        self.title, self.lines, self.refresh = title, lines, refresh
        self.top = 0
        self.hints = 'Up/Down:Scroll  X:Refresh  B:Back' if refresh else 'Up/Down:Scroll  B:Back'

    def handle(self, app, act):
        n = len(self.lines)
        if act == 'back':
            app.pop()
        elif act == 'down':
            self.top = min(max(0, n - self.rows), self.top + 1)
        elif act == 'up':
            self.top = max(0, self.top - 1)
        elif act == 'pgdn':
            self.top = min(max(0, n - self.rows), self.top + self.rows)
        elif act == 'pgup':
            self.top = max(0, self.top - self.rows)
        elif act == 'menu' and self.refresh:
            self.lines = self.refresh()
            self.top = max(0, len(self.lines) - self.rows)

    def draw(self, app, c):
        n = len(self.lines)
        chrome(c, self.title, ('%d-%d/%d' % (self.top + 1, min(n, self.top + self.rows), n)) if n else '', self.hints)
        for i, line in enumerate(self.lines[self.top:self.top + self.rows]):
            c.text(MX, 40 + i * 12, clip(line, 38), AMBER if line.startswith('!') else TEXT, BG, 1, 1)


class TextInput(Screen):
    """Type text with an on-screen keyboard (d-pad + A) or a real keyboard.

    A types the highlighted key, X deletes, Y toggles Shift, Start submits, B cancels.
    `on_submit(app, text)` returns an error message to show, or None when it handled the text.
    """
    wants_text = True
    ROWS = ['1234567890', 'qwertyuiop', 'asdfghjkl:', 'zxcvbnm./?', '=&_-%@#~+ ']
    SPECIAL = ('Shift', 'Del', 'Clear', 'OK')
    KW, KH, X0, Y0 = 28, 20, 20, 76
    MAX = 200

    def __init__(self, title, prompt, on_submit, initial='', spaces=False):
        self.title, self.prompt, self.on_submit = title, prompt, on_submit
        self.spaces = spaces                          # the space key types a space (searches); links and handles have none
        self.text, self.error = initial, ''
        self.row, self.col = 1, 0
        self.shift = False
        self.hints = 'A:Key X:Del Y:Shift Start:OK B:Cancel'

    def animated(self):
        return True

    def _width(self, row):
        return len(self.SPECIAL) if row == len(self.ROWS) else len(self.ROWS[row])

    def _char(self, ch):
        return ch.upper() if self.shift else ch

    def press(self, app):
        if self.row == len(self.ROWS):
            name = self.SPECIAL[self.col]
            if name == 'Shift':
                self.shift = not self.shift
            elif name == 'Del':
                self.text = self.text[:-1]
            elif name == 'Clear':
                self.text = ''
            else:
                self.submit(app)
        else:
            ch = self.ROWS[self.row][self.col]
            if (ch != ' ' or self.spaces) and len(self.text) < self.MAX:
                self.text += self._char(ch)
        self.error = ''

    def submit(self, app):
        self.error = self.on_submit(app, self.text.strip()) or ''

    def handle(self, app, act):
        if act.startswith('ch:'):
            if len(self.text) < self.MAX:
                self.text += act[3:]
            self.error = ''
        elif act == 'bksp' or act == 'menu':
            self.text, self.error = self.text[:-1], ''
        elif act == 'submit' or act == 'start':
            self.submit(app)
        elif act == 'back':
            app.pop()
        elif act == 'alt':
            self.shift = not self.shift
        elif act == 'ok':
            self.press(app)
        elif act in ('up', 'down'):
            rows = len(self.ROWS) + 1
            self.row = (self.row + (1 if act == 'down' else -1)) % rows
            self.col = min(self.col, self._width(self.row) - 1)
        elif act in ('left', 'right'):
            self.col = (self.col + (1 if act == 'right' else -1)) % self._width(self.row)

    def draw(self, app, c):
        chrome(c, self.title, 'Shift' if self.shift else '', self.hints)
        c.text(MX, 40, clip(self.prompt, 38), DIM, BG)
        c.rect(MX - 2, 52, W - 2 * MX + 4, 20, PANEL)
        c.frame(MX - 2, 52, W - 2 * MX + 4, 20, RED if self.text else DIM, 1)
        shown = self.text[-33:]
        cursor = '_' if int(time.monotonic() * 2) % 2 == 0 else ' '
        c.text(MX + 2, 54, shown + cursor, WHITE, PANEL, 1, 2, maxchars=34)
        for r, row in enumerate(self.ROWS):
            for i, ch in enumerate(row):
                x, y = self.X0 + i * self.KW, self.Y0 + r * self.KH
                sel = (r, i) == (self.row, self.col)
                bg = SEL if sel else PANEL
                c.rect(x, y, self.KW - 2, self.KH - 2, bg)
                if ch != ' ':
                    c.text(x + 10, y + 5, self._char(ch), WHITE if sel else TEXT, bg)
        y = self.Y0 + len(self.ROWS) * self.KH + 2
        wk = (W - 2 * MX) // len(self.SPECIAL)
        for i, name in enumerate(self.SPECIAL):
            sel = (self.row, self.col) == (len(self.ROWS), i)
            on = name == 'Shift' and self.shift
            bg = SEL if sel else (RED if on else PANEL)
            c.rect(MX + i * wk, y, wk - 2, self.KH - 2, bg)
            c.text(MX + i * wk + (wk - 2 - len(name) * 8) // 2, y + 5, name, WHITE if (sel or on) else TEXT, bg)
        if self.error:
            c.text(MX, 204, clip(self.error, 38), AMBER, BG)


class ListScreen(Screen):
    visible = 6
    detail = True

    def __init__(self):
        self.sel = 0
        self.top = 0

    # subclass API
    def items(self, app):
        return []

    def label(self, app, item):
        return str(item)

    def right(self, app, item):
        return ''

    def marker(self, app, item):
        return ' ', DIM

    def info(self, app, item):
        return None

    def empty_text(self, app):
        return ['Nothing here yet.']

    def activate(self, app, item):
        pass

    def extra(self, app, item, act):
        return False

    def status(self, app):
        return ''

    def handle(self, app, act):
        items = self.items(app)
        n = len(items)
        if act == 'back':
            app.pop()
        elif act == 'start':
            app.go_home()
        elif n:
            if act == 'down':
                self.sel = (self.sel + 1) % n
            elif act == 'up':
                self.sel = (self.sel - 1) % n
            elif act == 'pgdn':
                self.sel = min(n - 1, self.sel + self.visible)
            elif act == 'pgup':
                self.sel = max(0, self.sel - self.visible)
            elif act == 'ok':
                self.activate(app, items[self.sel])
            else:
                self.extra(app, items[self.sel], act)
        if self.sel < self.top:
            self.top = self.sel
        if self.sel >= self.top + self.visible:
            self.top = self.sel - self.visible + 1

    def draw(self, app, c):
        items = self.items(app)
        n = len(items)
        self.sel = min(self.sel, max(0, n - 1))
        chrome(c, self.title, self.status(app) or ('%d/%d' % (self.sel + 1, n) if n else ''), self.hints)
        if not n:
            y = 80
            for line in self.empty_text(app):
                for part in wrap(line, 35, 4):
                    c.text(MX, y, part, DIM, BG, 1, 1)
                    y += 12
            return
        top = max(0, min(self.top, n - self.visible))
        for r in range(self.visible):
            i = top + r
            if i >= n:
                break
            y = LIST_Y + r * ROW_H
            item = items[i]
            sel = i == self.sel
            bg = SEL if sel else BG
            if sel:
                c.rect(MX - 4, y, W - 2 * MX + 4 - 8, ROW_H - 2, SEL)
            mk, mc = self.marker(app, item)
            right = self.right(app, item)
            c.text(MX, y, mk, mc, bg, 1, 2)
            width = 34 - len(right) - (1 if right else 0)
            c.text(MX + 10, y, clip(self.label(app, item), width), WHITE if sel else TEXT, bg, 1, 2)
            if right:
                c.text(W - MX - 8 - len(right) * 8, y, right, WHITE if sel else DIM, bg, 1, 2)
        if n > self.visible:                       # scroll bar
            h = max(8, self.visible * ROW_H * self.visible // n)
            y = LIST_Y + (self.visible * ROW_H - h) * top // max(1, n - self.visible)
            c.rect(W - MX - 3, LIST_Y, 3, self.visible * ROW_H - 2, PANEL)
            c.rect(W - MX - 3, y, 3, h, DIM)
        if self.detail:
            self.draw_detail(app, c, items[self.sel])

    def draw_detail(self, app, c, item):
        info = self.info(app, item)
        if not info:
            return
        y0 = 154
        c.rect(MX - 4, y0 - 2, W - 2 * MX + 4 - 8, 58, PANEL)
        tx = MX
        if info.get('vid') and app.store.settings.get('thumbnails') == 'On':
            c.rect(MX, y0, THUMB_W + 2, THUMB_H + 2, BLACK)
            data = app.feeds.thumb(info['vid'])
            if data:
                c.blit(MX + 1, y0 + 1, THUMB_W, THUMB_H, data)
            tx = MX + THUMB_W + 8
        cols = (W - MX - 8 - tx) // 8
        y = y0
        for part in wrap(info.get('title', ''), cols, 3):
            c.text(tx, y, part, TEXT, PANEL, 1, 1)
            y += 10
        y = y0 + 34
        for line in info.get('lines', [])[:2]:
            c.text(tx, y, clip(line, cols), DIM, PANEL, 1, 1)
            y += 10
        if info.get('badge'):
            c.text(MX, y0 + THUMB_H + 4 - 1, '', DIM, PANEL)


MORE_ROW = {'_more': True, 'id': '', 'title': ''}


class VideoList(ListScreen):
    """Rows are video dicts. Subclasses supply `videos(app)`."""

    def __init__(self, title, source, hints='A:Play  Y:Later  B:Back', empty=None, status=None, refresh=None,
                 more=None, can_more=None, loading=None, remove=None, autoplay=False):
        super().__init__()
        self.autoplay = autoplay                                              # when a video ends, play the next one in this list
        self.title, self.source, self.hints = title, source, hints
        self._empty, self._status, self.refresh = empty, status, refresh
        self.more, self.can_more, self.loading = more, can_more, loading      # paging: a "Load more" row at the end
        self.remove = remove                                                  # Y removes the highlighted entry (My Videos)

    def items(self, app):
        vids = self.source(app)
        if vids and self.more and self.can_more and self.can_more(app):
            vids = list(vids) + [MORE_ROW]
        return vids

    def status(self, app):
        if self.is_loading(app):
            return 'loading ' + SPIN[int(time.monotonic() * 8) % 4]
        return self._status(app) if self._status else ''

    def is_loading(self, app):
        return bool(self.loading and self.loading(app))

    def busy(self, app):
        return self.is_loading(app)

    def empty_text(self, app):
        return self._empty(app) if self._empty else ['No videos.']

    def label(self, app, v):
        if v.get('_more'):
            if self.is_loading(app):
                return 'Loading more' + '.' * (int(time.monotonic() * 3) % 4)
            return 'Load more videos...'
        return v.get('title', '')

    def right(self, app, v):
        return '' if v.get('approx') or v.get('_more') else age_text(v.get('published') or 0)

    def marker(self, app, v):
        if v.get('_more'):
            if self.is_loading(app):
                return SPIN[int(time.monotonic() * 8) % 4], RED
            return '+', GREEN
        if app.store.in_later(v['id']):
            return '*', AMBER
        if app.store.saved_pos(v['id']) > 0:
            return '~', GREEN
        return ' ', DIM

    def info(self, app, v):
        if v.get('_more'):
            return None
        lines = [v.get('channel', '')]
        bits = []
        if v.get('views'):
            bits.append('%s views' % format(v['views'], ','))
        if v.get('short'):
            bits.append('Short')
        pos = app.store.saved_pos(v['id'])
        if pos:
            bits.append('resume ' + mmss(pos))
        if app.store.in_later(v['id']):
            bits.append('saved')
        if app.store.is_slow(v['id']):
            bits.insert(0, 'slow start')                  # needs YouTube's security check
        lines.append('  '.join(bits))
        return {'vid': v['id'], 'title': v.get('title', ''), 'lines': lines}

    def activate(self, app, v):
        if v.get('_more'):
            if not (self.loading and self.loading(app)):
                self.more(app)
            return
        queue = (lambda: [x for x in self.items(app) if not x.get('_more')]) if self.autoplay else None
        app.start_video(v, queue)

    def extra(self, app, v, act):
        if act == 'menu' and self.refresh:
            self.refresh(app)
            return True
        if v.get('_more'):
            return False
        if act == 'alt' and self.remove:
            self.remove(app, v)
            return True
        if act == 'alt':
            added = app.store.toggle_later(v)
            app.toast('Saved to Watch Later' if added else 'Removed from Watch Later')
            return True
        return False


class ChannelList(ListScreen):
    title = 'Channels'
    hints = 'A:Open X:Refresh Y:Remove B:Back'
    detail = False
    visible = 8

    def items(self, app):
        return app.feeds.channel_list(app.store.subscriptions())

    def label(self, app, ch):
        return ch[1]

    def right(self, app, ch):
        return '!' if ch[3] else str(ch[2])

    def marker(self, app, ch):
        return ('!', AMBER) if ch[3] else (' ', DIM)

    def empty_text(self, app):
        return app.no_subscriptions_text()

    def activate(self, app, ch):
        cid, name = ch[0], ch[1]
        if ch[3]:                                       # a channel that did not load: say why
            app.push(TextView(name, ['!' + l for l in wrap('Could not load: ' + ch[3], 37, 4)] +
                              ['', 'Check the spelling in', app.store.p_subs, 'then press X on Channels.']))
            return
        app.push(VideoList(name, lambda a: a.feeds.channel_videos(cid, a.store.settings['shorts'] == 'On'),
                           empty=lambda a: ['No videos found for this channel yet.'],
                           autoplay=True,
                           more=lambda a: a.load_more('channel', cid),
                           can_more=lambda a: a.feeds.can_load_more('channel', cid),
                           loading=lambda a: a.feeds.is_loading('channel', cid)))

    def extra(self, app, ch, act):
        if act == 'menu':
            app.refresh_feeds(force=True)
            return True
        if act == 'alt':
            app.confirm_remove_channel(ch[0], ch[1])
            return True
        return False


class PlaylistList(ListScreen):
    title = 'Playlists'
    hints = 'A:Open X:Refresh Y:Remove B:Back'
    detail = False
    visible = 8

    def items(self, app):
        return app.feeds.playlist_list(app.store.playlists())

    def label(self, app, row):
        return row[1]

    def right(self, app, row):
        if row[3]:
            return '...' if row[3] == 'not loaded yet' else '!'
        return str(row[2])

    def marker(self, app, row):
        return ('!', AMBER) if row[3] and row[3] != 'not loaded yet' else (' ', DIM)

    def empty_text(self, app):
        return app.no_lists_text('playlist')

    def activate(self, app, row):
        pid, name = row[0], row[1]
        if row[3]:
            if row[3] == 'not loaded yet':
                app.toast('Still loading...')
            else:
                app.push(TextView(name, ['!' + l for l in wrap('Could not load: ' + row[3], 37, 4)] +
                                  ['', 'Check the link in', app.store.p_subs, 'then press X on Playlists.']))
            return
        app.push(VideoList(name, lambda a: a.feeds.playlist_videos(pid, a.store.settings['shorts'] == 'On'),
                           hints='A:Play  Y:Later  B:Back', empty=lambda a: ['This playlist has no videos.'],
                           autoplay=True,
                           more=lambda a: a.load_more('playlist', pid),
                           can_more=lambda a: a.feeds.can_load_more('playlist', pid),
                           loading=lambda a: a.feeds.is_loading('playlist', pid)))

    def extra(self, app, row, act):
        if act == 'menu':
            app.refresh_lists(force=True)
            return True
        if act == 'alt':
            app.confirm_remove('playlist', row[1], [row[0]])
            return True
        return False


class FoundPlaylists(ListScreen):
    """The result of a YouTube playlist search: A opens one, Y saves it to your playlists."""
    hints = 'A:Open  Y:Save  B:Back'
    detail = False
    visible = 7

    def __init__(self, query, results, more=None, can_more=None, loading=None):
        super().__init__()
        self.title = 'Playlists: ' + clip(query, 20)
        self.results = results
        self.more, self.can_more, self.loading = more, can_more, loading     # paging: a "Load more" row at the end

    def items(self, app):
        if self.more and self.can_more and self.can_more(app):
            return list(self.results) + [MORE_ROW]
        return self.results

    def is_loading(self, app):
        return bool(self.loading and self.loading(app))

    def busy(self, app):
        return self.is_loading(app)

    def label(self, app, row):
        if row.get('_more'):
            if self.is_loading(app):
                return 'Loading more' + '.' * (int(time.monotonic() * 3) % 4)
            return 'Load more playlists...'
        return clip(row['title'], 29)

    def marker(self, app, row):
        if row.get('_more'):
            if self.is_loading(app):
                return SPIN[int(time.monotonic() * 8) % 4], RED
            return '+', GREEN
        return ' ', DIM

    def right(self, app, row):
        return '' if row.get('_more') else (str(row['count']) if row.get('count') else '')

    def empty_text(self, app):
        return ['No playlists found.']

    def info(self, app, row):
        return None if row.get('_more') else clip(row.get('channel') or '', 36)

    def activate(self, app, row):
        if row.get('_more'):
            if not self.is_loading(app):
                self.more(app)
            return
        app.open_found_playlist(row)

    def extra(self, app, row, act):
        if row.get('_more'):
            return False
        if act == 'alt':
            app.save_found_playlist(row)
            return True
        return False


class Home(ListScreen):
    title = 'YouTube'
    hints = 'A:Select  Start:Home  F12:MiSTer menu'
    detail = False
    visible = 7

    ENTRIES = ('Search', 'Search Playlists', 'Subscriptions', 'Channels', 'Playlists', 'My Videos', 'Watch Later', 'History', 'Play URL / ID',
               'Add Channel via @handle', 'Add to Lists', 'Settings')

    def items(self, app):
        return self.ENTRIES

    def status(self, app):
        from . import __version__
        return 'v' + __version__          # always visible: which player build is running

    def right(self, app, name):
        if name == 'Watch Later':
            return str(len(app.store.later))
        if name == 'History':
            return str(len(app.store.history))
        if name == 'Channels':
            return str(len(app.feeds.channel_list(app.store.subscriptions())))
        if name == 'Playlists':
            return str(len(app.store.playlists()))
        if name == 'My Videos':
            return str(len(app.store.video_list()))
        return ''

    def activate(self, app, name):
        app.open_home_entry(name)

    def handle(self, app, act):
        if act == 'back':
            return
        super().handle(app, act)

    def draw(self, app, c):
        super().draw(app, c)
        c.text(MX, 200, clip(app.profile_summary() + ('  updating...' if app.feeds_busy() else ''), 38), DIM, BG, 1, 1)


class Choice(Screen):
    """A short modal list of options."""

    def __init__(self, title, prompt, options, on_pick):
        self.title, self.prompt, self.options, self.on_pick = title, prompt, options, on_pick
        self.sel = 0
        self.hints = 'A:Select  B:Cancel'

    def handle(self, app, act):
        if act == 'back':
            app.pop()
        elif act == 'down':
            self.sel = (self.sel + 1) % len(self.options)
        elif act == 'up':
            self.sel = (self.sel - 1) % len(self.options)
        elif act == 'ok':
            app.pop()
            self.on_pick(self.sel)

    def draw(self, app, c):
        chrome(c, self.title, '', self.hints)
        y = 60
        for part in wrap(self.prompt, 34, 3):
            c.text(MX, y, part, TEXT, BG, 1, 2)
            y += 20
        y += 14
        for i, o in enumerate(self.options):
            sel = i == self.sel
            bg = SEL if sel else BG
            if sel:
                c.rect(MX - 4, y, W - 2 * MX + 4 - 8, ROW_H - 2, SEL)
            c.text(MX + 10, y, o, WHITE if sel else TEXT, bg, 1, 2)
            y += ROW_H + 4


class Settings(ListScreen):
    title = 'Settings'
    hints = 'Left/Right:Change  B:Back'
    detail = False
    visible = 8

    def rows(self, app):
        s = app.store.settings
        return [
            ('quality', 'Stream quality', s['quality'], ('Auto', '240p', '360p', '480p')),
            ('crop', 'Video crop', s['crop'], ('Fill', '14:9', 'Off')),
            ('resume', 'Resume playback', s['resume'], ('Ask', 'Always')),
            ('autoplay', 'Play next video', s['autoplay'], ('On', 'Off')),
            ('shorts', 'Show Shorts', s['shorts'], ('On', 'Off')),
            ('thumbnails', 'Thumbnails', s['thumbnails'], ('On', 'Off')),
            ('decode_speed', 'Decoder speed', s['decode_speed'], ('Normal', 'Fast', 'Fastest')),
            ('bench', 'Speed test', 'run', None),
            ('diag', 'Diagnostics', 'view', None),
            ('clear_later', 'Clear Watch Later', 'run', None),
            ('clear_history', 'Clear History', 'run', None),
        ]

    def items(self, app):
        return self.rows(app)

    def label(self, app, row):
        return row[1]

    def right(self, app, row):
        return ('< %s >' % row[2]) if row[3] else row[2]

    def activate(self, app, row):
        if row[3]:
            self.cycle(app, row, 1)
        else:
            app.settings_action(row[0])

    def cycle(self, app, row, d):
        opts = row[3]
        i = opts.index(row[2]) if row[2] in opts else 0
        app.store.set(row[0], opts[(i + d) % len(opts)])

    def extra(self, app, row, act):
        if act in ('left', 'right') and row[3]:
            self.cycle(app, row, -1 if act == 'left' else 1)
            return True
        return False

    def handle(self, app, act):
        super().handle(app, act)
