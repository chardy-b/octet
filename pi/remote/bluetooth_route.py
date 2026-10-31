"""Muted Bluetooth candidate preparation and durable recovery.

Caller holds server.LOCK. The inherited flock is retained until recovery finishes,
including failures; never transfer its ownership through a socket or unlink it.
"""
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
import time
import uuid

import yaml

from airplay_baseline import (BaselineRoute, MAX_CONFIG, MAX_JOURNAL, digest,
                              persist as replace_file, read_bounded, sync_directory)
from bluetooth_contract import routed


def persist(path, data, mode):
    """Publish a journal exclusively: never replace concurrent recovery evidence."""
    if path.suffix != '.json':
        return replace_file(path, data, mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.bluetooth-') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fdatasync(stream.fileno())
        os.link(stream.name, path, follow_symlinks=False)
        sync_directory(path.parent)


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
        airplay = self.server.AIRPLAY_CONTROL
        # CHANNEL_PROFILE casts have no journal: an unfinished rollback or mute
        # retry is AirPlay-owned DSP state just as much as an active cast.
        if (airplay.active or airplay.pending is not None or airplay.baseline.exists()
                or airplay.restore_pending is not None or airplay.needs_mute):
            raise RuntimeError('AirPlay owns audio; Bluetooth recovery retained')

    def read_record(self):
        # Re-read even with a cached record: replacement is not our generation.
        fd = os.open(self.journal, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeError('Invalid Bluetooth journal file')
            raw = stream.read(MAX_JOURNAL + 1)
            current = self.journal.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if (identity != (current.st_dev, current.st_ino)
                    or not stat.S_ISREG(current.st_mode)
                    or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    != (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                    or len(raw) > MAX_JOURNAL):
                raise RuntimeError('Bluetooth journal changed or oversized')
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise RuntimeError('Duplicate Bluetooth journal key')
                result[key] = value
            return result
        record = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(record, dict)
                or set(record) != {'version', 'original', 'sha256', 'generation'}
                or type(record['version']) is not int or record['version'] != 1
                or not isinstance(record['original'], str)
                or len(record['original']) > 4096
                or not isinstance(record['sha256'], str)
                or re.fullmatch(r'[0-9a-f]{64}', record['sha256']) is None
                or not isinstance(record['generation'], str)
                or re.fullmatch(r'[0-9a-f]{32}', record['generation']) is None):
            raise RuntimeError('Invalid Bluetooth recovery journal')
        if ((self.record is not None and record != self.record)
                or (getattr(self, '_journal_bytes', None) is not None
                    and raw != self._journal_bytes)
                or (getattr(self, '_journal_identity', None) is not None
                    and identity != self._journal_identity)):
            raise RuntimeError('Bluetooth journal generation or inode changed')
        self._journal_identity = identity
        self._journal_bytes = raw
        return record

    def original(self):
        self.acquire()
        record = self.read_record()
        path = Path(record['original'])
        if (path.parent != self.server.TUNING.root / 'baselines'
                or path.suffix != '.yml' or path.resolve() != path):
            raise RuntimeError('Bluetooth original is not an approved baseline path')
        data = read_bounded(path, MAX_CONFIG)
        if not self.server.TUNING.active(str(path)):
            raise RuntimeError('Bluetooth original is no longer the active profile; recovery retained')
        self.record = record
        return path, self.as_loaded(data)

    def begin(self, path, loaded):
        """Durably capture an approved baseline without changing any DSP path.

        This foundation is not a playback entry point. No bridge or candidate is
        started. Snapshot bytes are written once, read-only, and digest checked.
        """
        if self.exists():
            raise RuntimeError('Bluetooth recovery pending')
        self.acquire()
        try:
            if self.exists():
                raise RuntimeError('Bluetooth recovery appeared while acquiring lock')
            self._journal_bytes = None
            self._journal_identity = None
            self.ownership()
            self.mute()
            original = Path(path)
            if (original.parent != self.server.TUNING.root / 'baselines'
                    or original.suffix != '.yml' or original.resolve() != original):
                raise RuntimeError('Bluetooth requires an approved baseline path')
            data = read_bounded(original, MAX_CONFIG)
            if not self.server.TUNING.active(path) or self.as_loaded(data) != loaded:
                raise RuntimeError('Bluetooth requires the active profile to be loaded')
            # Validate the Tidal topology without claiming observed PCM or --check.
            routed(loaded, rate=48000, sample_format='S32_LE', channels=2)
            with self.server.DSP() as dsp:
                if (dsp.call('GetConfigFilePath') != path
                        or json.loads(dsp.call('GetConfigJson')) != loaded
                        or dsp.call('GetState') != 'Running'):
                    raise RuntimeError('DSP baseline changed')
            persist(self.snapshot, data, 0o400)
            self.record = dict(version=1, original=path, sha256=digest(data), generation=uuid.uuid4().hex)
            self.ownership()
            persist(self.journal, json.dumps(self.record).encode(), 0o600)
            self.original()
            return self.record['generation']
        except Exception:
            if not self.journal.exists() and not self.journal.is_symlink():
                self.record = None
                self.release()
            raise

    def prepare(self, rate, format):
        """Load a checked sub2 candidate while muted; retain evidence until recovery."""
        with self.server.LOCK:
            self.mute()
            self.acquire()
            try:
                self.ownership()
                with self.server.DSP() as dsp:
                    path = dsp.call('GetConfigFilePath')
                    loaded = json.loads(dsp.call('GetConfigJson'))
                expected = routed(loaded, rate=rate, sample_format=format, channels=2)
                self.begin(path, loaded)
                data = yaml.safe_dump(expected, sort_keys=False).encode()
                persist(self.candidate, data, 0o640)
                check = subprocess.run(
                    ['/opt/camilladsp/camilladsp', '--check', str(self.candidate)],
                    capture_output=True, timeout=10, shell=False)
                if check.returncode:
                    raise RuntimeError('DSP rejected the Bluetooth baseline route')
                self.verify()
                if read_bounded(self.candidate, MAX_CONFIG) != data:
                    raise RuntimeError('Bluetooth candidate changed during check')
                self.server.INPUT_LEVELS.release()
                self.verify()
                with self.server.DSP() as dsp:
                    if read_bounded(self.candidate, MAX_CONFIG) != data:
                        raise RuntimeError('Bluetooth candidate changed before path switch')
                    dsp.call('SetConfigFilePath', str(self.candidate))
                    try:
                        self.ownership()
                        self.original()
                        self.mute()
                        if read_bounded(self.candidate, MAX_CONFIG) != data:
                            raise RuntimeError('Bluetooth candidate changed before reload')
                        dsp.call('Reload')
                    finally:
                        self.mute()
                    for _ in range(20):
                        self.ownership()
                        self.original()
                        if read_bounded(self.candidate, MAX_CONFIG) != data:
                            raise RuntimeError('Bluetooth candidate changed after reload')
                        if dsp.call('GetMute') is not True:
                            raise RuntimeError('Bluetooth physical mute lost')
                        if (dsp.call('GetConfigFilePath') == str(self.candidate)
                                and json.loads(dsp.call('GetConfigJson')) == expected
                                and dsp.call('GetState') == 'Running'):
                            return expected
                        time.sleep(.1)
                raise RuntimeError('Bluetooth candidate load not verified; recovery retained')
            except BaseException:
                # Even external mute drift or an interrupted reload must fail muted.
                self.mute()
                raise
            finally:
                self.release_if_idle()

    def verify(self):
        self.ownership()
        path, config = self.original()
        with self.server.DSP() as dsp:
            if (dsp.call('GetConfigFilePath') != str(path)
                    or json.loads(dsp.call('GetConfigJson')) != config
                    or dsp.call('GetState') != 'Running'
                    or dsp.call('GetMute') is not True):
                raise RuntimeError('Bluetooth guarded baseline drifted')

    def recover(self, *, cleanup=True):
        # Mute is attempted even if the lock/approval is now unavailable.
        self.mute()
        self.acquire()
        self.ownership()
        path, config = self.original()
        with self.server.DSP() as dsp:
            self.mute()
            self.ownership()
            self.original()
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
                    if cleanup:
                        self.cleanup_journal()
                    return
                time.sleep(.1)
        raise RuntimeError('Bluetooth restore not verified; recovery retained')

    def cleanup_journal(self):
        # All writers must hold audio.lock in the trusted local state directory.
        # There is no POSIX conditional unlink; hostile directory writers cannot
        # be made safe by stat/unlink. They are outside this offline foundation.
        self.verify()
        current = self.journal.lstat()
        if (not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != self._journal_identity):
            raise RuntimeError('Bluetooth journal inode changed before cleanup')
        self.journal.unlink()
        try:
            sync_directory(self.root)
            self.ownership()
            if self.journal.exists() or self.journal.is_symlink():
                raise RuntimeError('Bluetooth journal appeared during cleanup')
        except Exception:
            # Recreate evidence without replacing any competing journal. Retain
            # record and flock even if this second durability attempt also fails.
            self._journal_identity = None
            persist(self.journal, self._journal_bytes, 0o600)
            raise
        self.record = None
        self._journal_identity = None
        self._journal_bytes = None
        self.release()
