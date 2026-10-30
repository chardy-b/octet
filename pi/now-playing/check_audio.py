"""Capture/check playback invariants around a publisher-only deployment."""
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen

status = json.load(urlopen('http://127.0.0.1:8767/api/status', timeout=10))
snapshot = {key: status.get(key) for key in ('source', 'volume', 'muted', 'dsp_state')}
snapshot['processes'] = subprocess.check_output([
    'systemctl', 'show', '--property=MainPID', 'camilladsp', 'mpd', 'shairport-sync', 'octet-remote'
]).decode()
path = Path('/tmp/octet-now-playing-audio-before.json')
if sys.argv[1:] == ['capture']:
    path.write_text(json.dumps(snapshot))
else:
    assert snapshot == json.loads(path.read_text()), 'Playback state or process changed'
print(json.dumps(snapshot))
