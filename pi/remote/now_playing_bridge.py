"""Bounded, same-host bridge from the Wi-Fi now-playing publisher to Tailscale UI."""

import hashlib
import json
import re
import time
from urllib.request import urlopen

PUBLISHER = 'http://octet.local:8768'
ARTWORK = re.compile(r'/api/artwork/([a-f0-9]{64})\Z')
TYPES = {'image/jpeg': b'\xff\xd8\xff', 'image/png': b'\x89PNG\r\n\x1a\n', 'image/webp': b'RIFF'}


def fetch_song():
    try:
        with urlopen(PUBLISHER + '/api/now-playing', timeout=0.8) as response:
            raw = response.read(8193)
        if len(raw) > 8192:
            return None
        song = json.loads(raw)
        if not isinstance(song, dict) or song.get('version') != 1 or song.get('state') not in ('playing', 'paused'):
            return None
        stamp = song.get('updated_at')
        if not isinstance(stamp, (int, float)) or abs(time.time() - stamp) > 30:
            return None
        fields = ('source', 'title', 'artist', 'album')
        if any(not isinstance(song.get(field), str) or len(song[field]) > 512 for field in fields):
            return None
        art = song.get('artwork')
        match = ARTWORK.fullmatch(art) if isinstance(art, str) else None
        return {**{field: song[field] for field in fields}, 'state': song['state'],
                'artwork': '/api/now-playing/artwork/' + match.group(1) if match else None}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def fetch_artwork(key):
    if not re.fullmatch(r'[a-f0-9]{64}', key):
        raise ValueError('Invalid artwork key')
    with urlopen(PUBLISHER + '/api/artwork/' + key, timeout=2) as response:
        kind = response.headers.get('Content-Type', '').split(';', 1)[0]
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024 or kind not in TYPES or not body.startswith(TYPES[kind]):
        raise ValueError('Invalid artwork')
    if kind == 'image/webp' and body[8:12] != b'WEBP':
        raise ValueError('Invalid artwork')
    if hashlib.sha256(body).hexdigest() != key:
        raise ValueError('Artwork hash mismatch')
    return body, kind
