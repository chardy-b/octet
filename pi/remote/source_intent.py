"""Small, fail-closed persistence primitive for requested input source.

This module stores intent only.  It deliberately has no knowledge of DSP, playback,
receivers, routing, or service installation.
"""

import fcntl
import json
import os
from pathlib import Path
import stat
import uuid


class SourceIntentError(RuntimeError):
    """The requested-source state is absent, unsafe, invalid, or not durable."""


class SourceIntent:
    MODES = frozenset(("auto", "tidal", "airplay", "bluetooth"))
    FILENAME = "source-intent.json"
    MAX_BYTES = 4096

    def __init__(self, directory, *, owner_uid=None):
        self.directory = Path(directory)
        self.owner_uid = os.geteuid() if owner_uid is None else owner_uid
        self.mode = None
        self.uncertain = False

    def _mark_uncertain(self):
        self.uncertain = True
        self.mode = None

    def _open_directory(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            fd = os.open(self.directory, flags)
            info = os.fstat(fd)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.owner_uid
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise SourceIntentError("source intent directory is not owner-only")
            return fd
        except Exception as error:
            if "fd" in locals():
                os.close(fd)
            if isinstance(error, SourceIntentError):
                raise
            raise SourceIntentError("source intent directory is missing or unsafe") from error

    @staticmethod
    def _unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result

    @classmethod
    def _decode(cls, raw):
        if len(raw) > cls.MAX_BYTES:
            raise SourceIntentError("source intent state is oversized")
        try:
            value = json.loads(raw, object_pairs_hook=cls._unique_pairs)
        except (UnicodeDecodeError, ValueError) as error:
            raise SourceIntentError("source intent state is corrupt") from error
        if (type(value) is not dict or set(value) != {"version", "mode"}
                or type(value["version"]) is not int or value["version"] != 1
                or type(value["mode"]) is not str or value["mode"] not in cls.MODES):
            raise SourceIntentError("source intent schema is invalid")
        return value["mode"]

    @classmethod
    def _payload(cls, mode):
        if type(mode) is not str or mode not in cls.MODES:
            raise SourceIntentError("invalid requested source mode")
        return json.dumps({"version": 1, "mode": mode}, separators=(",", ":")).encode()

    def _read_locked(self, directory_fd):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(self.FILENAME, flags, dir_fd=directory_fd)
        except OSError as error:
            raise SourceIntentError("source intent state is missing or unsafe") from error
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise SourceIntentError("source intent state is not an owner-only regular file")
            raw = bytearray()
            while len(raw) <= self.MAX_BYTES:
                chunk = os.read(fd, min(1024, self.MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            return self._decode(bytes(raw))
        except SourceIntentError:
            raise
        except OSError as error:
            raise SourceIntentError("source intent state could not be read") from error
        finally:
            os.close(fd)

    def load(self):
        """Perform the durable startup gate; never clear a process uncertainty latch."""
        if self.uncertain:
            raise SourceIntentError("source intent publication is uncertain; verification required")
        return self.verify_after_uncertainty()

    def verify_after_uncertainty(self):
        """Exclusively prove readable state and its directory entry are now durable."""
        directory_fd = self._open_directory()
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            first = self._read_locked(directory_fd)
            os.fsync(directory_fd)
            second = self._read_locked(directory_fd)
            if first != second:
                raise SourceIntentError("source intent changed during durable verification")
        except Exception as error:
            self._mark_uncertain()
            if isinstance(error, SourceIntentError):
                raise
            raise SourceIntentError("source intent durable verification failed") from error
        finally:
            os.close(directory_fd)
        self.mode = second
        self.uncertain = False
        return second

    @staticmethod
    def _write_all(fd, payload):
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("short source intent write")
            offset += written

    def _publish_locked(self, directory_fd, mode, *, require_missing=False):
        payload = self._payload(mode)
        temporary = ".source-intent-" + uuid.uuid4().hex
        temp_fd = None
        replaced = False
        try:
            if require_missing:
                try:
                    os.stat(self.FILENAME, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise SourceIntentError("source intent is already initialized")
            temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=directory_fd)
            os.fchmod(temp_fd, 0o600)
            self._write_all(temp_fd, payload)
            os.fsync(temp_fd)
            os.close(temp_fd)
            temp_fd = None
            os.replace(temporary, self.FILENAME,
                       src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            replaced = True
            os.fsync(directory_fd)
            if self._read_locked(directory_fd) != mode:
                raise SourceIntentError("source intent readback differs")
        except Exception as error:
            if replaced:
                self._mark_uncertain()
            if isinstance(error, SourceIntentError):
                raise
            raise SourceIntentError("source intent publication failed") from error
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if not replaced:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        self.mode = mode
        self.uncertain = False
        return mode

    def select(self, mode):
        """Serialize and durably replace a valid, existing requested-source state."""
        self._payload(mode)
        if self.uncertain:
            raise SourceIntentError("source intent publication is uncertain; verification required")
        try:
            directory_fd = self._open_directory()
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX)
                self._read_locked(directory_fd)  # selection never initializes missing state
                return self._publish_locked(directory_fd, mode)
            finally:
                os.close(directory_fd)
        except Exception:
            # Even a pre-publication failure invalidates this instance's snapshot.
            self.mode = None
            raise

    def initialize(self, mode, *, attended, server_stopped):
        """One-time initialization, guarded for an attended stopped-server migration."""
        if attended is not True or server_stopped is not True:
            raise SourceIntentError("initialization requires attended, server-stopped migration")
        self._payload(mode)
        directory_fd = self._open_directory()
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_EX)
            return self._publish_locked(directory_fd, mode, require_missing=True)
        finally:
            os.close(directory_fd)
