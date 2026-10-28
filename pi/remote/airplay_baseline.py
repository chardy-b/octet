"""Durable, fail-closed AirPlay routing of an approved Auto Tune baseline."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time

import yaml

from audio_sources import source_metadata

ROOT = Path('/var/lib/octet-dsp')
MAX_CONFIG = 2 * 1024 * 1024
MAX_JOURNAL = 8192


def read_bounded(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError('AirPlay recovery requires regular files')
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError('AirPlay recovery file exceeds size limit')
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def persist(path, data, mode):
    if len(data) > (MAX_JOURNAL if path.suffix == '.json' else MAX_CONFIG):
        raise RuntimeError('AirPlay recovery file exceeds size limit')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.airplay-', delete=False) as stream:
            temporary = Path(stream.name)
            os.fchown(stream.fileno(), -1, path.parent.stat().st_gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fdatasync(stream.fileno())
        temporary.replace(path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def routed(config):
    # Narrow scope: only the verified stereo Tidal baseline. No mixer/pipeline edits.
    capture = config['devices']['capture']
    if (source_metadata(config)['id'] != 'tidal' or capture.get('channels') != 2
            or capture.get('type') != 'Alsa' or capture.get('device') != 'hw:Loopback,1,0'
            or config['devices']['samplerate'] != 48000):
        raise RuntimeError('AirPlay requires an approved stereo Tidal baseline')
    result = copy.deepcopy(config)
    result['devices']['capture'].update(device='hw:Loopback,1,1', format='S32_LE')
    result['devices'].update(resampler=dict(type='AsyncSinc', profile='Balanced'),
                             enable_rate_adjust=True, capture_samplerate=44100,
                             target_level=config['devices']['chunksize'], adjust_period=10)
    return result


def lexists(path):
    return path.exists() or path.is_symlink()


class BaselineRoute:
    # Every other owner's journal. Its presence is a conflict, never permission to
    # proceed: each route (AirPlay, Bluetooth, manual) lists all journals but its own.
    foreign_journals = ('bluetooth-recovery.json', 'manual-source-recovery.json')

    def __init__(self, server):
        self.server = server
        self.root = ROOT
        self.record = None
        self._lock_fd = None

    def acquire(self):
        """Lease the existing Auto Tune inode, without creating or writing it."""
        path = self.server.TUNING.root / 'audio.lock'
        if self._lock_fd is not None:
            self.check_lock()
            return
        fd = None
        try:
            before = path.lstat()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(before.st_mode)
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
                raise RuntimeError('AirPlay audio lock inode changed')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_fd = fd
            self.check_lock()
        except Exception as error:
            self._lock_fd = None
            if fd is not None:
                os.close(fd)
            raise RuntimeError('AirPlay audio lock unavailable or changed; another Octet process may own audio') from error

    def check_lock(self):
        if self._lock_fd is None:
            raise RuntimeError('AirPlay audio lock is not held')
        held = os.fstat(self._lock_fd)
        current = (self.server.TUNING.root / 'audio.lock').lstat()
        if (not stat.S_ISREG(current.st_mode)
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
            raise RuntimeError('AirPlay audio lock inode changed; recovery retained')

    def release(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def release_if_idle(self):
        if not self.exists():
            self.release()

    @property
    def journal(self):
        return self.root / 'airplay-recovery.json'

    @property
    def candidate(self):
        return self.root / 'airplay-baseline.yml'

    def exists(self):
        return self.record is not None or self.journal.exists() or self.journal.is_symlink()

    def original(self):
        self.acquire()
        if self.record is None:
            self.record = json.loads(read_bounded(self.journal, MAX_JOURNAL))
        record = self.record
        if (set(record) != {'version', 'original', 'sha256'} or record['version'] != 1
                or not isinstance(record['original'], str) or len(record['original']) > 4096):
            raise RuntimeError('Invalid AirPlay recovery journal')
        path = Path(record['original'])
        if (path.parent != self.server.TUNING.root / 'baselines' or path.suffix != '.yml'
                or path.resolve() != path):
            raise RuntimeError('AirPlay recovery path is not an Auto Tune baseline')
        data = read_bounded(path, MAX_CONFIG)
        approval = self.server.TUNING.active(str(path))
        if digest(data) != record['sha256'] or not approval or approval['sha256'] != record['sha256']:
            raise RuntimeError('AirPlay baseline approval or digest changed')
        return path, yaml.safe_load(data)

    def refuse_foreign(self):
        owners = {'manual-source-recovery.json': 'A manual source route', 'bluetooth-recovery.json': 'Bluetooth',
                  'airplay-recovery.json': 'AirPlay'}
        for name in self.foreign_journals:
            if lexists(self.root / name):
                raise RuntimeError(f'{owners[name]} owns audio recovery evidence; recovery retained')

    def ownership(self):
        self.check_lock()
        if self.server.TUNING.status().get('audio_owned') is not False:
            raise RuntimeError('Auto Tune owns audio or ownership is unknown; AirPlay recovery retained')
        self.refuse_foreign()

    def begin(self, path, loaded):
        if self.exists():
            raise RuntimeError('AirPlay recovery is still pending')
        self.refuse_foreign()
        self.acquire()
        try:
            approval = self.server.TUNING.active(path)
            if not approval:
                raise RuntimeError('AirPlay baseline is not approved')
            self.record = dict(version=1, original=path, sha256=approval['sha256'])
            original, config = self.original()
            if loaded != config:
                raise RuntimeError('Loaded DSP differs from the approved baseline')
            expected = routed(loaded)
            persist(self.candidate, yaml.safe_dump(expected, sort_keys=False).encode(), 0o640)
            check = subprocess.run(['/opt/camilladsp/camilladsp', '--check', str(self.candidate)],
                                   capture_output=True, timeout=10)
            if check.returncode:
                raise RuntimeError('DSP rejected the AirPlay baseline route')
            self.ownership()
            self.original()
            with self.server.DSP() as dsp:
                if dsp.call('GetConfigFilePath') != path or json.loads(dsp.call('GetConfigJson')) != loaded:
                    raise RuntimeError('DSP changed during AirPlay preparation')
            # No path mutation until BOTH file data and directory entry are durable.
            persist(self.journal, json.dumps(self.record).encode(), 0o600)
        except Exception:
            if not self.journal.exists() and not self.journal.is_symlink():
                self.record = None
                try:
                    self.candidate.unlink(missing_ok=True)
                finally:
                    self.release()
            raise
        self.server.INPUT_LEVELS.release()
        self.ownership()
        with self.server.DSP() as dsp:
            dsp.call('SetConfigFilePath', str(self.candidate))
            try:
                dsp.call('Reload')
            finally:
                dsp.call('SetMute', True)
        return expected

    def ready(self, dsp, loaded, expected):
        self.ownership()
        self.original()
        return (dsp.call('GetConfigFilePath') == str(self.candidate)
                and dsp.call('GetState') == 'Running'
                and loaded == expected
                and yaml.safe_load(read_bounded(self.candidate, MAX_CONFIG)) == expected)

    def recover(self):
        self.refuse_foreign()  # before taking the lease: a refused owner must not hold it
        self.acquire()
        self.ownership()
        original, config = self.original()
        with self.server.DSP() as dsp:
            current = dsp.call('GetConfigFilePath')
            loaded = json.loads(dsp.call('GetConfigJson'))
            if current not in (str(original), str(self.candidate)):
                raise RuntimeError('DSP ownership changed; AirPlay recovery retained')
            if current == str(original) and loaded not in (config, routed(config)):
                raise RuntimeError('DSP config changed; AirPlay recovery retained')
            self.ownership()
            dsp.call('SetConfigFilePath', str(original))
            try:
                dsp.call('Reload')
            finally:
                dsp.call('SetMute', True)
            for _ in range(20):
                self.ownership()
                self.original()
                if (dsp.call('GetConfigFilePath') == str(original)
                        and json.loads(dsp.call('GetConfigJson')) == config
                        and dsp.call('GetState') == 'Running'):
                    self.server.ACTIVE_CAPTURE_CHANNELS = (0, 1)
                    # Journal last: a crash during cleanup still has recovery evidence.
                    self.candidate.unlink(missing_ok=True)
                    sync_directory(self.root)
                    self.journal.unlink(missing_ok=True)
                    sync_directory(self.root)
                    self.record = None
                    self.release()
                    return
                time.sleep(.1)
        raise RuntimeError('AirPlay baseline restore not verified; recovery retained')
