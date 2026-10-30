"""Validated, durable settings shared by HDMI and web visualizer clients."""
import json
import math
from pathlib import Path
import threading
import time
import uuid

THEMES = ('off', 'turell 1', 'turell 2', 'turell 3', 'turell 4', 'turell 5', 'turell lunar', 'well', 'overlap', 'water')
PALETTES = ('cycle', 'scrub', 'lime', 'magenta', 'ultraviolet', 'lagoon', 'watermelon', 'violet', 'indigo', 'rose', 'ochre', 'cyan', 'plum', 'pearl')


def validate(data):
    if not isinstance(data, dict) or set(data) - {'theme', 'palette', 'paletteClock', 'halo', 'edge', 'response', 'intensity', 'circleSize', 'zoom', 'solar', 'media'}:
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
    if 'circleSize' in data:
        number(data['circleSize'], .25, 2)
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
        self.settings = dict(theme='off', palette='cycle', paletteClock=0, halo='curated', edge=.35,
                             response='combined', intensity='balanced', circleSize=1, zoom={},
                             solar=dict(tone='natural', mode='realism', speed=1))
        self.updated = time.time()
        self.animation_epoch = time.time()
        self.revision = uuid.uuid4().hex
        self.display_seen = 0
        self.display_revision = ''
        self.display_view = None
        self.frame_data = None
        self.frame_seen = 0
        self.frame_id = 0
        self.preview_seen = -math.inf
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
                        revision=self.revision, serverTime=time.time(), animationEpoch=self.animation_epoch,
                        previewRequested=time.monotonic()-self.preview_seen < 5,
                        display=dict(online=self.settings['theme'] != 'off' and time.time()-self.display_seen<6,
                                     revision=self.display_revision, view=self.display_view))

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
            if settings['theme'] == 'off':
                self.display_seen = 0
                self.display_revision = ''
                self.display_view = None
                self.frame_data = None
            return self.read()

    def publish_frame(self, revision, data):
        if not isinstance(data, bytes) or not 4 <= len(data) <= 524288 or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
            raise ValueError('Invalid preview frame')
        with self.lock:
            if revision != self.revision:
                return False
            self.frame_data = data
            self.frame_seen = time.monotonic()
            self.frame_id += 1
            return True

    def preview_frame(self, after=0):
        with self.lock:
            self.preview_seen = time.monotonic()
            if self.frame_data is None or time.monotonic() - self.frame_seen > 8:
                return None
            if after == self.frame_id:
                return (self.frame_id, None)
            return (self.frame_id, self.frame_data)
