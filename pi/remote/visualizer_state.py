"""Validated, durable settings shared by HDMI and web visualizer clients."""
import json
import math
from pathlib import Path
import threading
import time
import uuid

THEMES = ('turell 1', 'turell 2', 'turell 3', 'turell 4', 'turell 5', 'turell lunar', 'well', 'overlap', 'water')
PALETTES = ('cycle', 'scrub', 'lime', 'magenta', 'ultraviolet', 'lagoon', 'watermelon', 'violet', 'indigo', 'rose', 'ochre', 'cyan', 'plum', 'pearl')


def validate(data):
    if not isinstance(data, dict) or set(data) - {'theme', 'palette', 'paletteClock', 'halo', 'edge', 'response', 'intensity', 'zoom', 'solar', 'media'}:
        raise ValueError('Invalid visualizer settings')
    def choice(value, allowed):
        if value not in allowed:
            raise ValueError('Invalid visualizer option')
    def number(value, low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('Invalid visualizer range')
    for key, allowed in [('theme', THEMES), ('palette', PALETTES), ('halo', ('curated', 'bridge', 'contrast')),
                         ('response', ('original', 'bloom', 'depth', 'rim', 'shimmer', 'combined')),
                         ('intensity', ('subtle', 'balanced', 'strong'))]:
        if key in data:
            choice(data[key], allowed)
    for key, high in [('paletteClock', 384), ('edge', 1)]:
        if key in data:
            number(data[key], 0, high)
    if 'zoom' in data:
        if not isinstance(data['zoom'], dict) or set(data['zoom']) - set(THEMES):
            raise ValueError('Invalid visualizer zoom')
        for value in data['zoom'].values():
            number(value, .5, 3)
    if 'solar' in data:
        solar = data['solar']
        if not isinstance(solar, dict) or set(solar) != {'tone', 'mode', 'speed'}:
            raise ValueError('Invalid eclipse settings')
        choice(solar['tone'], ('natural', 'cycle', 'yellow', 'amber', 'red'))
        choice(solar['mode'], ('realism', 'artistic'))
        choice(solar['speed'], (.25, .5, 1, 2, 4))
    if 'media' in data:
        media = data['media']
        if not isinstance(media, dict) or set(media) != {'theme', 'time', 'paused'}:
            raise ValueError('Invalid visualizer playback')
        choice(media['theme'], THEMES)
        number(media['time'], 0, 86400)
        if not isinstance(media['paused'], bool):
            raise ValueError('Invalid playback state')
    return data


class VisualizerState:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.settings = dict(theme='turell 1', palette='cycle', paletteClock=0, halo='curated', edge=.35,
                             response='combined', intensity='balanced', zoom={},
                             solar=dict(tone='natural', mode='realism', speed=1))
        self.updated = time.time()
        self.revision = uuid.uuid4().hex
        self.display_seen = 0
        self.display_revision = ''
        self.display_view = None
        try:
            saved = json.loads(self.path.read_text())
            settings = validate(saved['settings'])
            updated = float(saved['updated'])
            if not math.isfinite(updated):
                raise ValueError('Invalid saved time')
            self.settings.update(settings)
            self.updated = updated
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def read(self):
        with self.lock:
            return dict(settings=json.loads(json.dumps(self.settings)), updated=self.updated,
                        revision=self.revision, serverTime=time.time(),
                        display=dict(online=time.time()-self.display_seen<6, revision=self.display_revision, view=self.display_view))

    def heartbeat(self, revision, view=None):
        with self.lock:
            self.display_seen = time.time()
            self.display_revision = revision if isinstance(revision, str) and len(revision) == 32 else ''
            if isinstance(view, dict) and set(view) == {'theme', 'fullscreen', 'controls', 'width', 'height', 'renderer'}:
                if (view['theme'] in THEMES and isinstance(view['fullscreen'], bool)
                        and view['renderer'] in ('canvas', 'webgl', 'fallback')
                        and all(type(view[key]) is int and 0 <= view[key] <= high
                                for key, high in [('controls', 1000), ('width', 16384), ('height', 16384)])):
                    self.display_view = dict(view)

    def update(self, patch):
        validate(patch)
        with self.lock:
            settings = {**self.settings, **patch}
            updated = time.time()
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(dict(settings=settings, updated=updated), allow_nan=False))
            temp.chmod(0o600)
            temp.replace(self.path)
            self.settings, self.updated, self.revision = settings, updated, uuid.uuid4().hex
            return self.read()
