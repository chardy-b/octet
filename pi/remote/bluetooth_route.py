"""Bluetooth recovery foundation; deliberately no candidate activation API.

Caller holds server.LOCK. The inherited flock is retained until recovery finishes,
including failures; never transfer its ownership through a socket or unlink it.
"""
import json
from pathlib import Path
import time
import uuid

import yaml

from airplay_baseline import (BaselineRoute, MAX_CONFIG, MAX_JOURNAL, digest,
                              persist, read_bounded, sync_directory)
from bluetooth_contract import routed


class BluetoothRoute(BaselineRoute):
    @property
    def journal(self):
        return self.root / 'bluetooth-recovery.json'

    @property
    def candidate(self):
        return self.root / 'bluetooth-baseline.yml'

    @property
    def snapshot(self):
        return self.root / 'bluetooth-original.yml'

    def mute(self):
        with self.server.DSP() as dsp:
            dsp.call('SetMute', True)
            if dsp.call('GetMute') is not True:
                raise RuntimeError('Bluetooth physical mute not verified')

    def ownership(self):
        super().ownership()
        airplay = self.server.AIRPLAY
        if airplay.active or airplay.pending is not None or airplay.baseline.exists():
            raise RuntimeError('AirPlay owns audio; Bluetooth recovery retained')

    def original(self):
        self.acquire()
        record = json.loads(read_bounded(self.journal, MAX_JOURNAL)) if self.record is None else self.record
        if (set(record) != {'version', 'original', 'sha256', 'generation'}
                or type(record['version']) is not int or record['version'] != 1
                or not isinstance(record['original'], str)
                or not isinstance(record['generation'], str)
                or len(record['generation']) != 32):
            raise RuntimeError('Invalid Bluetooth recovery journal')
        path = Path(record['original'])
        if (path.parent != self.server.TUNING.root / 'baselines'
                or path.suffix != '.yml' or path.resolve() != path):
            raise RuntimeError('Bluetooth original is not an approved baseline path')
        data = read_bounded(self.snapshot, MAX_CONFIG)
        approval = self.server.TUNING.active(str(path))
        if (digest(data) != record['sha256'] or read_bounded(path, MAX_CONFIG) != data
                or not approval or approval['sha256'] != record['sha256']):
            raise RuntimeError('Bluetooth original bytes or approval changed; recovery retained')
        self.record = record
        return path, yaml.safe_load(data)

    def begin(self, path, loaded):
        """Durably capture an approved baseline without changing any DSP path.

        This foundation is not a playback entry point. No bridge or candidate is
        started. Snapshot bytes are written once, read-only, and digest checked.
        """
        if self.exists():
            raise RuntimeError('Bluetooth recovery pending')
        self.acquire()
        try:
            self.ownership()
            self.mute()
            original = Path(path)
            if (original.parent != self.server.TUNING.root / 'baselines'
                    or original.suffix != '.yml' or original.resolve() != original):
                raise RuntimeError('Bluetooth requires an approved baseline path')
            data = read_bounded(original, MAX_CONFIG)
            approval = self.server.TUNING.active(path)
            if not approval or approval['sha256'] != digest(data) or yaml.safe_load(data) != loaded:
                raise RuntimeError('Bluetooth requires exact approved active baseline')
            # Validate the Tidal topology without claiming observed PCM or --check.
            routed(loaded, rate=48000, sample_format='S32_LE', channels=2)
            if len(loaded.get('filters', {})) != 129:
                raise RuntimeError('Bluetooth requires the 129-filter baseline')
            with self.server.DSP() as dsp:
                if (dsp.call('GetConfigFilePath') != path
                        or json.loads(dsp.call('GetConfigJson')) != loaded
                        or dsp.call('GetState') != 'Running'):
                    raise RuntimeError('DSP baseline changed')
            persist(self.snapshot, data, 0o400)
            self.record = dict(version=1, original=path, sha256=digest(data), generation=uuid.uuid4().hex)
            self.ownership()
            persist(self.journal, json.dumps(self.record).encode(), 0o600)
            return self.record['generation']
        except Exception:
            if not self.journal.exists() and not self.journal.is_symlink():
                self.record = None
                self.release()
            raise

    def verify(self):
        self.ownership()
        path, config = self.original()
        with self.server.DSP() as dsp:
            if (dsp.call('GetConfigFilePath') != str(path)
                    or json.loads(dsp.call('GetConfigJson')) != config
                    or dsp.call('GetState') != 'Running'
                    or dsp.call('GetMute') is not True):
                raise RuntimeError('Bluetooth guarded baseline drifted')

    def recover(self):
        # Mute is attempted even if the lock/approval is now unavailable.
        self.mute()
        self.acquire()
        self.ownership()
        path, config = self.original()
        with self.server.DSP() as dsp:
            self.mute()
            self.ownership()
            dsp.call('SetConfigFilePath', str(path))
            try:
                dsp.call('Reload')
            finally:
                self.mute()
            for _ in range(20):
                self.ownership()
                self.original()
                if (dsp.call('GetConfigFilePath') == str(path)
                        and json.loads(dsp.call('GetConfigJson')) == config
                        and dsp.call('GetState') == 'Running'
                        and dsp.call('GetMute') is True):
                    # Retain immutable snapshot; journal is the last recovery marker.
                    self.journal.unlink()
                    sync_directory(self.root)
                    self.record = None
                    self.release()
                    return
                time.sleep(.1)
        raise RuntimeError('Bluetooth restore not verified; recovery retained')
