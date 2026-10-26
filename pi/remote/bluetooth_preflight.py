#!/usr/bin/env python3
"""Read-only WIL-249 prerequisites, never a receiver activation command."""
import json
from pathlib import Path
import re
import shutil
import subprocess

PLAYBACK = 'hw:Loopback,0,2'
CAPTURE = 'hw:Loopback,1,2'


def fields(path):
    result = {}
    for line in path.read_text().splitlines():
        if ':' not in line:
            continue
        key, value = (part.strip() for part in line.split(':', 1))
        if key in result:
            raise ValueError('Duplicate endpoint field')
        result[key] = value
    return result


def check_loopback(proc=Path('/proc/asound')):
    """Validate identity and both idle subdevice-2 endpoints without opening PCM."""
    proc = proc.resolve(strict=True)
    card = (proc / 'Loopback').resolve(strict=True)
    if card.parent != proc or not re.fullmatch(r'card[0-9]+', card.name):
        raise ValueError('Invalid Loopback card link')
    index = str(int(card.name[4:]))
    if (card / 'id').read_text().strip() != 'Loopback':
        raise ValueError('Incorrect ALSA card ID')
    modules = [line.split() for line in (proc / 'modules').read_text().splitlines()]
    if [row for row in modules if row and row[0] == index] != [[index, 'snd_aloop']]:
        raise ValueError('Loopback must use snd_aloop')
    for endpoint, device, direction in (('pcm0p', '0', 'PLAYBACK'), ('pcm1c', '1', 'CAPTURE')):
        sub = card / endpoint / 'sub2'
        # Do not accept a symlink to sub0/sub1 or another sound card.
        if sub.resolve(strict=True) != sub:
            raise ValueError('Redirected Loopback endpoint')
        for name in ('info', 'status'):
            metadata = sub / name
            if metadata.resolve(strict=True) != metadata:
                raise ValueError('Redirected Loopback endpoint metadata')
        info = fields(sub / 'info')
        expected = dict(card=index, device=device, subdevice='2', stream=direction)
        if any(info.get(key) != value for key, value in expected.items()):
            raise ValueError('Incorrect Loopback endpoint')
        if (sub / 'status').read_text().strip() != 'closed':
            raise ValueError('Dedicated Bluetooth endpoint is busy or status is unknown')
    return int(index)


def preflight(proc=Path('/proc/asound')):
    blockers = []
    # BlueALSA renamed its daemon; accept either explicit binary, never an audio fallback.
    daemon = shutil.which('bluealsad') or shutil.which('bluealsa')
    player = shutil.which('bluealsa-aplay')
    for name, executable in (('bluealsad (or bluealsa)', daemon), ('bluealsa-aplay', player)):
        if executable is None:
            blockers.append(f'Missing executable: {name}')
            continue
        try:
            result = subprocess.run([executable, '--version'], capture_output=True, timeout=2)
            if result.returncode != 0:
                blockers.append(f'Binary version check failed: {name}')
        except (OSError, subprocess.TimeoutExpired):
            blockers.append(f'Binary version check unavailable or timed out: {name}')
    try:
        check_loopback(proc)
    except (OSError, ValueError, RuntimeError) as error:
        blockers.append(f'Dedicated snd_aloop playback 0,2 and capture 1,2 required: {error}')
    return dict(prerequisites_ok=not blockers, available=False, advertise=False,
                playback=PLAYBACK, capture=CAPTURE, blockers=blockers,
                pending=['Protected 44100/S32_LE/stereo route and live format verification',
                         'BlueZ/BlueALSA A2DP sink capability and runtime verification',
                         'Pairing policy and fail-closed ownership/lifecycle controller'])


def main():
    result = preflight()
    print(json.dumps(result, indent=2))
    return 0 if result['prerequisites_ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
