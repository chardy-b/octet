#!/usr/bin/env python3
"""Read-only Octet song publisher. systemd binds its socket to wlan0."""
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote
from urllib.request import urlopen, build_opener, HTTPRedirectHandler


ART_ROOT = Path('/run/octet-shairport/.cache/coverart')
MAX_ART_BYTES = 2 * 1024 * 1024


def image_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('Unsupported artwork')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Artwork redirects are not allowed')


def read_artwork(source, uri):
    parsed = urlparse(uri)
    if source == 'airplay':
        original = Path(unquote(parsed.path))
        if (parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost')
                or original.parent != Path('/tmp/shairport-sync/.cache/coverart')):
            raise ValueError('Not an AirPlay cover')
        path = (ART_ROOT / original.name).resolve()
        if path.parent != ART_ROOT.resolve() or not path.name.startswith('cover-'):
            raise ValueError('Invalid AirPlay cover path')
        with path.open('rb') as stream:
            data = stream.read(MAX_ART_BYTES + 1)
    elif source == 'tidal':
        if (parsed.scheme != 'https' or parsed.netloc != 'resources.tidal.com'
                or not parsed.path.startswith('/images/') or parsed.query or parsed.fragment):
            raise ValueError('Not a Tidal cover')
        with build_opener(NoRedirect()).open(uri, timeout=3) as response:
            data = response.read(MAX_ART_BYTES + 1)
    else:
        raise ValueError('Unsupported artwork source')
    if not data or len(data) > MAX_ART_BYTES:
        raise ValueError('Artwork too large or empty')
    return data, image_type(data)


class Artwork:
    """Keep only the current cover in bounded memory; URLs expose content hashes."""
    def __init__(self):
        self.lock = threading.Lock()
        self.locator = None
        self.value = None
        self.retry_at = 0

    def update(self, source, uri):
        with self.lock:
            locator = (source, uri) if uri else None
            if locator != self.locator:
                self.locator, self.value, self.retry_at = locator, None, 0
            if locator and not self.value and time.monotonic() >= self.retry_at:
                try:
                    data, kind = read_artwork(source, uri)
                    key = hashlib.sha256(data).hexdigest()
                    self.value = (key, data, kind)
                except (OSError, ValueError):
                    self.retry_at = time.monotonic() + 15
            return '/api/artwork/' + self.value[0] if self.value else None

    def get(self, key):
        with self.lock:
            return self.value[1:] if self.value and self.value[0] == key else None


ARTWORK = Artwork()


def wifi_addresses():
    result = subprocess.run(['ip', '-j', '-4', 'address', 'show', 'dev', 'wlan0', 'up'],
                            capture_output=True, text=True, check=True, timeout=2)
    return [ipaddress.ip_interface(f"{entry['local']}/{entry['prefixlen']}")
            for interface in json.loads(result.stdout) for entry in interface.get('addr_info', [])
            if entry.get('family') == 'inet' and entry.get('scope') == 'global'
            and ipaddress.ip_address(entry['local']).is_private]


def allowed_peer(peer, destination, interfaces):
    try:
        address = ipaddress.ip_address(peer)
        return any(str(interface.ip) == destination and address in interface.network
                   for interface in interfaces)
    except ValueError:
        return False


def mpris_property(name):
    result = subprocess.run([
        'busctl', '--system', '--json=short', '--timeout=2', 'get-property',
        'org.mpris.MediaPlayer2.ShairportSync', '/org/mpris/MediaPlayer2',
        'org.mpris.MediaPlayer2.Player', name,
    ], capture_output=True, text=True, check=True, timeout=3)
    return json.loads(result.stdout)['data']


def text(value):
    return value[:512] if isinstance(value, str) else ''


def number(value):
    try:
        value = float(value)
        return max(0, value) if 0 <= value < 1e9 else 0
    except (ValueError, TypeError):
        return 0


def describe(status, read_mpris=mpris_property, artwork=None):
    source = status.get('source', 'unknown')
    result = dict(version=1, source=source, source_label=text(status.get('source_label')),
                  state='stopped', title='', artist='', album='', elapsed=0, duration=0,
                  updated_at=time.time(), artwork=None)
    art_uri = ''
    if source == 'airplay':
        if not status.get('airplay', {}).get('active'):
            if artwork:
                artwork.update(source, '')
            return result
        result['state'] = 'playing'
        try:
            metadata = read_mpris('Metadata')
            state = read_mpris('PlaybackStatus')
            result['state'] = {'Playing': 'playing', 'Paused': 'paused', 'Stopped': 'stopped'}.get(state, 'stopped')
            if result['state'] != 'stopped':
                result.update(title=text(metadata.get('xesam:title', {}).get('data')),
                              album=text(metadata.get('xesam:album', {}).get('data')),
                              artist=text(', '.join(metadata.get('xesam:artist', {}).get('data', []))),
                              duration=number(metadata.get('mpris:length', {}).get('data')) / 1e6)
                art_uri = text(metadata.get('mpris:artUrl', {}).get('data'))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            result['metadata_available'] = False
    elif source == 'tidal':
        if status.get('player_error'):
            raise RuntimeError('Music player unavailable')
        result['state'] = {'play': 'playing', 'pause': 'paused'}.get(status.get('playback'), 'stopped')
        current = status.get('current') or {}
        if result['state'] != 'stopped':
            result.update(title=text(current.get('title')), artist=text(current.get('artist')),
                          album=text(current.get('album')), elapsed=number(status.get('elapsed')),
                          duration=number(status.get('duration')))
            art_uri = text(current.get('cover'))
    elif source not in ('unknown', '') and status.get('dsp_state') == 'Running':
        result['state'] = 'playing'
    if status.get('dsp_state') == 'Offline' or source == 'unknown':
        raise RuntimeError('Audio source unavailable')
    if artwork:
        result['artwork'] = artwork.update(source, art_uri)
    return result


class Snapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.expires = 0
        self.value = None

    def read(self):
        with self.lock:
            if time.monotonic() >= self.expires:
                # Credentials, queues and upstream URLs stay on Octet.
                with urlopen('http://127.0.0.1:8767/api/status', timeout=4) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('Status too large')
                self.value = describe(json.loads(raw), artwork=ARTWORK)
                self.expires = time.monotonic() + 2
            return self.value


SNAPSHOT = Snapshot()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, value, code=200, kind='application/json', origin=None):
        body = json.dumps(value, allow_nan=False).encode() if kind == 'application/json' else value
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Vary', 'Origin')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            interfaces = wifi_addresses()
            destination = self.connection.getsockname()[0]
            hosts = {'octet.local:8768'} | {f'{item.ip}:8768' for item in interfaces}
            origin = self.headers.get('Origin')
            origins = {'http://octet.local:8767'} | {f'http://{item.ip}:8767' for item in interfaces}
            if (not allowed_peer(self.client_address[0], destination, interfaces)
                    or self.headers.get('Host', '').lower() not in hosts
                    or (origin and origin not in origins)):
                return self.reply({'error': 'Wi-Fi access only'}, 403)
            if self.path == '/api/now-playing':
                return self.reply(SNAPSHOT.read(), origin=origin)
            if self.path.startswith('/api/artwork/'):
                # Refresh the source so an old cover cannot be returned after a stop.
                SNAPSHOT.read()
                image = ARTWORK.get(self.path.removeprefix('/api/artwork/'))
                if image:
                    data, kind = image
                    return self.reply(data, kind=kind, origin=origin)
                return self.reply({'error': 'Artwork unavailable'}, 404)
            if self.path == '/':
                return self.reply(Path(__file__).with_name('now-playing.html').read_bytes(), kind='text/html; charset=utf-8')
            if self.path == '/now-playing.js':
                return self.reply(Path(__file__).with_name('now-playing.js').read_bytes(), kind='text/javascript; charset=utf-8')
            return self.reply({'error': 'Not found'}, 404)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return self.reply({'error': 'Now playing unavailable'}, 503)


def main():
    if int(os.environ.get('LISTEN_PID', '0')) != os.getpid() or os.environ.get('LISTEN_FDS') != '1':
        raise SystemExit('Start octet-now-playing.socket; a Wi-Fi-bound systemd socket is required.')
    listener = socket.socket(fileno=3)
    http = ThreadingHTTPServer(('0.0.0.0', 8768), Handler, bind_and_activate=False)
    http.socket.close()
    http.socket = listener
    http.server_address = listener.getsockname()
    http.serve_forever()


if __name__ == '__main__':
    main()
