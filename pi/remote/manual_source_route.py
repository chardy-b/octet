"""Durable, fail-closed manual source routing on an approved Auto Tune baseline.

Selecting an input never replaces the approved speaker chain. A candidate is derived
from the *original* baseline bytes, differs only by the allowlisted input mixer and
capture/clock fields (audio_sources.verify_output_equivalence), is checked by
CamillaDSP, journaled durably, and only then selected with SetConfigFilePath + Reload
while muted. The approved baseline file is never written. Each switch derives from the
original, never from a previous candidate, so input stages cannot accumulate.

Ownership. Caller holds server.LOCK; the cross-process audio.lock lease is held from
begin until recovery completes, including failures. `manual-source-recovery.json` is
one of three journals (airplay-recovery.json, bluetooth-recovery.json). Mutual
exclusion is deterministic: a manual journal beside any other journal, an active
AirPlay/Bluetooth owner, an Auto Tune owner or an unknown owner is a conflict. A
conflict keeps output muted, retains every journal and candidate file, and refuses
recovery and controls; one owner never rewrites another's route. Startup order
(server.initialize_dsp): mute, manual recovery, AirPlay recovery, Bluetooth recovery.
AirPlay and Bluetooth refuse while this journal exists, so their recovery after a
manual conflict is refused rather than reordered.

Recovery always returns to the original approved baseline (Tidal), muted, and does
not survive as a persistent route: restart restores the baseline. It deletes the
journal only after the original path, the exact original config, a running DSP and
physical mute are verified. CamillaDSP has no compare-and-set, so a non-cooperative
websocket writer is detected by readback, not prevented.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import uuid

import yaml

from airplay_baseline import (BaselineRoute, MAX_CONFIG, MAX_JOURNAL, digest, lexists,
                              persist, read_bounded, sync_directory)
from audio_sources import (meter_channels, require_tidal_baseline, source_config,
                           usb_capture_ready, validate_source, verify_output_equivalence)

JOURNAL = 'manual-source-recovery.json'
CANDIDATE = 'manual-source-{}.yml'
ORPHAN = re.compile(r'manual-source-[0-9a-f]{32}\.yml')
GENERATION = re.compile(r'[0-9a-f]{32}')
SOURCE_ID = re.compile(r'airplay|usb:[A-Za-z0-9_]+:(?:pair|mono)-[0-9]{1,2}')
KEYS = {'version', 'original', 'sha256', 'generation', 'previous', 'source'}
VERIFY_ATTEMPTS = 60


def publish(path, data, mode):
    """Create a journal exclusively: never replace another owner's evidence."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.manual-') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fdatasync(stream.fileno())
        os.link(stream.name, path, follow_symlinks=False)
    sync_directory(path.parent)


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError('Duplicate manual source journal key')
        result[key] = value
    return result


class ManualSourceRoute(BaselineRoute):
    foreign_journals = ('airplay-recovery.json', 'bluetooth-recovery.json')

    def __init__(self, server):
        super().__init__(server)
        self.blocked = None       # specific reason while recovery is pending/unverified
        self.expected = None      # exact config this process loaded
        self.source = None
        self._journal_bytes = None
        self._journal_identity = None
        self._parsed = None

    @property
    def journal(self):
        return self.root / JOURNAL

    def candidate_for(self, generation):
        return self.root / CANDIDATE.format(generation)

    def candidates(self, record):
        return [self.candidate_for(g) for g in (record['generation'], record['previous']) if g]

    # --- ownership ------------------------------------------------------------
    def acquire(self):
        try:
            super().acquire()
        except RuntimeError as error:
            raise RuntimeError('Source routing audio lock unavailable or changed; '
                               'Auto Tune or another Octet process may own audio') from error

    def ownership(self):
        try:
            self.check_lock()
        except RuntimeError as error:
            raise RuntimeError('Source routing audio lock is not held or changed; recovery retained') from error
        if self.server.TUNING.status().get('audio_owned') is not False:
            raise RuntimeError('Auto Tune owns audio or ownership is unknown; source routing recovery retained')
        if any(lexists(self.root / name) for name in self.foreign_journals):
            raise RuntimeError('AirPlay or Bluetooth recovery evidence exists; manual source recovery retained')
        airplay = getattr(self.server, 'AIRPLAY_CONTROL', None)
        # needs_mute is deliberately not checked: it is latched until the AirPlay
        # recovery that runs *after* this route's startup recovery.
        if airplay is not None and (airplay.active or airplay.pending is not None
                                    or airplay.baseline.exists() or airplay.restore_pending is not None):
            raise RuntimeError('AirPlay owns audio; manual source recovery retained')
        bluetooth = getattr(self.server, 'BLUETOOTH_CONTROL', None)
        if bluetooth is not None and bluetooth.owns_audio():
            raise RuntimeError('Bluetooth owns audio; manual source recovery retained')

    def mute(self):
        with self.server.DSP() as dsp:
            dsp.call('SetMute', True)
            if dsp.call('GetMute') is not True:
                raise RuntimeError('Manual source physical mute not verified')

    # --- journal --------------------------------------------------------------
    def read_record(self):
        fd = os.open(self.journal, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise RuntimeError('Invalid manual source journal file')
            raw = stream.read(MAX_JOURNAL + 1)
            current = self.journal.lstat()
            identity = (opened.st_dev, opened.st_ino)
            if (identity != (current.st_dev, current.st_ino) or not stat.S_ISREG(current.st_mode)
                    or len(raw) > MAX_JOURNAL):
                raise RuntimeError('Manual source journal changed or oversized')
        try:
            record = json.loads(raw, object_pairs_hook=unique)
        except ValueError as error:
            raise RuntimeError('Invalid manual source journal') from error
        if (not isinstance(record, dict) or set(record) != KEYS
                or type(record['version']) is not int or record['version'] != 1
                or not isinstance(record['original'], str) or len(record['original']) > 4096
                or not isinstance(record['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
                or not isinstance(record['generation'], str) or not GENERATION.fullmatch(record['generation'])
                or not (record['previous'] is None or (isinstance(record['previous'], str)
                                                      and GENERATION.fullmatch(record['previous'])))
                or record['previous'] == record['generation']
                or not isinstance(record['source'], str) or not SOURCE_ID.fullmatch(record['source'])):
            raise RuntimeError('Invalid manual source journal')
        if self._journal_bytes is not None and (raw != self._journal_bytes
                                                or identity != self._journal_identity):
            raise RuntimeError('Manual source journal generation or inode changed')
        self._journal_bytes, self._journal_identity = raw, identity
        return record

    def forget_journal(self):
        self.record = self._journal_bytes = self._journal_identity = None

    def commit(self, record, *, replace):
        """Durably publish a record, then re-read it as the only trusted copy."""
        data = json.dumps(record).encode()
        if replace:
            self.read_record()  # refuse if anything but our own journal is there
            persist(self.journal, data, 0o600)
            self._journal_bytes = self._journal_identity = None
        else:
            publish(self.journal, data, 0o600)
        stored = self.read_record()
        if stored != record:
            raise RuntimeError('Manual source journal readback differs')
        self.record = stored

    def original(self):
        self.acquire()
        record = self.read_record()
        path = Path(record['original'])
        if (path.parent != self.server.TUNING.root / 'baselines' or path.suffix != '.yml'
                or path.resolve() != path):
            raise RuntimeError('Manual source original is not an approved baseline path')
        data = read_bounded(path, MAX_CONFIG)
        approval = self.server.TUNING.active(str(path))
        if digest(data) != record['sha256'] or not approval or approval['sha256'] != record['sha256']:
            raise RuntimeError('Manual source baseline approval or digest changed; recovery retained')
        self.record = record
        if self._parsed is None or self._parsed[0] != record['sha256']:
            self._parsed = (record['sha256'], yaml.safe_load(data))
        return path, copy.deepcopy(self._parsed[1])

    def approved_baseline(self, path, loaded):
        """The exact approved file must equal the loaded config; returns (bytes, config)."""
        original = Path(path)
        if (original.parent != self.server.TUNING.root / 'baselines' or original.suffix != '.yml'
                or original.resolve() != original):
            raise RuntimeError('Source routing requires an approved Auto Tune baseline')
        data = read_bounded(original, MAX_CONFIG)
        approval = self.server.TUNING.active(path)
        config = yaml.safe_load(data)
        if not approval or approval['sha256'] != digest(data) or config != loaded:
            raise RuntimeError('Source routing requires the exact approved baseline to be loaded')
        return data, config

    # --- live state ---------------------------------------------------------------
    def approves(self, dsp, path):
        """True only for this process's journaled candidate with exact loaded content."""
        try:
            record = self.record
            if (record is None or self.blocked or self.expected is None
                    or path != str(self.candidate_for(record['generation']))):
                return False
            self.ownership()
            _, config = self.original()
            verify_output_equivalence(config, json.loads(dsp.call('GetConfigJson')), self.source)
            return (yaml.safe_load(read_bounded(self.candidate_for(record['generation']), MAX_CONFIG))
                    == self.expected and dsp.call('GetState') == 'Running')
        except Exception:
            return False

    def approval(self, dsp, path):
        """The original baseline's approval record while its verified candidate is loaded."""
        if not self.approves(dsp, path):
            return None
        return self.server.TUNING.active(self.record['original'])

    def status(self):
        record = self.record
        return dict(active=record is not None and not self.blocked,
                    recovery_pending=bool(self.blocked), source=record['source'] if record else None,
                    error=self.blocked)

    # --- selection ----------------------------------------------------------------
    def select(self, source):
        """Verified no-op, begin, switch or recover. Returns the loaded config or None."""
        validate_source(source)
        self.acquire()
        try:
            return self._select(source)
        finally:
            self.release_if_idle()

    def _read_dsp(self):
        with self.server.DSP() as dsp:
            return (dsp.call('GetConfigFilePath'), json.loads(dsp.call('GetConfigJson')),
                    dsp.call('GetState'))

    def _select(self, source):
        path, loaded, state = self._read_dsp()
        if self.exists():
            with self.server.DSP() as dsp:
                live = self.approves(dsp, path)
            if live and self.record['source'] == source['id']:
                return None  # verified no-op; never an implicit unmute
            if live and source['id'] != 'tidal':
                return self.switch(source)
            self.recover()
            if source['id'] == 'tidal':
                return None
            path, loaded, state = self._read_dsp()
        self.ownership()
        data, config = self.approved_baseline(path, loaded)
        require_tidal_baseline(config)
        if state != 'Running':
            raise RuntimeError('The speaker DSP is not running')
        if source['id'] == 'tidal':
            return None
        return self.begin(path, loaded, source)

    def prepare_candidate(self, config, source):
        expected = source_config(config, source)
        verify_output_equivalence(config, expected, source)
        generation = uuid.uuid4().hex
        candidate = self.candidate_for(generation)
        blob = yaml.safe_dump(expected, sort_keys=False).encode()
        persist(candidate, blob, 0o640)
        try:
            check = subprocess.run(['/opt/camilladsp/camilladsp', '--check', str(candidate)],
                                   capture_output=True, timeout=10)
            if check.returncode:
                raise RuntimeError('DSP rejected the source; the previous source was retained')
            if not read_bytes_equal(candidate, blob):
                raise RuntimeError('Manual source candidate changed during check')
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        return expected, generation, candidate, blob

    def begin(self, path, loaded, source):
        if self.exists():
            raise RuntimeError('Manual source recovery is still pending')
        self.acquire()
        try:
            self.ownership()
            self.mute()
            data, config = self.approved_baseline(path, loaded)
            require_tidal_baseline(config)
            self.sweep_orphans()
            expected, generation, candidate, blob = self.prepare_candidate(config, source)
            self.ownership()
            self.approved_baseline(path, loaded)
            with self.server.DSP() as dsp:
                if (dsp.call('GetConfigFilePath') != path or json.loads(dsp.call('GetConfigJson')) != loaded
                        or dsp.call('GetState') != 'Running'):
                    raise RuntimeError('DSP changed during source preparation')
            # Retain the queue and position, but never play Tidal in the background.
            self.server.mpd('pause 1')
            record = dict(version=1, original=path, sha256=digest(data), generation=generation,
                          previous=None, source=source['id'])
            # No path mutation until BOTH file data and directory entry are durable.
            self.commit(record, replace=False)
        except Exception:
            if lexists(self.journal):
                # Evidence exists but no path was switched: retry recovery, never reuse.
                self.blocked = 'Source routing recovery is pending'
            else:
                self.forget_journal()
                self.sweep_orphans()
                self.release()
            raise
        return self.load(source, expected, candidate, blob)

    def switch(self, source):
        """Replace the active candidate: new generation from the ORIGINAL, muted."""
        self.ownership()
        _, config = self.original()
        self.mute()
        old = self.record
        expected, generation, candidate, blob = self.prepare_candidate(config, source)
        try:
            self.ownership()
            self.server.mpd('pause 1')
            with self.server.DSP() as dsp:
                if not self.approves(dsp, str(self.candidate_for(old['generation']))):
                    raise RuntimeError('The loaded source route is not the journaled candidate')
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        record = dict(old, generation=generation, previous=old['generation'], source=source['id'])
        try:
            self.commit(record, replace=True)
        except Exception as error:
            candidate.unlink(missing_ok=True)
            self.fail(error)
        loaded = self.load(source, expected, candidate, blob)
        try:
            # Retire the previous candidate only after the new one is verified.
            self.commit(dict(record, previous=None), replace=True)
            self.candidate_for(old['generation']).unlink(missing_ok=True)
            sync_directory(self.root)
        except Exception:
            pass  # evidence only widens the set of paths recovery accepts
        return loaded

    def load(self, source, expected, candidate, blob):
        """Select the journaled candidate muted; any failure restores the baseline."""
        self.expected, self.source = expected, source
        self.server.INPUT_LEVELS.release()
        try:
            self.ownership()
            self.original()
            self.mute()
            with self.server.DSP() as dsp:
                if not read_bytes_equal(candidate, blob):
                    raise RuntimeError('Manual source candidate changed before path switch')
                dsp.call('SetConfigFilePath', str(candidate))
                try:
                    self.ownership()
                    self.original()
                    self.mute()
                    if not read_bytes_equal(candidate, blob):
                        raise RuntimeError('Manual source candidate changed before reload')
                    dsp.call('Reload')
                finally:
                    self.mute()
                unready = None
                for _ in range(VERIFY_ATTEMPTS):
                    self.ownership()
                    _, config = self.original()
                    if not read_bytes_equal(candidate, blob):
                        raise RuntimeError('Manual source candidate changed after reload')
                    if dsp.call('GetMute') is not True:
                        raise RuntimeError('Manual source physical mute lost')
                    if (dsp.call('GetConfigFilePath') == str(candidate) and dsp.call('GetState') == 'Running'
                            and equivalent(config, json.loads(dsp.call('GetConfigJson')), source)):
                        try:
                            if source['id'].startswith('usb:'):
                                # Running DSP and --check do not prove the physical capture
                                # substream runs: read it (never open it) while Camilla owns it.
                                usb_capture_ready(source, expected['devices']['samplerate'])
                        except RuntimeError as error:
                            unready = str(error)
                        else:
                            if dsp.call('GetMute') is not True:  # last readback before success
                                raise RuntimeError('Manual source physical mute lost')
                            self.blocked = None
                            self.server.ACTIVE_CAPTURE_CHANNELS = meter_channels(expected)
                            return expected
                    time.sleep(.1)
            raise RuntimeError('The selected input did not start' + (f': {unready}' if unready else ''))
        except Exception as error:
            self.fail(error)

    def fail(self, error):
        """After the journal exists: restore the original muted, or retain everything."""
        self.blocked = 'Source change failed; recovery pending'
        try:
            self.recover()
        except Exception as recovery:
            raise RuntimeError('Source change failed and the original baseline could not be '
                               'verified; output is muted and recovery evidence is retained') from recovery
        raise RuntimeError(f'Source change failed ({error}); the original baseline was restored and output is muted') from error

    # --- recovery -----------------------------------------------------------------
    def startup(self):
        """First step of server.initialize_dsp recovery, before AirPlay and Bluetooth."""
        if self.exists():
            self.recover()

    def recover(self):
        try:
            self._recover()
        except Exception as error:
            self.blocked = f'Source routing recovery is pending: {error}'
            raise
        self.blocked = None

    def _recover(self):
        # Mute is attempted even if the lease or approval is now unavailable.
        self.mute()
        self.acquire()
        self.ownership()
        path, config = self.original()
        record = self.record
        with self.server.DSP() as dsp:
            current = dsp.call('GetConfigFilePath')
            loaded = json.loads(dsp.call('GetConfigJson'))
            if current not in {str(path), *map(str, self.candidates(record))}:
                raise RuntimeError('DSP ownership changed; manual source recovery retained')
            if current == str(path):
                # Under the original path only the original or one of our own candidates
                # may be loaded. Under a candidate path the original is the safe target
                # whatever was loaded, exactly as for AirPlay.
                try:
                    verify_output_equivalence(config, loaded, None)
                except RuntimeError as error:
                    raise RuntimeError('DSP config changed; manual source recovery retained') from error
            self.mute()
            self.ownership()
            self.original()
            dsp.call('SetConfigFilePath', str(path))
            try:
                dsp.call('Reload')
            finally:
                self.mute()
            for _ in range(VERIFY_ATTEMPTS):
                self.ownership()
                self.original()
                if (dsp.call('GetConfigFilePath') == str(path)
                        and json.loads(dsp.call('GetConfigJson')) == config
                        and dsp.call('GetState') == 'Running'
                        and dsp.call('GetMute') is True):
                    self.server.ACTIVE_CAPTURE_CHANNELS = (0, 1)
                    self.cleanup(record)
                    return
                time.sleep(.1)
        raise RuntimeError('Manual source restore not verified; recovery retained')

    def cleanup(self, record):
        # Journal last: a crash during cleanup still leaves recovery evidence.
        self.ownership()
        for candidate in self.candidates(record):
            candidate.unlink(missing_ok=True)
        sync_directory(self.root)
        current = self.journal.lstat()
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != self._journal_identity:
            raise RuntimeError('Manual source journal inode changed before cleanup')
        retained = self._journal_bytes
        self.journal.unlink()
        try:
            sync_directory(self.root)
            if lexists(self.journal):
                raise RuntimeError('A journal appeared during manual source cleanup')
        except Exception:
            # Recreate evidence without replacing any competing journal.
            self._journal_identity = None
            publish(self.journal, retained, 0o600)
            raise
        self.forget_journal()
        self.expected = self.source = None
        self.release()

    def sweep_orphans(self):
        """No journal means no candidate was ever selected: remove unjournaled leftovers."""
        for stale in self.root.glob('manual-source-*.yml'):
            if ORPHAN.fullmatch(stale.name) and stale.is_file() and not stale.is_symlink():
                stale.unlink()


def equivalent(original, loaded, source):
    try:
        verify_output_equivalence(original, loaded, source)
        return True
    except RuntimeError:
        return False


def read_bytes_equal(path, blob):
    return read_bounded(path, MAX_CONFIG) == blob
