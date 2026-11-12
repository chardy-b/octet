"""Save and recover the Tidal queue around a confirmed DAC reboot."""

import hashlib
import json
import logging
import math
from pathlib import Path
import socket
import stat
import threading
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
        connection.settimeout(95)
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
        return self.phase in ('recovering', 'restarting', 'driver_reset', 'rebooting', 'restoring')

    @property
    def blocked(self):
        return self.phase == 'failed' and (self.journal.exists() or self.journal.is_symlink())

    def installed(self):
        try:
            return stat.S_ISSOCK(REBOOT_SOCKET.stat().st_mode)
        except OSError:
            return False

    def status(self):
        return dict(phase=self.phase, error=self.error, kind='boot' if self.boot_restore else 'dac',
                    available=self.installed() and not self.busy and (self.phase != 'failed' or self.boot_restore)
                    and self.server.AUDIO_WATCH.report['fault'] == 'dac_output_stalled'
                    and self.server.AUDIO_WATCH.report['source'] not in (None, 'unknown'))

    def guarded_config(self, dsp):
        self.server.approved(dsp)
        config = json.loads(dsp.call('GetConfigJson'))
        if (source_metadata(config)['id'] in ('unknown', 'airplay', 'bluetooth')
                or config['devices']['playback']['device'] != DAC_DEVICE):
            raise RecoveryRefused('DAC recovery requires a saved playback route')
        return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()

    def check_owners(self):
        tuning = self.server.TUNING.status()
        if (not self.server.AUDIO_WATCH._tidal_has_control()
                or self.server.source_route().exists()
                or not tuning.get('available') or tuning.get('audio_owned') is not False):
            raise RecoveryRefused('End the active cast or tuning session before DAC recovery')
        self.lease.check_lock()

    def _save(self, record):
        persist(self.journal, json.dumps(record).encode(), 0o600)

    def _queue_record(self):
        current, playlist = self.server.mpd('status', 'playlistinfo')
        current = dict(current)
        queue, count = queue_digest(playlist)
        position = int(current['song']) if current.get('song') is not None else None
        elapsed = float(current.get('elapsed', 0))
        if (current.get('state') not in ('play', 'pause', 'stop')
                or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400
                or (position is not None and not 0 <= position < count)):
            raise RecoveryRefused('The queue and position could not be saved')
        if current['state'] == 'play':
            paused = dict(self.server.mpd('pause 1', 'status')[1])
            if (paused.get('state') != 'pause'
                    or any(paused.get(key) != current.get(key) for key in ('playlist', 'songid'))):
                raise RecoveryRefused('The same track could not be verified paused')
            elapsed = float(paused['elapsed'])
            if not math.isfinite(elapsed) or not 0 <= elapsed <= 86400:
                raise RecoveryRefused('The paused position could not be saved')
        return dict(queue=queue, position=position, elapsed=elapsed, count=count)

    def start(self):
        server = self.server
        with server.LOCK:
            if self.busy or self.journal.exists() or self.journal.is_symlink():
                raise RecoveryRefused('A DAC recovery is already pending')
            if not self.status()['available']:
                raise RecoveryRefused('There is no confirmed DAC output stall to recover')
            self.boot_restore, self.error = False, None
            self.phase = 'recovering'
            try:
                tuning = server.TUNING.status()
                if not tuning.get('available') or tuning.get('audio_owned') is not False:
                    raise RecoveryRefused('Auto Tune owns audio or its ownership is unknown')
                if server.source_route().exists():
                    raise RecoveryRefused('Input recovery is already pending')
                airplay = server.AIRPLAY_CONTROL
                airplay_owns = airplay.baseline.exists() or airplay.pending is not None or airplay.active or airplay.idle
                bluetooth_owns = server.BLUETOOTH_CONTROL.owns_audio()
                if airplay_owns and bluetooth_owns:
                    raise RecoveryRefused('Conflicting receiver ownership; recovery retained')
                # Hold the current owner's shared lease before muting or pausing.
                # Transfer it to our lease only after the cast journal is restored.
                if airplay_owns:
                    airplay.baseline.acquire()
                    airplay.baseline.ownership()
                elif bluetooth_owns:
                    server.BLUETOOTH_CONTROL.route.ownership()
                else:
                    self.lease.acquire()
                with server.DSP() as dsp:
                    loaded = json.loads(dsp.call('GetConfigJson'))
                    source = source_metadata(loaded)['id']
                    if (source == 'unknown' or loaded['devices']['playback']['device'] != DAC_DEVICE
                            or source != server.AUDIO_WATCH.report['source']):
                        raise RecoveryRefused('The stalled source or hardware output changed')
                    dsp.call('SetMute', True)
                    if dsp.call('GetMute') is not True:
                        raise RecoveryRefused('Speaker mute could not be verified')
                    volume = dsp.call('GetVolume')
                    if type(volume) not in (float, int) or not math.isfinite(volume) or not -100 <= volume <= 0:
                        raise RecoveryRefused('The listening level could not be saved')
                # Root independently confirms stalled hardware and mute before any reset.
                self.broker(b'begin')
                server.CONTROL_GENERATION += 1
                server.AUDIO_INTENT += 1
                server.AUDIO_WATCH.resume_pending = None
                record = dict(version=2, boot=self.boot_id.read_text().strip(), source=source,
                              stage='preparing', volume=volume, **self._queue_record())
                self._save(record)
                # End casts through their existing journals; never delete recovery evidence.
                server.recover_source()
                if airplay.baseline.exists() or airplay.pending is not None or airplay.active or airplay.idle:
                    airplay.invalidate()
                if server.BLUETOOTH_CONTROL.owns_audio():
                    server.BLUETOOTH_CONTROL.stop('DAC recovery')
                self.lease.acquire()
                self.check_owners()
                with server.DSP() as dsp:
                    record.update(config=self.guarded_config(dsp), path=dsp.call('GetConfigFilePath'))
                    if dsp.call('GetMute') is not True:
                        raise RecoveryRefused('Speaker mute was lost while ending the source')
                self.sleep(.4)
                if self.read_pcm()[0] != CLOSED:
                    raise RecoveryRefused('MPD did not release Loopback')
                self._save(record)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                self.phase, self.error = 'failed', str(error)
                if not self.journal.exists() and not self.journal.is_symlink():
                    self.lease.release()
                logging.warning('DAC recovery preparation failed: %s', error)
                raise RecoveryRefused(self.error) from error
            # The worker releases the control lock during bounded root operations.
            # Its lease and busy flag continue to exclude tuning and receiver starts.
            self.worker = threading.Thread(target=self._run_ladder, args=(record,),
                                           name='octet-dac-recovery', daemon=True)
            self.worker.start()
            return dict(phase=self.phase)

    def _verify_local(self, record):
        server = self.server
        with server.LOCK:
            self.check_owners()
            with server.DSP() as dsp:
                if (self.guarded_config(dsp) != record['config']
                        or dsp.call('GetConfigFilePath') != record['path']
                        or dsp.call('GetMute') is not True or dsp.call('GetState') != 'Running'):
                    raise RecoveryRefused('The restored DSP configuration and mute differ')
                dsp.call('SetVolume', record['volume'])
                if dsp.call('GetVolume') != record['volume'] or dsp.call('GetMute') is not True:
                    raise RecoveryRefused('Muted listening-level restoration failed')
            before = self.read_pcm()[2]
            self.sleep(.4)
            if not advances(before, self.read_pcm()[2]):
                raise RecoveryRefused('DAC hardware progress has not returned')
            current, playlist = server.mpd('status', 'playlistinfo')
            current = dict(current)
            if queue_digest(playlist)[0] != record['queue'] or current.get('state') not in ('pause', 'stop'):
                raise RecoveryRefused('The saved queue is no longer paused/stopped')
            if record['position'] is not None and (int(current.get('song', -1)) != record['position']
                    or abs(float(current.get('elapsed', 0)) - record['elapsed']) > 2):
                raise RecoveryRefused('The saved track position changed')

    def _run_ladder(self, record):
        try:
            for operation, phase in ((b'stream', 'restarting'), (b'driver', 'driver_reset')):
                self.phase = phase
                record['stage'] = operation.decode()
                self._save(record)
                try:
                    self.broker(operation)
                except RecoveryRefused as error:
                    logging.warning('DAC %s step refused/failed: %s', operation.decode(), error)
                try:
                    self._verify_local(record)
                except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                    logging.warning('DAC %s verification: %s', operation.decode(), error)
                    continue
                # Root verifies graph, muted DAC progress, and restores held services.
                self.broker(b'finish')
                self._verify_local(record)
                self.journal.unlink()
                sync_directory(self.journal.parent)
                self.server.AUDIO_WATCH.report = dict(fault=None, recovery=None, source=None, attempts=0)
                self.server.AUDIO_WATCH.previous = None
                self.phase = 'restored'
                logging.info('DAC recovered via %s; output remains muted', operation.decode())
                return
            self.check_owners()
            record['stage'] = 'reboot'
            self._save(record)
            self.phase = 'rebooting'
            self.broker(b'reboot')
        except OSError as error:
            self.error = str(error)
            if self.phase != 'rebooting':
                self.phase = 'failed'
            logging.error('DAC recovery response lost; durable evidence retained: %s', error)
        except (RuntimeError, ValueError, KeyError, TypeError) as error:
            self.phase, self.error = 'failed', str(error)
            logging.error('DAC recovery retained muted for attention: %s', error)
        finally:
            if self.phase == 'restored' or (not self.journal.exists() and not self.journal.is_symlink()):
                self.lease.release()

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
                    if record.get('version') == 2 and record.get('stage') != 'reboot':
                        self.lease.acquire()
                        self._verify_local(record)
                        self.broker(b'finish')
                        self.journal.unlink()
                        sync_directory(self.journal.parent)
                        self.phase = 'restored'
                        return
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
                    if record.get('version') == 2:
                        volume = record['volume']
                        if type(volume) not in (int, float) or not math.isfinite(volume) or not -100 <= volume <= 0:
                            raise RecoveryRefused('Invalid saved listening level')
                        dsp.call('SetVolume', volume)
                        if dsp.call('GetVolume') != volume or dsp.call('GetMute') is not True:
                            raise RecoveryRefused('Muted listening-level restore failed')
                    before = self.read_pcm()[2]
                    self.sleep(.4)
                    if not advances(before, self.read_pcm()[2]):
                        raise RecoveryRefused('DAC progress did not return after reboot')
                    current, playlist = server.mpd('status', 'playlistinfo')
                    queue, count = queue_digest(playlist)
                    position, elapsed = record['position'], record['elapsed']
                    if (queue != record['queue'] or dict(current)['state'] not in ('stop', 'pause')
                            or (position is not None and (type(position) is not int or not 0 <= position < count))
                            or type(elapsed) not in (int, float) or not 0 <= elapsed <= 86400):
                        raise RecoveryRefused('The saved playback position no longer matches the queue')
                    if position is None:
                        # Empty/stopped MPD queues are normal while casting or using USB.
                        if record.get('version') != 2 or dict(current)['state'] != 'stop':
                            raise RecoveryRefused('The stopped queue changed during recovery')
                        self.journal.unlink()
                        sync_directory(self.journal.parent)
                        self.phase = 'restored'
                        return
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
                if self.phase == 'restored' or (not self.journal.exists() and not self.journal.is_symlink()):
                    self.lease.release()
