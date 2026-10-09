import re
from urllib.parse import urlparse, parse_qs

def normalise_url(value):
    value = value.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        video_id = value
    else:
        if '://' not in value:
            value = 'https://' + value
        parsed = urlparse(value)
        if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
            raise ValueError('Enter a YouTube video link or its 11-character video ID.')
        host = (parsed.hostname or '').lower()
        pieces = parsed.path.strip('/').split('/')
        if host in ('youtu.be', 'www.youtu.be') and len(pieces) == 1:
            video_id = pieces[0]
        elif host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
            if parsed.path == '/watch':
                video_id = parse_qs(parsed.query).get('v', [''])[0]
            elif len(pieces) == 2 and pieces[0] in ('shorts', 'embed', 'live'):
                video_id = pieces[1]
            else:
                video_id = ''
        else:
            video_id = ''
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError('Use a link to one YouTube video, not a channel or playlist.')
    return 'https://www.youtube.com/watch?v=' + video_id, video_id

