"""Save and recover the Tidal queue around a confirmed DAC reboot."""

import hashlib
import json
import logging
import math
from pathlib import Path
import socket
import stat
import time

from airplay_baseline import BaselineRoute, MAX_JOURNAL, persist, read_bounded, sync_directory
from audio_watch import CLOSED, DAC_DEVICE, advances, sample_pcm
from audio_sources import source_metadata

REBOOT_SOCKET = Path('/run/octet-dac-recovery.sock')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
MPD_BOOT_STATE = Path('/run/octet-audio-boot/mpd.json')


class RecoveryRefused(RuntimeError):
    pass


def request(operation):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(11)
        connection.connect(str(REBOOT_SOCKET))
        connection.sendall(operation + b'\n')
        with connection.makefile('rb') as response:
            result = response.readline(512).decode().strip()
    if result.startswith('NO '):
        raise RecoveryRefused(result[3:])
    if result != 'OK':
        raise OSError('The DAC recovery helper did not confirm the request')


def queue_digest(playlist):
    # MPD reassigns song IDs on boot. File identity includes Octet's queue entry ID.
    files = [value for key, value in playlist if key == 'file']
    return hashlib.sha256(json.dumps(files).encode()).hexdigest(), len(files)


class DacRecovery:
    def __init__(self, server, *, journal=None, boot_id=BOOT_ID, broker=request, read_pcm=sample_pcm, sleep=time.sleep):
        self.server = server
        self.journal = journal if journal is not None else server.STATE / 'dac-recovery.json'
        self.boot_id = boot_id
        self.broker = broker
        self.read_pcm = read_pcm
        self.sleep = sleep
        self.lease = BaselineRoute(server)
        self.phase = None
        self.error = None
        self.boot_restore = False

    @property
    def busy(self):
        return self.phase in ('rebooting', 'restoring')

    def installed(self):
        try:
            return stat.S_ISSOCK(REBOOT_SOCKET.stat().st_mode)
        except OSError:
            return False

    def status(self):
        return dict(phase=self.phase, error=self.error, kind='boot' if self.boot_restore else 'dac',
                    available=self.installed() and not self.busy and (self.phase != 'failed' or self.boot_restore)
                    and self.server.AUDIO_WATCH.report['fault'] == 'dac_output_stalled'
                    and self.server.AUDIO_WATCH.report['source'] == 'tidal')

    def guarded_config(self, dsp):
        self.server.approved(dsp)
        config = json.loads(dsp.call('GetConfigJson'))
        if (source_metadata(config)['id'] != 'tidal'
                or config['devices']['playback']['device'] != DAC_DEVICE):
            raise RecoveryRefused('DAC recovery requires the saved Tidal route')
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def check_owners(self):
        tuning = self.server.TUNING.status()
        if (not self.server.AUDIO_WATCH._tidal_has_control()
                or self.server.source_route().exists()
                or not tuning.get('available') or tuning.get('audio_owned') is not False):
            raise RecoveryRefused('End the active cast or tuning session before DAC recovery')
        self.lease.check_lock()

    def start(self):
        server = self.server
        with server.LOCK:
            if self.busy or self.journal.exists() or self.journal.is_symlink():
                raise RecoveryRefused('A DAC recovery is already pending')
            if not self.status()['available']:
                raise RecoveryRefused('There is no confirmed Tidal DAC stall to recover')
            self.phase = None
            self.boot_restore = False
            self.error = None
            try:
                self.lease.acquire()
                self.check_owners()
                self.broker(b'check')
                with server.DSP() as dsp:
                    config = self.guarded_config(dsp)
                    current, playlist = server.mpd('status', 'playlistinfo')
                    current = dict(current)
                    queue, count = queue_digest(playlist)
                    elapsed = float(current['elapsed'])
                    position = int(current['song'])
                    if (current['state'] not in ('play', 'pause') or not 0 <= position < count
                            or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400):
                        raise RecoveryRefused('The current track and position could not be saved')
                    dsp.call('SetMute', True)
                    if dsp.call('GetMute') is not True:
                        raise RecoveryRefused('Speaker mute could not be verified')
                    paused = dict(server.mpd('pause 1', 'status')[1])
                    if (paused.get('state') != 'pause'
                            or any(paused.get(key) != current[key] for key in ('playlist', 'songid'))):
                        raise RecoveryRefused('The same track could not be verified paused')
                    elapsed = float(paused['elapsed'])
                    if not 0 <= elapsed <= 86400:
                        raise RecoveryRefused('The paused track position could not be saved')
                    self.sleep(.4)
                    if self.read_pcm()[0] != CLOSED:
                        raise RecoveryRefused('MPD did not release the Loopback playback device')
                    self.check_owners()
                    if dsp.call('GetMute') is not True:
                        raise RecoveryRefused('Speaker mute could not be verified before reboot')
                    record = dict(boot=self.boot_id.read_text().strip(), queue=queue, position=position,
                                  elapsed=elapsed, config=config)
                    persist(self.journal, json.dumps(record).encode(), 0o600)
                    self.phase = 'rebooting'
                    server.CONTROL_GENERATION += 1
                    server.AUDIO_INTENT += 1
                    try:
                        self.broker(b'reboot')
                    except RecoveryRefused:
                        self.journal.unlink()
                        sync_directory(self.journal.parent)
                        self.phase = None
                        raise
            except (OSError, RuntimeError, ValueError, KeyError) as error:
                self.error = str(error) if isinstance(error, RecoveryRefused) else 'DAC recovery could not be confirmed. Check the Octet logs.'
                logging.warning('DAC recovery request failed: %s', error)
                raise RecoveryRefused(self.error) from error
            finally:
                # Retain the lease when the reboot was accepted or the response was lost.
                if not self.busy:
                    self.lease.release()
            return dict(phase=self.phase)

    def automatic(self):
        if self.status()['available']:
            try:
                self.start()
            except RecoveryRefused:
                pass  # start records the refusal for the operator and the journal.

    def prepare_startup(self):
        try:
            self.journal.lstat()
        except FileNotFoundError:
            if not MPD_BOOT_STATE.is_file():
                return
            boot = self.boot_id.read_text().strip()
            try:
                if read_bounded(self.journal.with_name('mpd-boot-processed'), 64).decode() == boot:
                    return
            except FileNotFoundError:
                pass
            self.boot_restore = True
        self.phase = 'restoring'

    def restore(self):
        if self.phase != 'restoring':
            return
        server = self.server
        with server.LOCK:
            try:
                boot = self.boot_id.read_text().strip()
                path = MPD_BOOT_STATE if self.boot_restore else self.journal
                record = json.loads(read_bounded(path, MAX_JOURNAL))
                if self.boot_restore and record['boot'] != boot:
                    raise RecoveryRefused('The MPD snapshot is from a different boot')
                if not self.boot_restore and record['boot'] == boot:
                    self.phase = 'rebooting'
                    self.error = 'The DAC recovery reboot has not completed. Check the Octet logs.'
                    return
                persist(self.journal.with_name('mpd-boot-processed'), boot.encode(), 0o600)
                if not self.boot_restore:
                    self.broker(b'ready')
                self.lease.acquire()
                self.check_owners()
                with server.DSP() as dsp:
                    config = self.guarded_config(dsp)
                    if ((not self.boot_restore and config != record['config']) or dsp.call('GetMute') is not True):
                        raise RecoveryRefused('The saved DSP configuration and mute could not be verified')
                    before = self.read_pcm()[2]
                    self.sleep(.4)
                    if not advances(before, self.read_pcm()[2]):
                        raise RecoveryRefused('DAC progress did not return after reboot')
                    current, playlist = server.mpd('status', 'playlistinfo')
                    queue, count = queue_digest(playlist)
                    position, elapsed = record['position'], record['elapsed']
                    if (queue != record['queue'] or dict(current)['state'] not in ('stop', 'pause')
                            or type(position) is not int or not 0 <= position < count
                            or type(elapsed) not in (int, float) or not 0 <= elapsed <= 86400):
                        raise RecoveryRefused('The saved playback position no longer matches the queue')
                    entries = [value for key, value in playlist if key == 'Id']
                    song = entries[position]
                    if not song.isascii() or not song.isdecimal():
                        raise RecoveryRefused('The saved track could not be identified')
                    try:
                        server.mpd(f'seekid {song} {elapsed}')
                    finally:
                        paused = dict(server.mpd('pause 1', 'status')[1])
                    if (paused.get('state') != 'pause' or paused.get('songid') != song
                            or abs(float(paused['elapsed']) - elapsed) > 2 or dsp.call('GetMute') is not True):
                        raise RecoveryRefused('The saved track could not be verified paused')
                if not self.boot_restore:
                    self.journal.unlink()
                    sync_directory(self.journal.parent)
            except (OSError, RuntimeError, ValueError, KeyError, IndexError, TypeError) as error:
                operation = 'Paused-track restore' if self.boot_restore else 'DAC recovery'
                self.error = f'{operation} needs attention. Verify mute and check the Octet logs.'
                self.phase = 'failed'
                logging.error('DAC recovery after reboot: %s', error)
            else:
                self.phase = 'restored'
                logging.info('Saved track restored paused after boot. Output remains muted.')
            finally:
                self.lease.release()
