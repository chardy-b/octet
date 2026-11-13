"""Shared-lease, durable rollback for ordinary input/profile changes.

Receiver routes keep their existing journals. This transaction covers the third
writer: Music-page input changes and profile changes on Tidal/USB.
"""
import json
import os
from pathlib import Path
import stat
import time

from airplay_baseline import (BaselineRoute, MAX_CONFIG, MAX_JOURNAL, digest,
                              persist, read_bounded, sync_directory)
from audio_sources import meter_channels


def provision_audio_lock(path, uid, gid):
    """Installer-only: create the stable shared inode once; never replace it."""
    path = Path(path)
    try:
        path.parent.mkdir(mode=0o750, parents=True)
    except FileExistsError:
        pass
    else:
        os.chown(path.parent, uid, gid)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    except FileExistsError:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError('Shared audio lock must be a regular file')
        return
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o640)
        os.write(fd, b'0')
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(path.parent)


class SourceRoute(BaselineRoute):
    def __init__(self, server):
        super().__init__(server)
        self.root = server.CHANNEL_PROFILE.parent

    @property
    def journal(self):
        return self.root / 'source-recovery.json'

    def begin_change(self, path, loaded, target):
        self.ownership()
        if self.exists():
            raise RuntimeError('Previous input recovery is still pending')
        if not self.server.listening_profile(path):
            raise RuntimeError('Input recovery requires a saved speaker profile')
        snapshots = {}
        for name, original in (('running', self.server.running_file()),
                               ('record', self.server.running_record())):
            data = read_bounded(original, MAX_CONFIG if name == 'running' else MAX_JOURNAL) if original.exists() else None
            backup = self.root / ('source-previous.yml' if name == 'running' else 'source-previous.json')
            if data is not None:
                persist(backup, data, 0o600)
            snapshots[name] = digest(data) if data is not None else None
        # Snapshot exactly what Camilla loaded, including its default values.
        data = json.dumps(loaded).encode()
        persist(self.root / 'source-loaded.yml', data, 0o600)
        record = dict(version=1, original=path, target=str(target), loaded=digest(data), **snapshots)
        persist(self.journal, json.dumps(record).encode(), 0o600)
        self.record = record

    def finish(self):
        # Keep evidence if removing the directory entry cannot be made durable.
        saved = read_bounded(self.journal, MAX_JOURNAL)
        try:
            self.journal.unlink(missing_ok=True)
            sync_directory(self.root)
        except Exception:
            self.record = json.loads(saved)
            persist(self.journal, saved, 0o600)
            raise
        self.record = None

    def recover(self):
        if not self.exists():
            return
        self.acquire()
        try:
            self.ownership()
            record = json.loads(read_bounded(self.journal, MAX_JOURNAL))
            if (not isinstance(record, dict) or set(record) !=
                    {'version', 'original', 'target', 'loaded', 'running', 'record'}
                    or type(record['version']) is not int or record['version'] != 1):
                raise RuntimeError('Invalid input recovery journal; recovery retained')
            path = record['original']
            if not isinstance(path, str) or Path(path).suffix != '.yml' or str(Path(path).resolve()) != path:
                raise RuntimeError('Invalid input recovery path')
            loaded = read_bounded(self.root / 'source-loaded.yml', MAX_CONFIG)
            if digest(loaded) != record['loaded']:
                raise RuntimeError('Input recovery snapshot changed')
            expected = json.loads(loaded)
            # Validate all snapshots before replacing any live file.
            snapshots = []
            for name, original in (('running', self.server.running_file()),
                                   ('record', self.server.running_record())):
                backup = self.root / ('source-previous.yml' if name == 'running' else 'source-previous.json')
                data = None if record[name] is None else read_bounded(backup, MAX_CONFIG if name == 'running' else MAX_JOURNAL)
                if data is not None and digest(data) != record[name]:
                    raise RuntimeError('Input recovery files changed')
                snapshots.append((original, data))
            with self.server.DSP() as dsp:
                if dsp.call('GetConfigFilePath') not in (path, record['target']):
                    raise RuntimeError('DSP ownership changed; input recovery retained')
            for original, data in snapshots:
                if data is None:
                    original.unlink(missing_ok=True)
                    sync_directory(self.root)
                else:
                    persist(original, data, 0o640)
            if not self.server.listening_profile(path):
                raise RuntimeError('Input recovery profile is no longer approved')
            self.ownership()
            with self.server.DSP() as dsp:
                dsp.call('SetConfigFilePath', path)
                dsp.call('Reload')
                for _ in range(60):
                    self.check_lock()
                    if (dsp.call('GetConfigFilePath') == path
                            and json.loads(dsp.call('GetConfigJson')) == expected
                            and dsp.call('GetState') == 'Running'):
                        self.server.ACTIVE_CAPTURE_CHANNELS = meter_channels(expected)
                        self.finish()
                        return
                    time.sleep(.1)
            raise RuntimeError('Previous input restore not verified; recovery retained')
        finally:
            self.release_if_idle()
