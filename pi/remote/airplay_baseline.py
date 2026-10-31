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

from dsp_inputs import with_input

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
    """The running profile fed from AirPlay (any profile; see dsp_inputs)."""
    return with_input(config, 'airplay')


class BaselineRoute:
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

    def as_loaded(self, data):
        """The config as CamillaDSP reports it once loaded from these bytes.

        CamillaDSP fills in defaults (for example description: null on filters that
        omit it), so the file's own YAML never equals GetConfigJson for such files.
        ReadConfig parses the raw text exactly as a reload does; a Python YAML/JSON
        round trip first changes the last digit of some floats. Cached per digest:
        checks right after the unmute must not open a connection.
        """
        key = digest(data)
        cached = getattr(self, '_as_loaded', None)
        if cached is None or cached[0] != key:
            with self.server.DSP() as dsp:
                cached = (key, yaml.safe_load(dsp.call('ReadConfig', data.decode())))
            self._as_loaded = cached
        return copy.deepcopy(cached[1])

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
        # Journals from before profile + input may still carry a checksum; it is ignored.
        if (not isinstance(record, dict) or set(record) - {'sha256'} != {'version', 'original'}
                or record['version'] != 1
                or not isinstance(record['original'], str) or len(record['original']) > 4096):
            raise RuntimeError('Invalid AirPlay recovery journal')
        path = Path(record['original'])
        if path.suffix != '.yml' or path.resolve() != path:
            raise RuntimeError('AirPlay recovery path is not a saved profile')
        data = read_bounded(path, MAX_CONFIG)
        if not self.server.listening_profile(str(path)):
            raise RuntimeError('AirPlay recovery path is not a saved profile')
        return path, self.as_loaded(data)

    def ownership(self):
        self.check_lock()
        if self.server.TUNING.status().get('audio_owned') is not False:
            raise RuntimeError('Auto Tune owns audio or ownership is unknown; AirPlay recovery retained')

    def begin(self, path, loaded):
        if self.exists():
            raise RuntimeError('AirPlay recovery is still pending')
        self.acquire()
        try:
            if not self.server.listening_profile(path):
                raise RuntimeError('AirPlay requires a saved speaker profile')
            self.record = dict(version=1, original=path)
            original, config = self.original()
            if loaded != config:
                raise RuntimeError('Loaded DSP differs from the saved profile')
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
