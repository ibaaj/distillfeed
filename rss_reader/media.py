"""Pure URL classification; no network enrichment is performed."""
from __future__ import annotations
import re
from urllib.parse import urlsplit, parse_qs

VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}

def youtube_metadata(url: str) -> dict:
    try:
        parsed = urlsplit(str(url or ''))
        if parsed.scheme not in {'http','https'} or parsed.username or parsed.password:
            return {}
        host = (parsed.hostname or '').lower()
        parts = parsed.path.strip('/').split('/')
        short = host in YOUTUBE_HOSTS and len(parts) == 2 and parts[0] == 'shorts'
        if host in {'youtu.be','www.youtu.be'} and len(parts)==1:
            video = parts[0]
        elif host in YOUTUBE_HOSTS and parsed.path == '/watch':
            video = parse_qs(parsed.query).get('v',[''])[0]
        elif host in YOUTUBE_HOSTS and len(parts)==2 and parts[0] in {'shorts','embed','live'}:
            video = parts[1]
        else:
            return {}
        if not VIDEO_ID.fullmatch(video):
            return {}
        return {'youtube_id':video,'is_short':short}
    except (TypeError, ValueError):
        return {}
