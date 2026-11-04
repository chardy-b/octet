"""Independent PCM health checks and one muted Tidal input recovery attempt."""

import json
import logging
from pathlib import Path
import time

from airplay_baseline import BaselineRoute
from audio_sources import source_metadata

INTERVAL = 2.0
FAILED_CHECKS = 3
LOOPBACK = Path('/proc/asound/Loopback')
DAC = Path('/proc/asound/sndrpihifiberry/pcm0p/sub0/status')
DAC_DEVICE = 'hw:sndrpihifiberry,0'
CLOSED = ('closed', '', 0)


def pcm(path):
    try:
        status = path.read_text()
        if status.strip() == 'closed':
            return CLOSED
        fields = {key.strip(): value.strip() for key, sep, value in
                  (line.partition(':') for line in status.splitlines()) if sep}
        return fields['state'], fields['owner_pid'], int(fields['hw_ptr'])
    except (OSError, ValueError, KeyError):
        return None


def advances(before, after):
    return (before is not None and after is not None and before[1] == after[1]
            and after[0] == 'RUNNING' and after[2] != before[2])


def sample_pcm():
    return (pcm(LOOPBACK / 'pcm0p/sub0/status'), pcm(LOOPBACK / 'pcm1c/sub0/status'), pcm(DAC))


class AudioWatch:
    def __init__(self, server, *, auto_recover=False, read_pcm=sample_pcm, sleep=time.sleep):
        self.server, self.read_pcm, self.sleep = server, read_pcm, sleep
        self.lease = BaselineRoute(server)
        self.auto_recover = auto_recover
        self.previous = self.source = None
        self.suspect = None
        self.checks = 0
        self.attempted = False
        self.report = dict(fault=None, recovery=None, source=None)

    def _tidal_has_control(self):
        airplay = self.server.AIRPLAY_CONTROL
        return (not (airplay.active or airplay.idle or airplay.pending is not None
                     or airplay.needs_mute or airplay.restore_pending is not None
                     or airplay.baseline.exists() or airplay.hook_busy.locked())
                and not self.server.BLUETOOTH_CONTROL.owns_audio())

    def tick(self):
        with self.server.DSP() as dsp:
            config = json.loads(dsp.call('GetConfigJson'))
            source = source_metadata(config)['id']
            dsp_state = dsp.call('GetState')
        if source == 'tidal':
            expected = dict(self.server.mpd('status')[0]).get('state') == 'play'
        elif source == 'airplay':
            expected = self.server.AIRPLAY_CONTROL.status()['active']
        elif source == 'bluetooth':
            expected = self.server.BLUETOOTH_CONTROL.status()['active']
        else:
            expected = source != 'unknown' and dsp_state == 'Running'
        if not expected or config.get('devices', {}).get('playback', {}).get('device') != DAC_DEVICE:
            self.previous = self.suspect = None
            self.checks = 0
            return

        old = self.previous if self.source == source else None
        new = self.read_pcm()
        self.previous, self.source = new, source
        if old is None:
            self.suspect, self.checks = None, 0
            return
        dac_ok = advances(old[2], new[2])
        capture_known = all(sample is not None for sample in (*old[:2], *new[:2]))
        capture_ok = capture_known and advances(old[0], new[0]) and advances(old[1], new[1])
        if old[2] is not None and new[2] is not None and not dac_ok:
            fault = 'dac_output_stalled'
        elif source == 'tidal' and dsp_state == 'Stalled' and dac_ok and capture_known and not capture_ok:
            fault = 'tidal_input_stalled'
        else:
            fault = None

        if fault != self.suspect:
            self.suspect, self.checks = fault, 0
        if fault is None:
            if dac_ok and dsp_state == 'Running' and (source != 'tidal' or capture_ok):
                if source == 'tidal':
                    self.attempted = False
                if self.report['source'] in (None, source) or self.report['fault'] == 'dac_output_stalled':
                    self.report = dict(fault=None, recovery=None, source=None)
            return
        self.checks += 1
        if self.checks < FAILED_CHECKS:
            return
        if self.report['fault'] != fault:
            logging.warning('Audio watchdog: %s during %s playback (DSP %s, PCM %s → %s)',
                            fault, source, dsp_state, old, new)
            self.report = dict(fault=fault, recovery='operator_needed', source=source)
        if fault == 'tidal_input_stalled' and self.auto_recover and not self.attempted:
            self.attempted = True
            try:
                self.recover_tidal(new)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                logging.warning('Audio watchdog recovery refused or failed: %s', error)
                self.report = dict(fault=fault, recovery='failed', source=source)
            else:
                self.report = dict(fault=None, recovery='ready_to_resume', source=source)

    def recover_tidal(self, last_pcm):
        server = self.server
        with server.LOCK:
            if not self._tidal_has_control():
                raise RuntimeError('Another receiver owns audio')
            self.lease.acquire()
            try:
                tuning = server.TUNING.status()
                if not tuning.get('available') or tuning.get('audio_owned') is not False:
                    raise RuntimeError('Auto Tune ownership is not clear')
                with server.DSP() as dsp:
                    server.approved(dsp)
                    preset = dsp.call('GetConfigFilePath')
                    volume = dsp.call('GetVolume')
                    loaded = json.loads(dsp.call('GetConfigJson'))
                    if (source_metadata(loaded)['id'] != 'tidal' or dsp.call('GetState') != 'Stalled'
                            or loaded['devices']['playback']['device'] != DAC_DEVICE):
                        raise RuntimeError('The expected Tidal route is no longer stalled')
                    self.sleep(.15)
                    observed = self.read_pcm()
                    if not advances(last_pcm[2], observed[2]):
                        raise RuntimeError('DAC progress could not be confirmed')
                    if None in observed[:2]:
                        raise RuntimeError('Tidal input counters could not be read')
                    if advances(last_pcm[0], observed[0]) and advances(last_pcm[1], observed[1]):
                        raise RuntimeError('Tidal input is progressing again')
                    current = dict(server.mpd('status')[0])
                    if current.get('state') != 'play' or not all(current.get(k) for k in ('playlist', 'songid')):
                        raise RuntimeError('Tidal is no longer playing')
                    if not self._tidal_has_control():
                        raise RuntimeError('Another receiver is preparing')
                    self.lease.check_lock()
                    dsp.call('SetMute', True)
                    if dsp.call('GetMute') is not True:
                        raise RuntimeError('Speaker mute could not be verified')
                    server.mpd('pause 1')
                    paused = dict(server.mpd('status')[0])
                    if paused.get('state') != 'pause' or any(paused.get(k) != current.get(k)
                                                             for k in ('playlist', 'songid')):
                        raise RuntimeError('Could not preserve the Tidal queue and position')
                    self.sleep(.4)
                    if self.read_pcm()[0] != CLOSED:
                        raise RuntimeError('MPD did not release the Loopback playback device')
                    if dsp.call('GetMute') is not True:
                        raise RuntimeError('Speaker mute changed during recovery')
                    if (dsp.call('GetConfigFilePath') != preset or dsp.call('GetVolume') != volume
                            or json.loads(dsp.call('GetConfigJson')) != loaded):
                        raise RuntimeError('The DSP configuration changed during recovery')
                logging.info('Audio watchdog paused Tidal and released its stalled input. Ready for Play.')
            finally:
                self.lease.release()

    def run(self):
        warned = False
        while True:
            try:
                self.tick()
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                self.previous = self.suspect = None
                self.checks = 0
                if not warned:
                    logging.exception('Audio watchdog could not read the audio state')
                warned = True
            else:
                warned = False
            self.sleep(INTERVAL)
