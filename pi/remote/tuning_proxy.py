"""Small, fixed-endpoint bridge to the Pi's local measurement service."""
import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TuningProxy:
    def __init__(self, root='/var/lib/octet-autotune', url='http://127.0.0.1:8766'):
        self.root, self.url = Path(root), url

    def request(self, action=None, body=None):
        if action not in (None, 'start', 'stop', 'restore', 'select', 'test-level'):
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

    def saved(self, path=None, identifier=None):
        """The saved-profile record (presets/*.json) for a config path or profile id."""
        try:
            records = [json.loads(item.read_text()) for item in sorted((self.root/'presets').glob('*.json'))]
        except (OSError, ValueError):
            return None
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('config_path'), str):
                continue
            if ((path is not None and record['config_path'] == str(path))
                    or (identifier is not None and record.get('id') == identifier)):
                return record if Path(record['config_path']).is_file() else None
        return None

    def active(self, path):
        try:
            record = json.loads((self.root/'active.json').read_text())
            # The profile selected on the DSP page; its bytes are not re-checked.
            if record['config_path'] == str(path) and Path(path).is_file():
                return record
        except (OSError, ValueError, KeyError):
            pass
        return None
