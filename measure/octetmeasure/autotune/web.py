"""Pi-only Auto mode and immutable listening presets, behind the music web server."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException
import yaml

from . import config
from .backend import Hardware, exclusive
from .camilla import atomic, control
from .runner import Runner


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_safety(document, limits=False):
    """Reject a recall if either tweeter has lost its non-bypassable protection."""
    for output, side in ((6, 'L'), (7, 'R')):
        stages = [s for s in document.get('pipeline', []) if s.get('type') == 'Filter'
                  and output in s.get('channels', []) and not s.get('bypassed')]
        filters = [document['filters'][n] for s in stages for n in s.get('names', [])]
        if not any(f.get('type') == 'BiquadCombo' and
                   f.get('parameters', {}).get('type') == 'LinkwitzRileyHighpass' and
                   f['parameters'].get('freq', 0) >= config.TWEETER_MIN_HZ and
                   f['parameters'].get('order') in (4, 8) for f in filters):
            raise ValueError(f'{side} tweeter is missing its 3500 Hz safety high-pass.')
    if limits:
        for output in range(8):
            names = [n for s in document['pipeline'] if s.get('type') == 'Filter'
                     and output in s.get('channels', []) and not s.get('bypassed') for n in s['names']]
            if not names or document['filters'][names[-1]].get('type') != 'Limiter':
                raise ValueError('Every tuned output must end with a hard sample ceiling.')


class Controller:
    def __init__(self, root, demo=False):
        self.root = Path(root)
        self.demo = demo
        self.lock = threading.RLock()
        self.meter = None
        self.runner = Runner(self.root, demo=demo, emit=self.event)

    def event(self, event):
        if event.get('type') == 'auto_meter':
            self.meter = event['peak_db']
        if event.get('status') == 'applied' and not event.get('running', True):
            try:
                self.publish(event)
            except Exception as error:
                self.runner.state.update(status='failed', audio_owned=True,
                                         message=f'Tuning completed but saving or restarting music failed: {error}. Restore the pre-tuning settings.')
                atomic(self.root/'current.json', json.dumps(self.runner.state, indent=2))

    def write_preset(self, identifier, name, path, **extra):
        document = yaml.safe_load(Path(path).read_text())
        check_safety(document, limits=extra.get('kind') == 'tuned')
        record = dict(id=identifier, name=name, config_path=str(Path(path).resolve()),
                      sha256=digest(path), created=datetime.now(timezone.utc).isoformat(), **extra)
        atomic(self.root/'presets'/f'{identifier}.json', json.dumps(record, indent=2))
        return record

    def publish(self, state):
        path = self.root/'presets'/f"{state['id']}.json"
        if not path.exists():
            self.write_preset(state['id'], state['cfg'].get('name', 'Auto Tune'),
                              self.root/state['id']/'camilladsp.yml', kind='tuned', chosen=state['chosen'],
                              verification=state.get('final_verification', {}), demo=self.demo,
                              selected_pairs=config.selected_pairs(state['cfg']),
                              active_driver_ids=[d['id'] for d in config.active_drivers(state['cfg'])],
                              failed_drivers=state.get('failed_drivers',{}),
                              stereo_skipped=state.get('stereo_skipped'),
                              stereo=state.get('stereo',{}),
                              stereo_verification=state.get('stereo_verification'),
                              active_stereo_verification=state.get('active_stereo_verification'),
                              driver_polarity_report=state.get('driver_polarity_report'),
                              measurement_repeats=state['cfg'].get('measurement_repeats',1),
                              baseline_measured=state.get('baseline_measured',False),
                              playback_max_db=state['cfg'].get('playback_max_db'),
                              test_attenuation_db=state['cfg'].get('test_attenuation_db',0),
                              preamp_db={side:20*math.log10(scale) for side,scale in state['preamp'].items()})
        record = json.loads(path.read_text())
        atomic(self.root/'active.json', json.dumps(record, indent=2))
        if not self.demo:
            self.restart_producers(state['cfg'])

    def restart_producers(self, cfg):
        backend = Hardware(cfg)
        for service in cfg['quiesce_services']:
            backend.systemctl('start', service)

    def profiles(self):
        rows = []
        for path in (self.root/'presets').glob('*.json'):
            row = json.loads(path.read_text())
            if row.get('demo', False) == self.demo:
                rows.append(row)
        return sorted(rows, key=lambda row: row['created'], reverse=True)

    def measurements(self, identifier):
        from .results import measurements
        record = next((row for row in self.profiles() if row['id'] == identifier), None)
        if record is None:
            raise ValueError('Select a saved profile to view its measurements.')
        return measurements(self.root, record)

    def status(self):
        state = self.runner.status()
        active = self.root/'active.json'
        return dict(available=True, status=state.get('status', 'idle'), stage=state.get('stage'),
                    running=self.runner.running, audio_owned=state.get('audio_owned', False),
                    message=state.get('message', 'Place the microphone at your listening position, then start Auto mode.'),
                    peak_db=self.meter, profiles=self.profiles(),
                    active=json.loads(active.read_text()) if active.exists() else None,
                    chosen=state.get('chosen'), verification=state.get('final_verification'),
                    stereo=state.get('stereo'), stereo_verification=state.get('stereo_verification'),
                    levels=state.get('level_report', {}), tweeter_safety_hz=config.TWEETER_MIN_HZ,
                    selected_pairs=self.selection(), test_attenuation_db=self.test_attenuation(),
                    failed_drivers=state.get('failed_drivers',{}), last_error=state.get('last_error'))

    def test_attenuation(self):
        path = self.root/'test-level.json'
        value = (json.loads(path.read_text())['test_attenuation_db'] if path.exists()
                 else config.parse(self.runner.profile()).get('test_attenuation_db',0))
        config.number(value,'test_attenuation_db',0,30)
        return value

    def set_test_attenuation(self, body):
        with self.lock:
            if self.runner.running:
                raise ValueError('Stop tones before changing test attenuation.')
            value = body.get('test_attenuation_db')
            config.number(value,'test_attenuation_db',0,30)
            atomic(self.root/'test-level.json',json.dumps(dict(test_attenuation_db=value)))
            return dict(self.status(),message=f'Test attenuation saved: {value:g} dB. Used by the next Auto run; music levels are unchanged.')

    def selection(self):
        path = self.root/'selection.json'
        return (json.loads(path.read_text()) if path.exists()
                else config.selected_pairs(config.parse(self.runner.profile())))

    def snapshot(self, cfg):
        if self.demo:
            return
        selected, raw = control([('GetConfigFilePath', None), ('GetConfigJson', None)])
        document = json.loads(raw)
        check_safety(document)
        # Preserve the actual currently playing config, including its selected source.
        key = 'before-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        path = self.root/'baselines'/f'{key}.yml'
        atomic(path, yaml.safe_dump(document, sort_keys=False))
        record = self.write_preset(key, 'Before tuning · '+datetime.now().strftime('%b %d %H:%M'),
                                   path, kind='manual', original_path=selected,
                                   original_sha256=digest(selected))
        atomic(self.root/'recovery.json', json.dumps(record))

    def start(self, body):
        with self.lock:
            if self.runner.running or self.runner.status().get('audio_owned'):
                raise ValueError('Stop and restore the previous settings before starting another run.')
            if body.get('microphone_ready') is not True:
                raise ValueError('Place the EMM-6 at the listening position and confirm the microphone is ready.')
            cfg = config.parse(self.runner.profile())
            name = str(body.get('name', '')).strip()[:80]
            cfg.update(automatic=True, auto_apply=True, room_positions=1,
                       measurement_repeats=3, stereo_alignment=True, capture_baseline=True,
                       name=name or 'Auto · '+datetime.now().strftime('%b %d %H:%M'),
                       test_attenuation_db=body.get('test_attenuation_db',self.test_attenuation()),
                       excluded_drivers=[],
                       selected_pairs=body.get('selected_pairs', self.selection()))
            cfg = config.validate(cfg, hardware=not self.demo)
            self.snapshot(cfg)
            self.meter = None
            self.runner.start(profile=cfg)
            atomic(self.root/'test-level.json',json.dumps(dict(test_attenuation_db=cfg['test_attenuation_db'])))
            atomic(self.root/'selection.json', json.dumps(cfg['selected_pairs']))
            return self.status()

    def activate(self, identifier, restore=False):
        with self.lock:
            if self.runner.running:
                raise ValueError('Stop Auto mode before changing profiles.')
            rows = {r['id']: r for r in self.profiles()}
            if identifier not in rows:
                raise ValueError('Select a saved profile.')
            record = rows[identifier]
            path = Path(record['config_path'])
            if digest(path) != record['sha256']:
                raise ValueError('Saved profile changed on disk; it cannot be applied.')
            check_safety(yaml.safe_load(path.read_text()), limits=record['kind'] == 'tuned')
            cfg = config.parse(self.runner.profile())
            if not self.demo:
                with exclusive(self.root/'audio.lock'):
                    self._activate_hardware(cfg, record, restore)
            atomic(self.root/'active.json', json.dumps(record, indent=2))
            with self.runner.lock:
                self.runner.state = self.runner._load()
                self.runner.state.update(audio_owned=False, status='idle', message='Saved profile applied at −36 dB, muted. Resume music when ready.')
                self.runner.save()
            return self.status()

    def retry(self):
        """Repeat the stopped stage using its original immutable run settings."""
        with self.lock:
            self.runner.start('resume')
            return self.status()

    def _activate_hardware(self, cfg, record, restore):
        """Recall needs no microphone, but validates routing/guards and rolls back on failure."""
        backend = Hardware(cfg)
        path = Path(record['config_path'])
        # Measurement never edits the manual config. Re-select it when still intact;
        # otherwise use the immutable backup without writing into its shared directory.
        if restore and record['kind'] == 'manual' and record.get('original_path') == '/var/lib/octet-dsp/channel-dsp.yml':
            original = Path(record['original_path'])
            if original.exists() and digest(original) == record.get('original_sha256', record['sha256']):
                path = original
        check_safety(yaml.safe_load(path.read_text()), limits=record['kind'] == 'tuned')
        subprocess.run([cfg['camilla']['binary'], '--check', str(path)], check=True,
                       capture_output=True, text=True, timeout=15)
        statefile = Path(cfg['camilla']['statefile'])
        previous = statefile.read_text()
        selected = yaml.safe_load(previous) or {}
        backend.take_dac()
        try:
            selected.update(config_path=str(path), mute=[True]*5, volume=[-36., 0., 0., 0., 0.])
            atomic(statefile, yaml.safe_dump(selected))
            backend.systemctl('start')
            for _ in range(30):
                time.sleep(.1)
                try:
                    loaded, state = control([('GetConfigFilePath', None), ('GetState', None)])
                    if loaded == str(path) and state == 'Running':
                        backend.master(-36, True)
                        self.restart_producers(cfg)
                        return
                except OSError:
                    pass
            raise RuntimeError('CamillaDSP did not confirm the saved profile.')
        except BaseException:
            backend.systemctl('stop')
            atomic(statefile, previous)
            raise

    def restore(self):
        path = self.root/'recovery.json'
        if not path.exists():
            raise ValueError('No pre-tuning backup is available.')
        record = json.loads(path.read_text())
        return self.activate(record['id'], restore=record['kind'] == 'manual' and
                             record.get('original_path') == '/var/lib/octet-dsp/channel-dsp.yml')


def create_app(root=None, demo=False):
    controller = Controller(root or os.environ.get('OCTET_TUNE_DATA', '/var/lib/octet-autotune'), demo)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if controller.runner.running:
            controller.runner.mute()
            controller.runner.thread.join(10)

    app = FastAPI(lifespan=lifespan)
    app.state.controller = controller

    @app.get('/api/auto')
    def status():
        return controller.status()

    @app.get('/api/auto/profiles/{identifier}/measurements')
    def profile_measurements(identifier: str):
        try:
            return controller.measurements(identifier)
        except (ValueError, KeyError, OSError) as error:
            raise HTTPException(404, str(error)) from error

    @app.post('/api/auto/{action}')
    def action(action: str, body: dict):
        try:
            if action == 'start':
                return controller.start(body)
            if action == 'stop':
                controller.runner.mute()
                return controller.status()
            if action == 'restore':
                return controller.restore()
            if action == 'retry':
                return controller.retry()
            if action == 'test-level':
                return controller.set_test_attenuation(body)
            if action == 'select':
                return controller.activate(body.get('id'))
            raise ValueError('Unknown Auto mode action.')
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            raise HTTPException(400, str(error)) from error

    return app


if __name__ == '__main__':
    import uvicorn
    os.umask(0o027)
    uvicorn.run(create_app(), host='127.0.0.1', port=8766)
