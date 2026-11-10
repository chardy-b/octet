#!/usr/bin/env python3
"""Root-only DAC reboot broker and post-reboot PipeWire readiness gate."""

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

STATE = Path('/var/lib/octet-dac-recovery')
BOOT_STATE = Path('/run/octet-audio-boot/mpd.json')
MPD_STATE = Path('/var/lib/mpd/state')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
DMA_SUMMARY = Path('/sys/kernel/debug/dmaengine/summary')
DAC_STATUS = Path('/proc/asound/sndrpihifiberry/pcm0p/sub0/status')
LOOPBACK = Path('/proc/asound/Loopback')
COOLDOWN = 3600
GRAPH = frozenset((
    'alsa_card.platform-snd_aloop.0',
    'alsa_output.platform-snd_aloop.0.analog-stereo',
    'alsa_input.platform-snd_aloop.0.analog-stereo',
    'alsa_card.platform-soc_107c000000_sound',
    'alsa_output.platform-soc_107c000000_sound.stereo-fallback',
))


def run(*args, timeout=4):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=timeout).stdout


def read_attempt():
    try:
        return json.loads((STATE / 'attempt.json').read_text())
    except FileNotFoundError:
        return None


def save_json(path, record, group=0):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream)
            stream.flush()
            if group:
                os.fchown(stream.fileno(), 0, group)
            os.fchmod(stream.fileno(), 0o640 if group else 0o600)
            os.fsync(stream.fileno())
            temporary.replace(path)
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def save_attempt(record):
    save_json(STATE / 'attempt.json', record)


def paused_mpd_record(raw):
    header, sep, playlist = raw.partition('\nplaylist_begin\n')
    playlist, end, _ = playlist.partition('\nplaylist_end')
    if not sep or not end:
        return None
    fields = dict(line.split(': ', 1) for line in header.splitlines() if ': ' in line)
    if fields.get('state') not in ('play', 'pause'):
        return None
    files = [line.removeprefix('song_begin: ') for line in playlist.splitlines() if line.startswith('song_begin: ')]
    try:
        position, elapsed = int(fields['current']), float(fields['time'])
    except (KeyError, ValueError):
        return None
    if (not 0 <= position < len(files) or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400
            or not re.fullmatch(r'http://127\.0\.0\.1:8767/stream/\d+(?:\?entry=[a-f0-9]+)?', files[position])):
        return None
    return dict(position=position, elapsed=elapsed, queue=hashlib.sha256(json.dumps(files).encode()).hexdigest())


def pcm_sample():
    fields = {key.strip(): value.strip() for key, sep, value in
              (line.partition(':') for line in DAC_STATUS.read_text().splitlines()) if sep}
    return fields.get('owner_pid'), fields.get('state'), fields.get('hw_ptr')


def fault_evidence():
    channels = re.findall(r'^\s*(dma\d+chan\d+)\s*\|\s*1f000a0000\.i2s:tx[ \t]*$',
                          DMA_SUMMARY.read_text(), re.M)
    if len(channels) != 1:
        raise RuntimeError('The DAC I²S transmit DMA channel could not be identified')
    channel = channels[0]
    before = pcm_sample()
    time.sleep(.4)
    after = pcm_sample()
    if (not before[0] or before[0] != after[0] or not before[2] or not after[2]
            or (after[1] == 'RUNNING' and before[2] != after[2])):
        raise RuntimeError('The DAC PCM is progressing or its owner changed')
    pattern = f'^dma {channel}: {channel} (failed to stop|is non-idle!)$'
    try:
        lines = run('/usr/bin/journalctl', '-q', '-k', '-b', '--since=-15s', '--no-pager',
                    '--output=short-iso-precise', '--lines=24', '--grep', pattern).splitlines()
    except subprocess.CalledProcessError as error:
        if error.returncode != 1 or error.stderr:
            raise
        lines = []
    if (sum(line.endswith('failed to stop') for line in lines) < 2
            or sum(line.endswith('is non-idle!') for line in lines) < 2):
        raise RuntimeError('No repeated recent DAC DMA stop errors were confirmed')
    return lines


def handle(operation):
    with (STATE / 'reboot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        boot = BOOT_ID.read_text().strip()
        previous = read_attempt()
        if operation == b'ready':
            if not previous or previous.get('graph_verified') != boot:
                raise RuntimeError('The audio graph was not verified on this recovery boot')
            return
        if previous and (previous['boot'] == boot or time.time() - previous['time'] < COOLDOWN):
            raise RuntimeError('The DAC recovery reboot limit has been reached')
        for unit in ('camilladsp.service', 'mpd.service'):
            required = run('/usr/bin/systemctl', 'show', unit, '-p', 'Requires', '--value')
            if 'octet-dac-graph.service' not in required.split():
                raise RuntimeError('The DAC recovery boot graph gate is not enabled')
        startup = run('/usr/bin/systemctl', 'show', 'camilladsp.service', '-p', 'ExecStart', '--value')
        if '--mute' not in startup.split():
            raise RuntimeError('CamillaDSP startup mute could not be verified')
        lines = fault_evidence()
        if operation == b'check':
            return
        if (LOOPBACK / 'pcm0p/sub0/status').read_text().strip() != 'closed':
            raise RuntimeError('The MPD Loopback device is still open')
        # Record before scheduling. A failure or crash consumes the attempt.
        save_attempt(dict(boot=boot, time=time.time(), kernel=lines))
        run('/usr/bin/systemd-run', '--quiet', '--on-active=3s', '--timer-property=AccuracySec=100ms',
            '--unit=octet-dac-reboot-now', '/usr/bin/systemctl', '--no-block', 'reboot')


def serve():
    with socket.socket(fileno=0) as connection:
        connection.settimeout(2)
        peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        _, uid, _ = struct.unpack('3i', peer)
        if uid != pwd.getpwnam('upmpdcli').pw_uid:
            return
        with connection.makefile('rb') as stream:
            operation = stream.readline(8)
        if operation not in (b'check\n', b'reboot\n', b'ready\n'):
            return
        try:
            handle(operation.strip())
        except RuntimeError as error:
            connection.sendall(f'NO {error}\n'.encode())
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            print(f'DAC reboot broker: {error}', file=sys.stderr)
            connection.sendall(b'NO DAC reboot could not be scheduled or verified. Check the Octet logs.\n')
        else:
            connection.sendall(b'OK\n')


def boot_graph():
    attempt = read_attempt()
    boot = BOOT_ID.read_text().strip()
    # Both MPD and CamillaDSP are ordered behind this gate before opening PCM.
    statuses = [*LOOPBACK.glob('pcm*/sub*/status'), DAC_STATUS]
    if len(statuses) < 3 or any(path.read_text().strip() != 'closed' for path in statuses):
        raise RuntimeError('DAC recovery requires owner-free Loopback and DAC devices')
    octet = ('/usr/sbin/runuser', '-u', 'octet', '--', '/usr/bin/env',
             'XDG_RUNTIME_DIR=/run/user/1000', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus')
    for _ in range(12):
        if Path('/run/user/1000/bus').exists():
            break
        time.sleep(1)
    run(*octet, '/usr/bin/systemctl', '--user', 'start', 'wireplumber.service', timeout=6)
    for _ in range(12):
        try:
            graph = run(*octet, '/usr/bin/wpctl', 'status', '-n', timeout=1)
        except subprocess.CalledProcessError:
            graph = ''
        if GRAPH <= set(graph.split()):
            if attempt and attempt['boot'] != boot:
                save_attempt(dict(attempt, graph_verified=boot))
            try:
                fd = os.open(MPD_STATE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd) as state:
                    raw = state.read(4 * 1024 * 1024 + 1) if stat.S_ISREG(os.fstat(fd).st_mode) else ''
            except (OSError, UnicodeError):
                raw = ''
            record = paused_mpd_record(raw) if len(raw) <= 4 * 1024 * 1024 else None
            if record:
                save_json(BOOT_STATE, dict(record, boot=boot), pwd.getpwnam('upmpdcli').pw_gid)
            print('Verified all five Octet audio nodes before starting MPD and CamillaDSP', flush=True)
            return
        time.sleep(1)
    raise RuntimeError('Octet’s Loopback and DAC desktop nodes did not return')


if __name__ == '__main__':
    if sys.argv[1:] == ['graph']:
        boot_graph()
    elif not sys.argv[1:]:
        serve()
    else:
        raise SystemExit('Invalid DAC recovery command')
