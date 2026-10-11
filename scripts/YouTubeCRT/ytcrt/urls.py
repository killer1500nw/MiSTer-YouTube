"""Turn what a person types into a YouTube video id. Channels and playlists are refused with a clear message."""
import re
from urllib.parse import parse_qs, urlparse

VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')


def normalise_url(value):
    """-> (canonical watch URL, 11-character video id). Raises ValueError with a message for the screen."""
    value = value.strip()
    if not value:
        raise ValueError('Type a YouTube link or video ID.')
    if VIDEO_ID.fullmatch(value):
        video_id = value
    else:
        if '://' not in value:
            value = 'https://' + value
        parsed = urlparse(value)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('Enter a YouTube video link or its 11-character video ID.')
        host = (parsed.hostname or '').lower()
        pieces = parsed.path.strip('/').split('/')
        video_id = ''
        if host in ('youtu.be', 'www.youtu.be') and len(pieces) == 1:
            video_id = pieces[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if parsed.path == '/watch':
                video_id = parse_qs(parsed.query).get('v', [''])[0]
            elif len(pieces) == 2 and pieces[0] in ('shorts', 'embed', 'live'):
                video_id = pieces[1]
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError('That is not a single video link (channels and playlists are not supported here).')
    return 'https://www.youtube.com/watch?v=' + video_id, video_id
