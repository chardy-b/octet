#!/usr/bin/env python3
"""Tailscale-only music controls for Octet. No browser-side Tidal credentials."""
import base64
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.client import IncompleteRead
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from visualizer_state import VisualizerState
from audio_sources import discover_sources, source_config, source_metadata, meter_channels
from tuning_proxy import TuningProxy
from input_levels import InputLevels
from now_playing_bridge import fetch_song, fetch_artwork
from airplay_control import AirPlayController, serve as serve_airplay
from bluetooth_auto import start_auto_bluetooth
from bluetooth_control import BluetoothControl, serve as serve_bluetooth
from network_access import NetworkAccess

BIND = os.environ.get('OCTET_BIND', '100.100.177.81')
PORT = int(os.environ.get('OCTET_PORT', '8767'))
NETWORK = NetworkAccess()
ROOT = Path(__file__).parent
STATE = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/octet-remote'))
VISUALIZER = VisualizerState(STATE / 'visualizer.json')
PROFILE = '/opt/camilladsp/configs/exciter-audition.yml'
PROFILES = {
    PROFILE: ('Exciter audition', 'Outputs 1–4 · 500 Hz–5 kHz · outputs 5–8 silent'),
}
MIN_DB, MAX_DB = -60.0, -18.0
HIGH_DB = 0.0
LEVELS_UNLOCKED = False
TUNING = TuningProxy()
CHANNEL_PROFILE = Path('/var/lib/octet-dsp/channel-dsp.yml')
PROFILES[str(CHANNEL_PROFILE)] = ('Output DSP controls', 'EX30 → 1/2 · EX32 → 3/4 · subs → 5/6 · tweeters → 7/8')
LOCK = threading.RLock()
TIDAL_LOCK = threading.RLock()
SESSION = None
CATALOG = {}
PLAYLIST_CACHE = {}
PLAYLIST_CACHE_SECONDS = 60
MAX_PLAYLIST_TRACKS = 10000
ACTIVE_CAPTURE_CHANNELS = (0, 1)
STREAM_PREBUFFER_BYTES = 1024 * 1024
logging.getLogger('tidalapi').setLevel(logging.ERROR)
try:
    CATALOG.update(json.loads((STATE / 'catalog.json').read_text()))
except (OSError, ValueError):
    pass


def quote(value):
    value = str(value)
    if '\n' in value or '\r' in value or '\x00' in value:
        raise ValueError('Invalid MPD argument')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def mpd(*commands):
    results = []
    with socket.create_connection(('127.0.0.1', 6600), timeout=5) as sock:
        stream = sock.makefile('rwb')
        if not stream.readline().startswith(b'OK MPD '):
            raise RuntimeError('MPD did not respond')
        for command in commands:
            stream.write((command + '\n').encode())
            stream.flush()
            pairs = []
            while True:
                line = stream.readline().decode().rstrip('\r\n')
                if line == 'OK':
                    break
                if not line or line.startswith('ACK '):
                    raise RuntimeError('Music player could not complete that action')
                if ': ' in line:
                    pairs.append(line.split(': ', 1))
            results.append(pairs)
    return results


def read_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise RuntimeError('DSP disconnected')
        result.extend(chunk)
    return bytes(result)


DSP_SOCKET = '/run/octet-dsp/control.sock'


class DSP:
    """Small synchronous RFC6455 client for the DSP proxy's Unix socket (no direct TCP fallback)."""
    def __enter__(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(4)
            self.sock.connect(DSP_SOCKET)
            self.handshake()
        except Exception:
            self.sock.close()
            raise
        return self

    def handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        request = ('GET / HTTP/1.1\r\nHost: 127.0.0.1:1234\r\nUpgrade: websocket\r\n'
                   'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(request.encode())
        response = bytearray()
        while not response.endswith(b'\r\n\r\n') and len(response) < 8192:
            response.extend(read_exact(self.sock, 1))
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if not response.endswith(b'\r\n\r\n') or not self.upgrade_accepted(bytes(response), expected):
            raise RuntimeError('DSP connection failed')

    @staticmethod
    def upgrade_accepted(response, expected):
        lines = response.decode('latin-1').split('\r\n')
        if not lines[0].startswith('HTTP/1.1 101 '):
            return False
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(':')
            if not separator:
                return False
            headers.setdefault(name.strip().lower(), []).append(value.strip())
        accept, upgrade, connection = (headers.get(name, []) for name in ('sec-websocket-accept', 'upgrade', 'connection'))
        return (accept == [expected] and len(upgrade) == 1 and upgrade[0].lower() == 'websocket'
                and any(token.strip().lower() == 'upgrade' for value in connection for token in value.split(',')))

    def __exit__(self, *args):
        self.sock.close()

    def send_frame(self, data, opcode=1):
        mask = os.urandom(4)
        length = len(data)
        header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else bytes([0x80 | opcode, 0xfe]) + struct.pack('!H', length)
        self.sock.sendall(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))

    def call(self, name, value=None):
        self.send_frame(json.dumps(name if value is None else {name: value}).encode())
        while True:
            first, second = read_exact(self.sock, 2)
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', read_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack('!Q', read_exact(self.sock, 8))[0]
            if length > 2_000_000:
                raise RuntimeError('DSP response too large')
            mask = read_exact(self.sock, 4) if second & 128 else None
            data = read_exact(self.sock, length)
            if mask:
                data = bytes(v ^ mask[i % 4] for i, v in enumerate(data))
            opcode = first & 15
            if opcode == 9:
                self.send_frame(data, 10)
                continue
            if opcode != 1 or not first & 128:
                raise RuntimeError('Unexpected DSP response')
            result = json.loads(data).get(name, {})
            if result.get('result') != 'Ok':
                raise RuntimeError('DSP could not complete that action')
            return result.get('value')


def meter_audio_owned():
    # The local ownership record avoids polling the Auto Tune HTTP service at meter rate.
    try:
        return bool(json.loads(Path('/var/lib/octet-autotune/current.json').read_text()).get('audio_owned'))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


INPUT_LEVELS = InputLevels(LOCK, DSP, discover_sources, meter_audio_owned)


def decode_credentials(data):
    return {key: value.get('data') if isinstance(value, dict) else value for key, value in data.items()}


def tidal():
    global SESSION
    with TIDAL_LOCK:
        if SESSION is not None:
            return SESSION
        import tidalapi
        own = STATE / 'tidal-session.json'
        source = own if own.exists() else Path('/var/cache/upmpdcli/tidal/oauth2.credentials.json')
        credentials = decode_credentials(json.loads(source.read_text()))
        session = tidalapi.Session(tidalapi.Config(quality=tidalapi.Quality.high_lossless, item_limit=50))
        valid = session.load_oauth_session(credentials['token_type'], credentials['access_token'],
                                          credentials.get('refresh_token'), is_pkce=credentials.get('is_pkce', False))
        if not valid:
            raise RuntimeError('Tidal needs a new login on Octet')
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        saved = dict(token_type=session.token_type, access_token=session.access_token,
                     refresh_token=session.refresh_token, is_pkce=session.is_pkce)
        temp = own.with_suffix('.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(saved, handle)
        temp.replace(own)
        SESSION = session
        return session


def remember(track):
    image = getattr(track.album, 'cover', None)
    item = dict(id=str(track.id), title=track.name,
                artist=', '.join(a.name for a in track.artists), album=track.album.name,
                duration=track.duration,
                cover='https://resources.tidal.com/images/' + image.replace('-', '/') + '/320x320.jpg' if image else '')
    CATALOG[item['id']] = item
    return item


def track_id(value):
    value = str(value)
    if not re.fullmatch(r'[0-9]{1,12}', value):
        raise ValueError('Invalid track')
    return value


def playlist_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value):
        raise ValueError('Invalid playlist')
    return str(uuid.UUID(value))


def playlist_summary(playlist):
    try:
        cover = playlist.image(320)
        if not cover.startswith('https://resources.tidal.com/'):
            cover = ''
    except (AttributeError, ValueError):
        cover = ''
    return dict(id=playlist_id(str(playlist.id)), title=playlist.name,
                tracks=int(playlist.num_tracks or 0),
                creator=getattr(getattr(playlist, 'creator', None), 'name', '') or 'TIDAL', cover=cover)


def playlist_page(offset=0):
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
        raise ValueError('Invalid playlist offset')
    with TIDAL_LOCK:
        page = tidal().user.playlist_and_favorite_playlists(offset=offset, limit=50)
        return dict(playlists=[playlist_summary(p) for p in page],
                    next_offset=offset + len(page), has_more=len(page) == 50)


def playlist_contents(identifier, refresh=False):
    identifier = playlist_id(identifier)
    with TIDAL_LOCK:
        cached = PLAYLIST_CACHE.get(identifier)
        if not refresh and cached and time.monotonic() - cached[0] < PLAYLIST_CACHE_SECONDS:
            return cached[1]
        playlist = tidal().playlist(identifier)
        summary = playlist_summary(playlist)
    if summary['tracks'] > MAX_PLAYLIST_TRACKS:
        raise ValueError('This playlist exceeds the 10,000-track queue limit')
    rows, offset, unavailable = [], 0, 0
    while True:
        with TIDAL_LOCK:
            page = playlist.tracks(limit=100, offset=offset)
            if not page:
                break
            for track in page:
                if getattr(track, 'available', True) is False:
                    unavailable += 1
                else:
                    rows.append(remember(track))
        offset += len(page)
        if offset > MAX_PLAYLIST_TRACKS:
            raise ValueError('This playlist exceeds the 10,000-track queue limit')
        if summary['tracks'] and offset >= summary['tracks']:
            break
    result = dict(playlist=summary, tracks=rows, unavailable=unavailable)
    with TIDAL_LOCK:
        PLAYLIST_CACHE[identifier] = (time.monotonic(), result)
    return result


def play_playlist(data):
    identifier = playlist_id(data.get('id'))
    position = data.get('position', 0)
    if isinstance(position, bool) or not isinstance(position, int) or position < 0:
        raise ValueError('Invalid playlist position')
    contents = playlist_contents(identifier)
    rows = contents['tracks']
    if not rows:
        raise RuntimeError('This playlist has no available tracks')
    if position >= len(rows):
        raise ValueError('Invalid playlist position')
    if data.get('track_id') is not None and track_id(data['track_id']) != rows[position]['id']:
        raise RuntimeError('The playlist changed. Reopen it before selecting a track.')
    commands = ['addid ' + quote(f'http://{NETWORK.stream_host(BIND)}:{PORT}/stream/{track_id(row["id"])}') for row in rows]
    with LOCK, DSP() as dsp:
        approved(dsp)
        require_tidal(dsp)
        STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TIDAL_LOCK:
            (STATE / 'catalog.json').write_text(json.dumps(CATALOG))
        old = mpd('playlistinfo')[0]
        old_ids = [str(int(value)) for key, value in old if key == 'Id']
        # Append first: a Tidal or queue-loading error must not erase current music.
        try:
            added = mpd(*commands)
            ids = [str(int(dict(result)['Id'])) for result in added]
        except Exception:
            try:
                current = mpd('playlistinfo')[0]
                staged = [str(int(value)) for key, value in current if key == 'Id' and str(int(value)) not in old_ids]
                if staged:
                    mpd(*['deleteid ' + value for value in staged])
            except Exception:
                pass
            raise RuntimeError('Could not queue the playlist. Your previous queue was retained.')
        dsp.call('SetMute', True)
        try:
            mpd('random 0', 'single 0', 'repeat 0', 'consume 0', 'playid ' + ids[position])
            if old_ids:
                mpd(*['deleteid ' + value for value in old_ids])
            dsp.call('SetMute', False)
        except Exception:
            dsp.call('SetMute', True)
            raise RuntimeError('Could not start the playlist. Playback was muted; please retry.')
    return current_status()


def stream_url(identifier):
    with TIDAL_LOCK:
        track = tidal().track(int(track_id(identifier)))
        stream = track.get_stream()
        manifest = stream.get_stream_manifest()
        if manifest.is_encrypted:
            raise RuntimeError('This stream needs a different Tidal login')
        if manifest.is_mpd:
            return ('dash', stream.get_manifest_data())
        if not manifest.urls or not manifest.urls[0].startswith('https://'):
            raise RuntimeError('No supported Tidal stream is available')
        return ('redirect', manifest.urls[0])


def approved(dsp):
    path = dsp.call('GetConfigFilePath')
    if path not in PROFILES and not TUNING.active(path):
        raise RuntimeError('Select an approved exciter DSP preset before playback')


def require_tidal(dsp):
    if source_metadata(json.loads(dsp.call('GetConfigJson')))['id'] != 'tidal':
        raise RuntimeError('Select Tidal as the source before starting music')


def restore_channel_profile(previous):
    # Keep rollback bytes and the mute retry latched until durable restore and
    # DSP reload both succeed. Cancel any pending automatic AirPlay unmute.
    AIRPLAY_CONTROL.invalidate(manual=False)
    AIRPLAY_CONTROL.restore_pending = previous
    AIRPLAY_CONTROL.needs_mute = True
    AIRPLAY_CONTROL.recover()


def select_source(identifier, defer_ready=False):
    global ACTIVE_CAPTURE_CHANNELS
    import yaml
    if not isinstance(identifier, str):
        raise ValueError('Choose an audio source')
    source = next((item for item in discover_sources() if item['id'] == identifier), None)
    if source is None:
        raise ValueError('That input is no longer connected. Refresh the available sources.')
    with DSP() as dsp:
        if dsp.call('GetConfigFilePath') != str(CHANNEL_PROFILE):
            raise RuntimeError('Source selection requires the Output DSP controls preset')
        config = json.loads(dsp.call('GetConfigJson'))
        if source_metadata(config)['id'] == identifier and dsp.call('GetState') == 'Running':
            return (None, config) if defer_ready else None
    updated = source_config(config, source)
    candidate = CHANNEL_PROFILE.with_name('channel-dsp-source-candidate.yml')
    previous = CHANNEL_PROFILE.read_bytes()
    try:
        candidate.write_text(yaml.safe_dump(updated, sort_keys=False))
        candidate.chmod(0o640)
        validation = subprocess.run(['/opt/camilladsp/camilladsp', '--check', str(candidate)], capture_output=True, timeout=10)
        if validation.returncode:
            raise RuntimeError('DSP rejected the source; the previous source was retained')
        INPUT_LEVELS.release()
        with DSP() as dsp:
            dsp.call('SetMute', True)
        try:
            # Retain the queue and its position, but never play Tidal in the background.
            if not defer_ready:
                mpd('pause 1')
            candidate.replace(CHANNEL_PROFILE)
            with DSP() as dsp:
                dsp.call('Reload')
            if defer_ready:
                return previous, updated
            for _ in range(60):
                time.sleep(.1)
                with DSP() as dsp:
                    loaded = json.loads(dsp.call('GetConfigJson'))
                    if (source_metadata(loaded)['id'] == identifier
                            and all(loaded['devices']['capture'].get(key) == value
                                    for key, value in updated['devices']['capture'].items())
                            and dsp.call('GetState') == 'Running'):
                        ACTIVE_CAPTURE_CHANNELS = meter_channels(loaded)
                        return  # Explicit Unmute/Play is required after a source change.
            raise RuntimeError('The selected input did not start')
        except Exception:
            restore_channel_profile(previous)
            ACTIVE_CAPTURE_CHANNELS = meter_channels(config)
            raise RuntimeError('Source change failed; the previous source was restored and output is muted')
    finally:
        candidate.unlink(missing_ok=True)


def volume_ceiling():
    return HIGH_DB if LEVELS_UNLOCKED else MAX_DB


def eq_block(config, output, required=True):
    """Find only the optional measured EQ block; never match a protection block."""
    matches = [block for block in config['pipeline']
               if block.get('type') == 'Filter' and block.get('channels') == [output - 1]
               and block.get('description') == f'Output {output} measured EQ'
               and block.get('names')
               and all(name.startswith(('exciter_ex30_', 'exciter_ex32_')) and not name.endswith('_headroom')
                       for name in block['names'])]
    if not matches and not required:
        return None
    if len(matches) != 1:
        raise RuntimeError('No verified measured EQ is assigned to this output')
    return matches[0]


def unmeasured_config(config):
    """Retire unverified correction and its headroom, retaining routing and guards."""
    config = json.loads(json.dumps(config))
    retired = ('panel_left_', 'panel_right_', 'exciter_ex30_', 'exciter_ex32_')
    pipeline = []
    for stage in config['pipeline']:
        if stage.get('type') == 'Filter':
            stage['names'] = [name for name in stage.get('names', []) if not name.startswith(retired)]
            if not stage['names']:
                continue
        pipeline.append(stage)
    config['pipeline'] = pipeline
    config['filters'] = {name: value for name, value in config['filters'].items() if not name.startswith(retired)}
    config['title'] = 'Exciter audition · no measured EQ'
    config['description'] = 'EX30 on 1/2; EX32 on 3/4. No verified measured correction assigned. Temporary 500 Hz–5 kHz filters and -12 dB attenuation remain.'
    config['devices']['volume_limit'] = HIGH_DB
    return with_polarity_controls(config)


def channel_config(config):
    """Give both stereo outputs of each exciter model identical measured filters."""
    config = json.loads(json.dumps(config))
    prefixes = ('exciter_ex30_', 'exciter_ex32_')
    if any(name.startswith(('panel_left_', 'panel_right_')) for name in config['filters']):
        raise ValueError('Legacy left/right measurements are unverified and must not be installed')
    if not any(name.startswith(prefixes) for name in config['filters']):
        return unmeasured_config(config)
    assigned = tuple(prefix for prefix in prefixes if any(name.startswith(prefix) for name in config['filters']))
    for prefix in assigned:
        if prefix + 'headroom' not in config['filters']:
            raise ValueError('Expected exported headroom for each assigned exciter EQ')
    config['pipeline'] = [block for block in config['pipeline']
                          if not any(name.startswith(prefixes) for name in block.get('names', []))]
    for index, prefix in enumerate(prefixes):
        if prefix in assigned:
            config['pipeline'].append(dict(type='Filter', channels=[index * 2, index * 2 + 1], names=[prefix + 'headroom']))
    for output in range(1, 5):
        prefix = prefixes[(output - 1) // 2]
        if prefix not in assigned:
            continue
        names = [name for name in config['filters'] if name.startswith(prefix) and not name.endswith('_headroom')]
        names.sort(key=lambda name: int(name.rsplit('_', 1)[1]))
        config['pipeline'].append(dict(type='Filter', channels=[output - 1], names=names,
                                      description=f'Output {output} measured EQ', bypassed=False))
    config['title'] = 'Measured exciter EQ with independent output controls'
    config['description'] = 'Verified EQ is assigned by exciter model; each measured stereo pair shares filters and headroom. Unmeasured outputs retain audition filters only.'
    config['devices']['volume_limit'] = HIGH_DB
    return with_polarity_controls(config)


def assign_subs_and_tweeters(config, sub_hp, sub_lp, sub_gain, tweeter_hp, tweeter_lp, tweeter_gain):
    """Add protected stereo output pairs, initially muted; preserve exciter settings."""
    if not (0 < sub_hp < sub_lp < 24000 and 0 < tweeter_hp < tweeter_lp < 24000):
        raise ValueError('Expected valid high-pass and low-pass frequencies')
    if not (-60 <= sub_gain <= 0 and -60 <= tweeter_gain <= 0):
        raise ValueError('Expected nonpositive starting output gains')
    config = json.loads(json.dumps(config))
    mixer = config['mixers']['exciter_pairs']
    mixer['mapping'] = [entry for entry in mixer['mapping'] if entry['dest'] < 4]
    for channel in range(4, 8):
        mixer['mapping'].append(dict(dest=channel, sources=[dict(channel=channel % 2, gain=0)]))
    prefixes = ('sub_', 'tweeter_')
    config['pipeline'] = [stage for stage in config['pipeline']
                          if not any(name.startswith(prefixes) for name in stage.get('names', []))]
    for role, channels, hp, lp, gain in (
            ('sub', [4, 5], sub_hp, sub_lp, sub_gain),
            ('tweeter', [6, 7], tweeter_hp, tweeter_lp, tweeter_gain)):
        config['filters'][role + '_hp'] = dict(type='BiquadCombo', parameters=dict(type='LinkwitzRileyHighpass', freq=hp, order=4))
        config['filters'][role + '_lp'] = dict(type='BiquadCombo', parameters=dict(type='LinkwitzRileyLowpass', freq=lp, order=4))
        config['filters'][role + '_attenuation'] = dict(type='Gain', parameters=dict(gain=gain, inverted=False, mute=False))
        config['pipeline'].append(dict(type='Filter', channels=channels,
                                      names=[role + '_hp', role + '_lp', role + '_attenuation'],
                                      description=role.capitalize() + ' starting crossover and level'))
    config['title'] = 'Eight-output speaker DSP'
    config['description'] = '1/2 EX30, 3/4 EX32, 5/6 left/right subs, 7/8 left/right tweeters. New pairs start muted; crossover and level settings are provisional.'
    return with_polarity_controls(config, outputs=range(5, 9), muted=True)


def with_polarity_controls(config, outputs=range(1, 5), muted=False):
    config = json.loads(json.dumps(config))
    for output in outputs:
        name = f'output_{output}_polarity'
        if name not in config['filters']:
            config['filters'][name] = dict(type='Gain', parameters=dict(gain=0.0, inverted=False, mute=muted))
        description = f'Output {output} polarity'
        if not any(stage.get('description') == description for stage in config['pipeline']):
            config['pipeline'].append(dict(type='Filter', channels=[output - 1], names=[name], description=description))
    return config


def polarity_parameters(config, output):
    if output not in range(1, 9):
        raise RuntimeError('This output has no assigned polarity control')
    name = f'output_{output}_polarity'
    fil = config.get('filters', {}).get(name, {})
    stages = [stage for stage in config['pipeline'] if stage.get('description') == f'Output {output} polarity'
              and stage.get('channels') == [output - 1] and stage.get('names') == [name]]
    if len(stages) != 1 or fil.get('type') != 'Gain' or fil.get('parameters', {}).get('gain') != 0:
        raise RuntimeError('This output has no assigned polarity control')
    return fil['parameters']


def change_channel_setting(config, output, control, enabled):
    if control == 'eq':
        eq_block(config, output)['bypassed'] = not enabled
    elif control == 'polarity':
        polarity_parameters(config, output)['inverted'] = enabled
    elif control == 'mute':
        polarity_parameters(config, output)['mute'] = enabled
    elif control == 'all-eq':
        blocks = [eq_block(config, channel, required=False) for channel in range(1, 5)]
        blocks = [block for block in blocks if block is not None]
        if not blocks:
            raise RuntimeError('No verified measured EQ is assigned to any output')
        for block in blocks:
            block['bypassed'] = not enabled
    else:
        raise ValueError('Unknown output control')


def always_active_controls(config, output):
    """Editable values outside measured EQ and the dedicated mute/polarity stage."""
    block = eq_block(config, output, required=False)
    controls = {}
    for stage in config['pipeline']:
        if (stage.get('type') != 'Filter' or output - 1 not in stage.get('channels', [])
                or stage is block or stage.get('bypassed')):
            continue
        for name in stage.get('names', []):
            fil = config['filters'][name]
            params = fil.get('parameters', {})
            kind = params.get('type', '')
            if fil.get('type') == 'Gain' and name != f'output_{output}_polarity':
                controls[name] = dict(name=name, parameter='gain', label='Attenuation', unit='dB', value=params['gain'])
            elif fil.get('type') in ('Biquad', 'BiquadCombo') and kind in (
                    'Highpass', 'Lowpass', 'LinkwitzRileyHighpass', 'LinkwitzRileyLowpass',
                    'ButterworthHighpass', 'ButterworthLowpass'):
                controls[name] = dict(name=name, parameter='freq', label='High-pass' if 'Highpass' in kind else 'Low-pass',
                                      unit='Hz', value=params['freq'])
    if not any(item['parameter'] == 'gain' for item in controls.values()):
        controls['__attenuation'] = dict(name='__attenuation', parameter='gain', label='Attenuation', unit='dB', value=0)
    return list(controls.values())


def change_always_active(config, output, values):
    polarity_parameters(config, output)
    controls = always_active_controls(config, output)
    if not isinstance(values, dict) or set(values) != {item['name'] for item in controls}:
        raise ValueError('Output settings changed; reload the page and try again')
    nyquist = config['devices']['samplerate'] / 2
    for item in controls:
        value = values[item['name']]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Enter finite numbers for all output settings')
        if item['parameter'] == 'gain' and not -60 <= value <= 0:
            raise ValueError('Attenuation must be between -60 and 0 dB')
        if item['parameter'] == 'freq' and not 0 < value < nyquist:
            raise ValueError(f'Frequencies must be above 0 and below {nyquist:g} Hz')
        if output in (7, 8) and item['label'] == 'High-pass' and value < 3500:
            raise ValueError('Tweeter safety high-pass must remain at or above 3500 Hz')
    highs = [values[item['name']] for item in controls if item['label'] == 'High-pass']
    lows = [values[item['name']] for item in controls if item['label'] == 'Low-pass']
    if highs and lows and max(highs) >= min(lows):
        raise ValueError('High-pass must be below low-pass')
    replacements = {}
    for item in controls:
        name = item['name']
        if values[name] == item['value']:
            continue
        # Private names and split stages keep edits from affecting any other output.
        prefix = f'output_{output}_active_'
        private = name if name.startswith(prefix) else prefix + name
        fil = (dict(type='Gain', parameters=dict(gain=0, inverted=False, mute=False))
               if name == '__attenuation' else json.loads(json.dumps(config['filters'][name])))
        fil['parameters'][item['parameter']] = values[name]
        config['filters'][private] = fil
        replacements[name] = private
    pipeline = []
    for stage in config['pipeline']:
        if (stage.get('type') == 'Filter' and output - 1 in stage.get('channels', [])
                and any(name in replacements for name in stage.get('names', []))):
            own = dict(stage, channels=[output - 1], names=[replacements.get(name, name) for name in stage['names']])
            others = [channel for channel in stage['channels'] if channel != output - 1]
            if others:
                pipeline.append(dict(stage, channels=others))
            pipeline.append(own)
        else:
            pipeline.append(stage)
    if '__attenuation' in replacements:
        pipeline.append(dict(type='Filter', channels=[output - 1], names=[replacements['__attenuation']],
                             description=f'Output {output} attenuation'))
    config['pipeline'] = pipeline


def set_channel_control(output, control, enabled):
    if control != 'all-eq' and (isinstance(output, bool) or not isinstance(output, int) or output not in range(1, 9)):
        raise ValueError('Expected an output number from 1 to 8')
    if control != 'always-active' and not isinstance(enabled, bool):
        raise ValueError('Expected a boolean setting')
    with DSP() as dsp:
        if dsp.call('GetConfigFilePath') != str(CHANNEL_PROFILE):
            raise RuntimeError('The independent output preset is not active')
        config = json.loads(dsp.call('GetConfigJson'))
        if control == 'always-active':
            change_always_active(config, output, enabled)
        else:
            change_channel_setting(config, output, control, enabled)
        previous_mute = dsp.call('GetMute')
    import yaml
    previous = CHANNEL_PROFILE.read_bytes()
    candidate = CHANNEL_PROFILE.with_name('channel-dsp-candidate.yml')
    candidate.write_text(yaml.safe_dump(config, sort_keys=False))
    candidate.chmod(0o640)
    validation = subprocess.run(['/opt/camilladsp/camilladsp', '--check', str(candidate)], capture_output=True, timeout=10)
    if validation.returncode:
        raise RuntimeError('DSP rejected the change; current settings were retained')
    with DSP() as dsp:
        dsp.call('SetMute', True)
    try:
        candidate.replace(CHANNEL_PROFILE)
        with DSP() as dsp:
            dsp.call('Reload')
        for _ in range(25):
            time.sleep(.08)
            with DSP() as dsp:
                loaded = json.loads(dsp.call('GetConfigJson'))
                if control == 'always-active':
                    applied = always_active_controls(loaded, output) == always_active_controls(config, output)
                elif control == 'all-eq':
                    applied = all(bool(eq_block(loaded, channel).get('bypassed')) == (not enabled) for channel in range(1, 5) if eq_block(config, channel, required=False) is not None)
                elif control == 'eq':
                    applied = bool(eq_block(loaded, output).get('bypassed')) == (not enabled)
                elif control == 'mute':
                    applied = bool(polarity_parameters(loaded, output).get('mute')) == enabled
                else:
                    applied = bool(polarity_parameters(loaded, output).get('inverted')) == enabled
                if applied:
                    if dsp.call('GetState') == 'Running':
                        dsp.call('SetMute', previous_mute)
                        return
        raise RuntimeError('DSP did not confirm the change')
    except Exception:
        restore_channel_profile(previous)
        raise RuntimeError('DSP change failed; previous settings restored and output muted')


def set_channel_eq(output, enabled):
    return set_channel_control(output, 'eq', enabled)


def dsp_status():
    with LOCK, DSP() as dsp:
        config = json.loads(dsp.call('GetConfigJson'))
        path = dsp.call('GetConfigFilePath')
        result = dict(profile=config.get('title', 'CamillaDSP'), volume=dsp.call('GetVolume'),
                      muted=dsp.call('GetMute'), level_unlocked=LEVELS_UNLOCKED,
                      min_db=MIN_DB, max_db=volume_ceiling(), channels=[])
        result['sample_rate'] = config.get('devices', {}).get('samplerate', 48000)
    tuned = TUNING.active(path) if path != str(CHANNEL_PROFILE) else None
    if tuned:
        result.update(auto_tuned=True, tuning=tuned, profile=tuned['name'])
        return result
    filters = config.get('filters', {})
    roles = ['EX30HESF2-4', 'EX30HESF2-4', 'EX32VBDS-4', 'EX32VBDS-4', 'Subwoofer', 'Subwoofer', 'PRO Neo-8 tweeter', 'PRO Neo-8 tweeter']
    for output in range(1, 9):
        try:
            parameters = polarity_parameters(config, output) if path == str(CHANNEL_PROFILE) else None
        except RuntimeError:
            parameters = None
        available = parameters is not None
        block = eq_block(config, output, required=False) if available else None
        names = block['names'] if block else []
        protected = []
        for stage in config.get('pipeline', []):
            if stage.get('type') == 'Filter' and output - 1 in stage.get('channels', []) and stage is not block:
                protected.extend(stage.get('names', []))
        def describe(name):
            return dict(name=name, **filters[name])
        result['channels'].append(dict(output=output, driver=roles[output - 1] if available else 'Unassigned',
                                      role=('exciter' if output <= 4 else 'subwoofer' if output <= 6 else 'tweeter') if available else 'unassigned',
                                      input=('Left' if output % 2 else 'Right') if available else None,
                                      measurement=(filters.get(('exciter_ex30_' if output <= 2 else 'exciter_ex32_') + 'headroom', {}).get('description') or 'Measured ' + roles[output - 1] + ' EQ') if block else None,
                                      available=available, eq_available=block is not None, enabled=not bool(block.get('bypassed')) if block else False,
                                      inverted=bool(polarity_parameters(config, output).get('inverted')) if available else False,
                                      muted=bool(polarity_parameters(config, output).get('mute')) if available else False,
                                      filters=[describe(name) for name in names],
                                      always_active_controls=always_active_controls(config, output) if available else [],
                                      always_active=[describe(name) for name in protected]))
    return result


def current_status():
    global ACTIVE_CAPTURE_CHANNELS
    result = dict(tidal_connected=SESSION is not None, profile='Exciter audition',
                  profile_description='Outputs 1–4 · 500 Hz–5 kHz · outputs 5–8 silent',
                  min_db=MIN_DB, max_db=volume_ceiling(), level_unlocked=LEVELS_UNLOCKED,
                  queue=[], playback='stop', elapsed=0, duration=0,
                  sources=discover_sources(), source='unknown', source_label='Source unavailable')
    try:
        status, song, playlist = mpd('status', 'currentsong', 'playlistinfo')
        status, song = dict(status), dict(song)
        result.update(playback=status.get('state', 'stop'), elapsed=float(status.get('elapsed', 0)),
                      duration=float(status.get('duration', 0)), song_position=int(status.get('song', -1)))
        rows = []
        for key, value in playlist:
            if key == 'file':
                match = re.search(r'/stream/([0-9]+)$', value)
                identifier = match.group(1) if match else ''
                rows.append(dict(CATALOG.get(identifier, dict(id=identifier, title='Tidal track', artist='', album='', cover='')), position=len(rows)))
        result['queue'] = rows
        position = result.get('song_position', -1)
        result['current'] = rows[position] if 0 <= position < len(rows) else None
    except (OSError, RuntimeError, ValueError):
        result['player_error'] = 'Music player is offline'
    try:
        with LOCK, DSP() as dsp:
            profile = dsp.call('GetConfigFilePath')
            config = json.loads(dsp.call('GetConfigJson'))
            source = source_metadata(config)
            ACTIVE_CAPTURE_CHANNELS = meter_channels(config)
            selected = next((item for item in result['sources'] if item['id'] == source['id']), None)
            if selected is None:
                result['sources'].append(dict(id=source['id'], label=source['label'] + ' · disconnected',
                                              available=False, group='Unavailable', description='Reconnect the interface or choose Tidal.'))
            result.update(source=source['id'], source_label=source['label'])
            tuned = TUNING.active(profile)
            label, description = PROFILES.get(profile, (tuned['name'], 'Saved system tuning · tweeter safety high-pass active') if tuned else ('Other DSP preset', 'Playback controls require an approved exciter preset'))
            result.update(volume=dsp.call('GetVolume'), muted=dsp.call('GetMute'),
                          dsp_state=dsp.call('GetState'), ready=(profile in PROFILES or bool(tuned)) and selected is not None,
                          profile=label, profile_description=description)
            result['source_switching_available'] = profile == str(CHANNEL_PROFILE)
    except (OSError, RuntimeError, ValueError):
        result.update(volume=-36, muted=True, ready=False, dsp_state='Offline')
    result['airplay'] = AIRPLAY_CONTROL.status()
    return result


AIRPLAY_CONTROL = AirPlayController(sys.modules[__name__])
# Disabled unless the root-owned local policy is enabled; started only over the
# root-only local socket, never over HTTP.
BLUETOOTH_CONTROL = BluetoothControl(sys.modules[__name__])


def action(name, data):
    # The ownership check and the mutation share the same lock as starting Auto mode.
    with LOCK:
        return _action(name, data)


def _action(name, data):
    global LEVELS_UNLOCKED
    if AIRPLAY_CONTROL.needs_mute or AIRPLAY_CONTROL.baseline.exists():
        if (name in ('auto-stop', 'stop') or (name == 'mute' and data.get('muted') is True)) and TUNING.status().get('audio_owned'):
            AIRPLAY_CONTROL.mute()
            return TUNING.request('stop', {})
        if AIRPLAY_CONTROL.needs_mute:
            AIRPLAY_CONTROL.recover()
        if AIRPLAY_CONTROL.baseline.exists():
            AIRPLAY_CONTROL.invalidate()
    if BLUETOOTH_CONTROL.owns_audio():
        # Every manual or Auto Tune request overrides Bluetooth, which stops muted.
        # A failed restore raises here, so no mutation proceeds on its route.
        BLUETOOTH_CONTROL.stop('Manual control override')
    if name.startswith('auto-'):
        with LOCK:
            AIRPLAY_CONTROL.invalidate()
            INPUT_LEVELS.release()
            return TUNING.request(name.removeprefix('auto-'), data)
    tuning = TUNING.status()
    if tuning.get('audio_owned'):
        if name == 'stop' or (name == 'mute' and data.get('muted') is True):
            return TUNING.request('stop', {})
        raise RuntimeError('Auto mode owns the audio outputs. Stop and restore settings before using music or manual DSP controls.')
    if name == 'play-playlist':
        return play_playlist(data)
    with LOCK:
        if AIRPLAY_CONTROL.pending is not None and name in (
                'channel-eq', 'channel-mute', 'channel-polarity', 'channel-always-active', 'all-eq'):
            AIRPLAY_CONTROL.invalidate()
        if name == 'source':
            AIRPLAY_CONTROL.invalidate()
            with DSP() as dsp:
                dsp.call('SetMute', True)
            select_source(data.get('id'))
        elif name == 'unlock':
            enabled = data.get('enabled')
            if not isinstance(enabled, bool):
                raise ValueError('Expected an unlock setting')
            with DSP() as dsp:
                approved(dsp)
                if not enabled and dsp.call('GetVolume') > MAX_DB:
                    dsp.call('SetVolume', MAX_DB)
            LEVELS_UNLOCKED = enabled
        elif name == 'channel-eq':
            set_channel_eq(data.get('output'), data.get('enabled'))
        elif name == 'channel-mute':
            set_channel_control(data.get('output'), 'mute', data.get('muted'))
        elif name == 'channel-polarity':
            set_channel_control(data.get('output'), 'polarity', data.get('inverted'))
        elif name == 'channel-always-active':
            set_channel_control(data.get('output'), 'always-active', data.get('values'))
        elif name == 'all-eq':
            set_channel_control(None, 'all-eq', data.get('enabled'))
        elif name == 'stop':
            AIRPLAY_CONTROL.invalidate()
            with DSP() as dsp:
                dsp.call('SetMute', True)
            mpd('stop')
        elif name == 'mute':
            AIRPLAY_CONTROL.invalidate()
            if not isinstance(data.get('muted'), bool):
                raise ValueError('Expected a mute setting')
            with DSP() as dsp:
                if not data['muted']:
                    approved(dsp)
                    source = source_metadata(json.loads(dsp.call('GetConfigJson')))['id']
                    if source not in {item['id'] for item in discover_sources()} or dsp.call('GetState') != 'Running':
                        raise RuntimeError('The selected source is unavailable. Reconnect it or choose Tidal.')
                dsp.call('SetMute', data['muted'])
        elif name == 'volume':
            value = float(data['db'])
            if not math.isfinite(value) or not MIN_DB <= value <= volume_ceiling():
                raise ValueError('Volume is outside the currently unlocked range')
            with DSP() as dsp:
                approved(dsp)
                dsp.call('SetVolume', value)
        elif name in ('play', 'enqueue'):
            identifier = track_id(data['id'])
            if identifier not in CATALOG:
                with TIDAL_LOCK:
                    remember(tidal().track(int(identifier)))
            STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
            (STATE / 'catalog.json').write_text(json.dumps(CATALOG))
            with DSP() as dsp:
                approved(dsp)
                if name == 'play':
                    require_tidal(dsp)
                result = mpd('addid ' + quote(f'http://{NETWORK.stream_host(BIND)}:{PORT}/stream/{identifier}'))
                songid = dict(result[0])['Id']
                if name == 'play':
                    dsp.call('SetMute', False)
                    try:
                        mpd('playid ' + songid)
                    except Exception:
                        dsp.call('SetMute', True)
                        raise
        elif name in ('resume', 'next', 'previous', 'position'):
            with DSP() as dsp:
                approved(dsp)
                require_tidal(dsp)
                command = {'resume': 'play', 'next': 'next', 'previous': 'previous'}.get(name)
                if name == 'position':
                    position = int(data['position'])
                    if position < 0:
                        raise ValueError('Invalid queue position')
                    command = 'play ' + str(position)
                mpd(command)
                dsp.call('SetMute', False)
        elif name == 'pause':
            mpd('pause 1')
        elif name == 'clear':
            with DSP() as dsp:
                if source_metadata(json.loads(dsp.call('GetConfigJson')))['id'] == 'tidal':
                    dsp.call('SetMute', True)
            mpd('clear')
        elif name == 'seek':
            seconds = float(data['seconds'])
            if not math.isfinite(seconds) or seconds < 0 or seconds > 86400:
                raise ValueError('Invalid seek position')
            with DSP() as dsp:
                require_tidal(dsp)
            mpd('seekcur ' + str(seconds))
        else:
            raise ValueError('Unknown control')
    return current_status()


def visualizer_frame(dsp):
    """Read source envelopes before output volume/EQ, without another audio clock."""
    levels = dsp.call('GetSignalLevelsSince', 0.1)
    running = dsp.call('GetState') == 'Running'

    def channels(name):
        values = levels.get(name, [])
        return [max(-120.0, min(0.0, float(value)))
                if isinstance(value, (int, float)) and math.isfinite(value) else -120.0
                for channel in ACTIVE_CAPTURE_CHANNELS if channel < len(values)
                for value in [values[channel]]] if isinstance(values, list) else []

    rms, peak = channels('capture_rms'), channels('capture_peak')
    if running and (not rms or not peak):
        # A short history window can be empty even while the latest capture
        # chunk has live audio. Keep its paired RMS/peak values together.
        levels = dsp.call('GetSignalLevels')
        rms, peak = channels('capture_rms'), channels('capture_peak')
    return dict(available=True, active=running and bool(rms) and bool(peak),
                rms=rms if running else [], peak=peak if running else [])


class Handler(BaseHTTPRequestHandler):
    server_version = 'Octet'

    def log_message(self, *args):
        pass

    def send(self, value, status=200, content_type='application/json', headers=None):
        body = json.dumps(value, allow_nan=False).encode() if content_type == 'application/json' else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob: https://resources.tidal.com; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        if not NETWORK.allowed_peer(self.client_address[0], BIND):
            return False
        host = self.headers.get('Host', '').lower()
        return host in NETWORK.hosts(PORT, BIND) or (
            self.https_proxy() and host in {'octet.tailf55458.ts.net', 'octet.tailf55458.ts.net:443'})

    def https_proxy(self):
        # Tailscale Serve overwrites these headers. Only trust the local proxy,
        # never forwarded headers supplied directly by another tailnet device.
        return (NETWORK.local_peer(self.client_address[0], BIND)
                and self.headers.get('X-Forwarded-Proto') == 'https'
                and self.headers.get('X-Forwarded-Host', '').lower() in {
                    'octet.tailf55458.ts.net', 'octet.tailf55458.ts.net:443'})

    def request_origin(self):
        if self.https_proxy():
            return 'https://' + self.headers['X-Forwarded-Host']
        return 'http://' + self.headers.get('Host', '')

    def serve_media(self, name, head_only=False):
        """Bounded local media delivery with byte ranges for seeking/looping."""
        path = ROOT / 'visualizer-media' / name
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        byte_range = self.headers.get('Range')
        if byte_range:
            match = re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})', byte_range)
            valid = bool(match and any(match.groups()))
            if valid:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    valid = int(last) > 0
                    start = max(0, size - int(last))
                valid = valid and 0 <= start <= end < size
            if not valid:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header('Content-Type', 'video/mp4' if name.endswith('.mp4') else 'audio/mpeg')
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if head_only:
            return
        try:
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            self.close_connection = True

    def do_HEAD(self):
        if not self.valid_host():
            self.send_error(403)
            return
        name = urlparse(self.path).path.removeprefix('/visualizer-media/')
        if self.path.startswith('/visualizer-media/') and name in ('sky.mp4', 'water.mp4', 'stars.mp4', 'solar.mp4', 'sun.mp4', 'lunar.mp4', 'sky-playback.mp4', 'water-playback.mp4', 'stars-playback.mp4', 'lunar-playback.mp4', 'edm.mp3', 'classical.mp3'):
            return self.serve_media(name, head_only=True)
        self.send_error(404)

    def stream_audio(self, url):
        """Buffer the beginning of a progressive stream; preserve byte-range seeks."""
        headers = {'Accept-Encoding': 'identity'}
        byte_range = self.headers.get('Range')
        if byte_range:
            if not re.fullmatch(r'bytes=(?:[0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})', byte_range):
                raise ValueError('Invalid audio byte range')
            headers['Range'] = byte_range
        with urlopen(Request(url, headers=headers), timeout=30) as upstream:
            if upstream.status not in (200, 206):
                raise RuntimeError('The audio source returned an unexpected response')
            # Do not release an almost-empty stream to the decoder. This stays
            # bounded per connection and does not store audio on disk.
            first = upstream.read(STREAM_PREBUFFER_BYTES)
            if not first:
                raise RuntimeError('The audio source returned no data')
            self.send_response(upstream.status)
            for name in ('Content-Type', 'Content-Length', 'Content-Range', 'Accept-Ranges'):
                if value := upstream.headers.get(name):
                    self.send_header(name, value)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(first)
                while chunk := upstream.read(64 * 1024):
                    self.wfile.write(chunk)
            except (OSError, IncompleteRead):
                # Once headers are sent, never append a JSON error to audio.
                self.close_connection = True

    def stream_visualizer(self):
        # One read-only control socket per visible viewer. No PCM, Tidal URLs,
        # microphone permissions, FIFO output, or playback changes are needed.
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True

        def emit(frame):
            self.wfile.write(('data: ' + json.dumps(frame, allow_nan=False) + '\n\n').encode())
            self.wfile.flush()

        try:
            self.wfile.write(b'retry: 2000\n\n')
            with DSP() as dsp:
                while True:
                    started = time.monotonic()
                    emit(visualizer_frame(dsp))
                    time.sleep(max(0, 1 / 15 - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            try:
                emit(dict(available=False, active=False, rms=[], peak=[]))
            except OSError:
                pass

    def do_GET(self):
        if not self.valid_host():
            return self.send({'error': 'Unrecognized host'}, 403)
        parsed = urlparse(self.path)
        try:
            if parsed.path == '/api/auto':
                return self.send(TUNING.status())
            measurement = re.fullmatch(r'/api/auto/profiles/([A-Za-z0-9-]{1,100})/measurements', parsed.path)
            if measurement:
                return self.send(TUNING.measurements(measurement.group(1)))
            if parsed.path == '/auto.js':
                return self.send((ROOT / 'auto.js').read_bytes(), content_type='text/javascript; charset=utf-8')
            if parsed.path == '/measurements.js':
                return self.send((ROOT / 'measurements.js').read_bytes(), content_type='text/javascript; charset=utf-8')
            if parsed.path == '/input-levels.js':
                return self.send((ROOT / 'input-levels.js').read_bytes(), content_type='text/javascript; charset=utf-8')
            if parsed.path == '/visualizer/display':
                body = (ROOT / 'visualizer.html').read_bytes().replace(b'class="visualizer-page"', b'class="visualizer-page projection"')
                return self.send(body, content_type='text/html; charset=utf-8')
            if parsed.path == '/visualizer-sync.js':
                return self.send((ROOT / 'visualizer-sync.js').read_bytes(), content_type='text/javascript; charset=utf-8')
            if parsed.path == '/api/visualizer/settings':
                query = parse_qs(parsed.query)
                if query.get('display') == ['1']:
                    try:
                        view = json.loads(query.get('view', ['null'])[0][:512])
                    except ValueError:
                        view = None
                    VISUALIZER.heartbeat(query.get('revision', [''])[0], view)
                return self.send(VISUALIZER.read())
            if parsed.path == '/api/visualizer/frame':
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                frame = VISUALIZER.preview_frame(after)
                if frame is None:
                    return self.send({'error': 'HDMI frame unavailable'}, 503)
                frame_id, data = frame
                return self.send(data or b'', 200 if data else 204, 'image/jpeg', {'X-Octet-Frame': frame_id})
            if parsed.path in ('/', '/dsp', '/visualizer', '/app.js', '/dsp.js', '/visualizer.js', '/visualizer-themes.js', '/visualizer-solar.js', '/visualizer-audio.js', '/style.css', '/visualizer.css'):
                name = {'/': 'index.html', '/dsp': 'dsp.html', '/visualizer': 'visualizer.html', '/app.js': 'app.js', '/dsp.js': 'dsp.js', '/visualizer.js': 'visualizer.js', '/visualizer-themes.js': 'visualizer-themes.js', '/visualizer-solar.js': 'visualizer-solar.js', '/visualizer-audio.js': 'visualizer-audio.js', '/style.css': 'style.css', '/visualizer.css': 'visualizer.css'}[parsed.path]
                kind = ('text/html' if name.endswith('.html') else 'text/javascript' if name.endswith('.js') else 'text/css') + '; charset=utf-8'
                return self.send((ROOT / name).read_bytes(), content_type=kind)
            if parsed.path in ('/visualizer-media/sky.jpg', '/visualizer-media/water.jpg', '/visualizer-media/stars.jpg', '/visualizer-media/solar.jpg', '/visualizer-media/sun.jpg', '/visualizer-media/lunar.jpg'):
                return self.send((ROOT / parsed.path.lstrip('/')).read_bytes(), content_type='image/jpeg')
            if parsed.path in ('/visualizer-media/sky.mp4', '/visualizer-media/water.mp4', '/visualizer-media/stars.mp4', '/visualizer-media/solar.mp4', '/visualizer-media/sun.mp4', '/visualizer-media/lunar.mp4', '/visualizer-media/sky-playback.mp4', '/visualizer-media/water-playback.mp4', '/visualizer-media/stars-playback.mp4', '/visualizer-media/lunar-playback.mp4', '/visualizer-media/edm.mp3', '/visualizer-media/classical.mp3'):
                return self.serve_media(parsed.path.rsplit('/', 1)[1])
            if parsed.path == '/api/visualizer':
                return self.stream_visualizer()
            if parsed.path == '/api/status':
                return self.send(current_status())
            if parsed.path == '/api/now-playing':
                return self.send({'song': fetch_song()})
            artwork = re.fullmatch(r'/api/now-playing/artwork/([a-f0-9]{64})', parsed.path)
            if artwork:
                try:
                    body, kind = fetch_artwork(artwork.group(1))
                except (OSError, ValueError):
                    return self.send({'error': 'Artwork unavailable'}, 404)
                return self.send(body, content_type=kind)
            if parsed.path == '/api/input-levels':
                return self.send(INPUT_LEVELS.snapshot())
            if parsed.path == '/api/dsp':
                return self.send(dsp_status())
            if parsed.path == '/api/playlists':
                offset = int(parse_qs(parsed.query).get('offset', ['0'])[0])
                return self.send(playlist_page(offset))
            if parsed.path == '/api/playlist':
                query = parse_qs(parsed.query)
                return self.send(playlist_contents(query.get('id', [''])[0], refresh=query.get('refresh') == ['1']))
            if parsed.path in ('/api/search', '/api/favorites', '/api/album'):
                with TIDAL_LOCK:
                    session = tidal()
                    if parsed.path == '/api/search':
                        import tidalapi
                        query = parse_qs(parsed.query).get('q', [''])[0].strip()[:200]
                        results = session.search(query, models=[tidalapi.Track, tidalapi.Album], limit=20) if query else {}
                        albums = [dict(id=str(a.id), title=a.name, artist=a.artist.name,
                                       tracks=a.num_tracks) for a in results.get('albums', [])]
                        return self.send({'tracks': [remember(t) for t in results.get('tracks', [])], 'albums': albums})
                    elif parsed.path == '/api/album':
                        identifier = track_id(parse_qs(parsed.query).get('id', [''])[0])
                        tracks = session.album(int(identifier)).tracks()
                    else:
                        tracks = session.user.favorites.tracks(limit=30)
                    return self.send({'tracks': [remember(t) for t in tracks]})
            match = re.fullmatch(r'/stream/([0-9]{1,12})', parsed.path)
            if match:
                kind, content = stream_url(match.group(1))
                if kind == 'dash':
                    return self.send(content.encode() if isinstance(content, str) else content, content_type='application/dash+xml')
                self.stream_audio(content)
                return
            return self.send({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            message = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Tidal is unavailable. Please try again.'
            self.send({'error': message}, 503)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or (origin and origin != self.request_origin()):
            return self.send({'error': 'Request blocked'}, 403)
        if urlparse(self.path).path == '/api/control/visualizer-frame':
            try:
                # Only the Pi's own kiosk may supply the HDMI image. A remote
                # browser opening /visualizer/display must not replace it.
                if (not NETWORK.local_peer(self.client_address[0], BIND)
                        or self.headers.get('X-Forwarded-For')
                        or self.headers.get('X-Forwarded-Proto')):
                    return self.send({'error': 'Local display required'}, 403)
                if self.headers.get('Content-Type') != 'image/jpeg':
                    return self.send({'error': 'Expected JPEG'}, 415)
                length = int(self.headers.get('Content-Length', 0))
                if not 4 <= length <= 524288:
                    raise ValueError('Invalid frame size')
                revision = parse_qs(urlparse(self.path).query).get('revision', [''])[0]
                accepted = VISUALIZER.publish_frame(revision, self.rfile.read(length))
                return self.send({'accepted': accepted}, 200 if accepted else 409)
            except ValueError as error:
                return self.send({'error': str(error)}, 400)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send({'error': 'Expected JSON'}, 415)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
            if self.path == '/api/control/visualizer':
                return self.send(VISUALIZER.update(data))
            if not self.path.startswith('/api/control/'):
                return self.send({'error': 'Not found'}, 404)
            self.send(action(self.path.removeprefix('/api/control/'), data))
        except ValueError as error:
            self.send({'error': str(error)}, 400)
        except (KeyError, TypeError):
            self.send({'error': 'Invalid control request'}, 400)
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else 'Could not reach the music player'
            self.send({'error': message}, 503)


def initialize_dsp():
    global LEVELS_UNLOCKED, ACTIVE_CAPTURE_CHANNELS
    def confirm_mute():
        with DSP() as dsp:
            dsp.call('SetMute', True)
            if dsp.call('GetMute') is not True:
                raise RuntimeError('DSP mute could not be confirmed')

    with LOCK:
        AIRPLAY_CONTROL.needs_mute = True
        try:
            confirm_mute()
            AIRPLAY_CONTROL.recover()
        except Exception:
            AIRPLAY_CONTROL.needs_mute = True
            AIRPLAY_CONTROL.error = 'DSP initialization/recovery failed; output must remain muted'
        finally:
            try:
                # Independent of AirPlay; failure retains its journal and error.
                BLUETOOTH_CONTROL.startup()
            except Exception:
                logging.warning('Bluetooth startup recovery failed; output remains muted')
            # Recovery can reload the DSP or fail partway through. Always use a
            # fresh connection to mute again, and refuse startup without proof.
            try:
                confirm_mute()
            except Exception as error:
                AIRPLAY_CONTROL.needs_mute = True
                AIRPLAY_CONTROL.error = 'DSP mute could not be confirmed; refusing remote startup'
                raise RuntimeError(AIRPLAY_CONTROL.error) from error
        try:
            with DSP() as dsp:
                LEVELS_UNLOCKED = dsp.call('GetVolume') > MAX_DB
                ACTIVE_CAPTURE_CHANNELS = meter_channels(json.loads(dsp.call('GetConfigJson')))
        except Exception:
            AIRPLAY_CONTROL.needs_mute = True
            AIRPLAY_CONTROL.error = 'DSP initialization/recovery failed; output must remain muted'


def main():
    os.umask(0o077)
    initialize_dsp()
    if Path('/run/octet-airplay').exists():
        threading.Thread(target=serve_airplay, args=(AIRPLAY_CONTROL,), daemon=True).start()
    if Path('/run/octet-bluetooth').exists():
        # RuntimeDirectory exists only with the guarded Bluetooth installer drop-in.
        threading.Thread(target=serve_bluetooth, args=(BLUETOOTH_CONTROL,), daemon=True).start()
        # Optional observer/worker after muted recovery; failures are contained and leave
        # the manual socket, Tidal and AirPlay running.
        # Automatic unmute is deployment-gated until the private DSP proxy and
        # all clients are verified. A runtime directory alone is not consent.
        if os.environ.get('OCTET_BLUETOOTH_AUTO') == '1':
            start_auto_bluetooth(BLUETOOTH_CONTROL)
    print(f'Octet music controls: http://{BIND}:{PORT}', flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
