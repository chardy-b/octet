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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BIND = os.environ.get('OCTET_BIND', '100.100.177.81')
PORT = int(os.environ.get('OCTET_PORT', '8767'))
ROOT = Path(__file__).parent
STATE = Path(os.environ.get('STATE_DIRECTORY', '/var/lib/octet-remote'))
PROFILE = '/opt/camilladsp/configs/exciter-audition.yml'
PROFILES = {
    PROFILE: ('Exciter audition', 'Outputs 1–4 · 500 Hz–5 kHz · outputs 5–8 silent'),
}
MIN_DB, MAX_DB = -60.0, -18.0
HIGH_DB = 0.0
LEVELS_UNLOCKED = False
CHANNEL_PROFILE = Path('/var/lib/octet-dsp/channel-dsp.yml')
PROFILES[str(CHANNEL_PROFILE)] = ('Output DSP controls', 'EX30 → 1/2 · EX32 → 3/4 · subs → 5/6 · tweeters → 7/8')
LOCK = threading.RLock()
TIDAL_LOCK = threading.RLock()
SESSION = None
CATALOG = {}
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


class DSP:
    """Small synchronous RFC6455 client for the local CamillaDSP control socket."""
    def __enter__(self):
        self.sock = socket.create_connection(('127.0.0.1', 1234), timeout=4)
        key = base64.b64encode(os.urandom(16)).decode()
        request = ('GET / HTTP/1.1\r\nHost: 127.0.0.1:1234\r\nUpgrade: websocket\r\n'
                   'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(request.encode())
        response = bytearray()
        while not response.endswith(b'\r\n\r\n') and len(response) < 8192:
            response.extend(read_exact(self.sock, 1))
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        if not response.startswith(b'HTTP/1.1 101 ') or expected not in response:
            self.sock.close()
            raise RuntimeError('DSP connection failed')
        return self

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
    if dsp.call('GetConfigFilePath') not in PROFILES:
        raise RuntimeError('Select an approved exciter DSP preset before playback')


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


def set_channel_control(output, control, enabled):
    if not isinstance(enabled, bool) or (control != 'all-eq' and
        (isinstance(output, bool) or not isinstance(output, int) or output not in range(1, 9))):
        raise ValueError('Expected an output number and boolean setting')
    with DSP() as dsp:
        if dsp.call('GetConfigFilePath') != str(CHANNEL_PROFILE):
            raise RuntimeError('The independent output preset is not active')
        config = json.loads(dsp.call('GetConfigJson'))
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
                if control == 'all-eq':
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
        CHANNEL_PROFILE.write_bytes(previous)
        CHANNEL_PROFILE.chmod(0o640)
        with DSP() as dsp:
            dsp.call('SetMute', True)
            dsp.call('Reload')
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
                                      always_active=[describe(name) for name in protected]))
    return result


def current_status():
    result = dict(tidal_connected=SESSION is not None, profile='Exciter audition',
                  profile_description='Outputs 1–4 · 500 Hz–5 kHz · outputs 5–8 silent',
                  min_db=MIN_DB, max_db=volume_ceiling(), level_unlocked=LEVELS_UNLOCKED,
                  queue=[], playback='stop', elapsed=0, duration=0)
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
        with DSP() as dsp:
            profile = dsp.call('GetConfigFilePath')
            label, description = PROFILES.get(profile, ('Other DSP preset', 'Playback controls require an approved exciter preset'))
            result.update(volume=dsp.call('GetVolume'), muted=dsp.call('GetMute'),
                          dsp_state=dsp.call('GetState'), ready=profile in PROFILES,
                          profile=label, profile_description=description)
    except (OSError, RuntimeError, ValueError):
        result.update(volume=-36, muted=True, ready=False, dsp_state='Offline')
    return result


def action(name, data):
    global LEVELS_UNLOCKED
    with LOCK:
        if name == 'unlock':
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
        elif name == 'all-eq':
            set_channel_control(None, 'all-eq', data.get('enabled'))
        elif name == 'stop':
            with DSP() as dsp:
                dsp.call('SetMute', True)
            mpd('stop')
        elif name == 'mute':
            if not isinstance(data.get('muted'), bool):
                raise ValueError('Expected a mute setting')
            with DSP() as dsp:
                if not data['muted']:
                    approved(dsp)
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
                result = mpd('addid ' + quote(f'http://{BIND}:{PORT}/stream/{identifier}'))
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
                dsp.call('SetMute', True)
            mpd('clear')
        elif name == 'seek':
            seconds = float(data['seconds'])
            if not math.isfinite(seconds) or seconds < 0 or seconds > 86400:
                raise ValueError('Invalid seek position')
            mpd('seekcur ' + str(seconds))
        else:
            raise ValueError('Unknown control')
    return current_status()


class Handler(BaseHTTPRequestHandler):
    server_version = 'Octet'

    def log_message(self, *args):
        pass

    def send(self, value, status=200, content_type='application/json'):
        body = json.dumps(value, allow_nan=False).encode() if content_type == 'application/json' else value
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' https://resources.tidal.com; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get('Host', '').lower() in {f'{BIND}:{PORT}', f'octet:{PORT}', f'octet.tailf55458.ts.net:{PORT}'}

    def do_GET(self):
        if not self.valid_host():
            return self.send({'error': 'Unrecognized host'}, 403)
        parsed = urlparse(self.path)
        try:
            if parsed.path in ('/', '/dsp', '/app.js', '/dsp.js', '/style.css'):
                name = {'/': 'index.html', '/dsp': 'dsp.html', '/app.js': 'app.js', '/dsp.js': 'dsp.js', '/style.css': 'style.css'}[parsed.path]
                kind = ('text/html' if name.endswith('.html') else 'text/javascript' if name.endswith('.js') else 'text/css') + '; charset=utf-8'
                return self.send((ROOT / name).read_bytes(), content_type=kind)
            if parsed.path == '/api/status':
                return self.send(current_status())
            if parsed.path == '/api/dsp':
                return self.send(dsp_status())
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
                self.send_response(302)
                self.send_header('Location', content)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            return self.send({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            message = str(error) if isinstance(error, (RuntimeError, ValueError)) else 'Tidal is unavailable. Please try again.'
            self.send({'error': message}, 503)

    def do_POST(self):
        origin = self.headers.get('Origin')
        if not self.valid_host() or (origin and origin != 'http://' + self.headers.get('Host', '')):
            return self.send({'error': 'Request blocked'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self.send({'error': 'Expected JSON'}, 415)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 4096:
                raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('Expected an object')
            if not self.path.startswith('/api/control/'):
                return self.send({'error': 'Not found'}, 404)
            self.send(action(self.path.removeprefix('/api/control/'), data))
        except (KeyError, TypeError, ValueError):
            self.send({'error': 'Invalid control request'}, 400)
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else 'Could not reach the music player'
            self.send({'error': message}, 503)


if __name__ == '__main__':
    os.umask(0o077)
    try:
        with DSP() as dsp:
            LEVELS_UNLOCKED = dsp.call('GetVolume') > MAX_DB
    except (OSError, RuntimeError):
        pass
    print(f'Octet music controls: http://{BIND}:{PORT}', flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
