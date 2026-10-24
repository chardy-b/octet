"""Small, fixed-endpoint bridge to the Pi's local measurement service."""
import hashlib
import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TuningProxy:
    def __init__(self, root='/var/lib/octet-autotune', url='http://127.0.0.1:8766'):
        self.root, self.url = Path(root), url

    def request(self, action=None, body=None):
        if action not in (None, 'start', 'stop', 'restore', 'select'):
            raise ValueError('Unknown Auto mode action')
        request = Request(self.url+'/api/auto'+('/'+action if action else ''),
                          data=json.dumps(body or {}).encode() if action else None,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=8 if action else 2) as response:
                return json.load(response)
        except HTTPError as error:
            detail = json.load(error).get('detail', 'Auto mode request failed')
            raise ValueError(str(detail)) from error
        except (OSError, URLError) as error:
            raise RuntimeError('Auto Tune service is unavailable; existing tuning has not been changed.') from error

    def status(self):
        if self.root.exists():
            try:
                return self.request()
            except RuntimeError:
                pass
        path = self.root/'current.json'
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        return dict(available=False, running=False, audio_owned=state.get('audio_owned', False),
                    profiles=[], message='Auto Tune service is offline. Restore the previous settings before playback if a run was interrupted.')

    def measurements(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', identifier):
            raise ValueError('Invalid saved profile ID')
        try:
            with urlopen(self.url+'/api/auto/profiles/'+identifier+'/measurements', timeout=8) as response:
                return json.load(response)
        except HTTPError as error:
            raise ValueError(str(json.load(error).get('detail', 'Measurements unavailable'))) from error
        except (OSError, URLError) as error:
            raise RuntimeError('Could not load the saved measurements. The profile has not been changed.') from error

    def active(self, path):
        try:
            record = json.loads((self.root/'active.json').read_text())
            if record['config_path'] == str(path) and hashlib.sha256(Path(path).read_bytes()).hexdigest() == record['sha256']:
                return record
        except (OSError, ValueError, KeyError):
            pass
        return None
