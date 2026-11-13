"""Independent PCM health checks and bounded Tidal input retries."""

import json
import logging
from pathlib import Path
import time

from airplay_baseline import BaselineRoute
from audio_sources import source_metadata
from audio_diagnostics import AudioDiagnostics

INTERVAL = 2.0
FAILED_CHECKS = 3
MAX_RETRIES = 3
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
    def __init__(self, server, *, auto_recover=False, on_dac_fault=None, read_pcm=sample_pcm, sleep=time.sleep):
        self.server, self.read_pcm, self.sleep = server, read_pcm, sleep
        self.lease = BaselineRoute(server)
        self.auto_recover = auto_recover
        self.on_dac_fault = on_dac_fault
        self.previous = self.source = None
        self.suspect = None
        self.checks = self.healthy_checks = self.attempts = 0
        self.report = dict(fault=None, recovery=None, source=None, attempts=0)
        self.resume_pending = None
        self.diagnostics = AudioDiagnostics(server.STATE) if isinstance(getattr(server, 'STATE', None), Path) else None

    def _resume_matches(self, status):
        pending = self.resume_pending
        return (pending is not None and self._tidal_has_control()
                and pending['generation'] == getattr(self.server, 'AUDIO_INTENT', 0)
                and all(status.get(key) == pending[key] for key in ('playlist', 'songid')))

    def _resume(self):
        """Retry only our own pause; a user command or queue change cancels it."""
        status = dict(self.server.mpd('status')[0])
        if not self._resume_matches(status) or status.get('state') != 'pause':
            self.resume_pending = None
            raise RuntimeError('MPD is no longer paused on the same track and queue')
        self.lease.check_lock()
        resumed = dict(self.server.mpd('pause 0', 'status')[1])
        if resumed.get('state') != 'play':
            raise RuntimeError('MPD did not resume playback')
        self.resume_pending = None

    def _retry_resume(self):
        with self.server.LOCK:
            status = dict(self.server.mpd('status')[0])
            if not self._resume_matches(status) or status.get('state') not in ('pause', 'play'):
                self.resume_pending = None
                self.report = dict(self.report, recovery='failed')
                return
            if status['state'] == 'play':
                self.resume_pending = None  # The timed-out resume actually reached MPD.
                return
            if self.attempts >= MAX_RETRIES:
                self.resume_pending = None
                self.report = dict(self.report, recovery='failed')
                return
            self.attempts += 1
            self.report = dict(self.report, recovery='retrying', attempts=self.attempts)
            try:
                self.lease.acquire()
                if self.server.TUNING.status().get('audio_owned') is not False:
                    raise RuntimeError('Auto Tune ownership is not clear')
                with self.server.DSP() as dsp:
                    self.server.approved(dsp)
                    config = json.loads(dsp.call('GetConfigJson')) or {}
                    if source_metadata(config)['id'] != 'tidal':
                        self.resume_pending = None
                        raise RuntimeError('Tidal route changed during recovery')
                self._resume()
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                logging.warning('Tidal resume retry %s/%s failed: %s', self.attempts, MAX_RETRIES, error)
            finally:
                self.lease.release()

    def _tidal_has_control(self):
        airplay = self.server.AIRPLAY_CONTROL
        return (not (airplay.active or airplay.idle or airplay.pending is not None
                     or airplay.recovery_pending or airplay.restore_pending is not None
                     or airplay.baseline.exists() or airplay.hook_busy.locked())
                and not self.server.BLUETOOTH_CONTROL.owns_audio())

    def tick(self):
        recovery = getattr(self.server, 'DAC_RECOVERY', None)
        if recovery is not None and recovery.busy is True:
            self.previous = None
            return
        recover = getattr(self.server, 'recover_source', None)
        if recover is not None:
            recover()
        if self.resume_pending is not None:
            self._retry_resume()
            return
        with self.server.DSP() as dsp:
            config = json.loads(dsp.call('GetConfigJson')) or {}
            source = source_metadata(config)['id']
            dsp_state = dsp.call('GetState')
            if self.diagnostics is not None:
                self.diagnostics.sample_metrics(dsp)
        if source == 'tidal':
            expected = dict(self.server.mpd('status')[0]).get('state') == 'play'
        elif source == 'airplay':
            cast = self.server.AIRPLAY_CONTROL.status()
            expected = any(cast.get(key) for key in ('active', 'idle', 'preparing'))
        elif source == 'bluetooth':
            cast = self.server.BLUETOOTH_CONTROL.status()
            expected = cast.get('active') or cast.get('preparing')
        else:
            expected = source != 'unknown' and dsp_state in ('Running', 'Stalled')
        # Observe idle periods too: an idle-to-playing edge can trigger DAC recovery.
        samples = self.read_pcm() if self.diagnostics is not None else None
        if self.diagnostics is not None:
            self.diagnostics.observe(source, dsp_state, expected, config, samples)
        if not expected or config.get('devices', {}).get('playback', {}).get('device') != DAC_DEVICE:
            self.previous = self.suspect = None
            self.checks = self.healthy_checks = 0
            if source == 'tidal' and not expected and self.report['recovery'] == 'retrying':
                self.report = dict(self.report, recovery='failed')
            return

        old = self.previous if self.source == source else None
        new = samples if samples is not None else self.read_pcm()
        self.previous, self.source = new, source
        if old is None:
            self.suspect = None
            self.checks = self.healthy_checks = 0
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
        healthy = dac_ok and dsp_state == 'Running' and (source != 'tidal' or capture_ok)
        self.healthy_checks = self.healthy_checks + 1 if healthy else 0
        if fault is None:
            if self.healthy_checks >= FAILED_CHECKS:
                if source == 'tidal':
                    self.attempts = 0
                if self.report['source'] in (None, source) or self.report['fault'] == 'dac_output_stalled':
                    self.report = dict(fault=None, recovery=None, source=None, attempts=0)
            return
        self.checks += 1
        if self.checks < FAILED_CHECKS:
            return
        if self.report['fault'] != fault:
            logging.warning('Audio watchdog: %s during %s playback (DSP %s, PCM %s → %s)',
                            fault, source, dsp_state, old, new)
            self.report = dict(fault=fault, recovery='operator_needed', source=source, attempts=0)
            if self.diagnostics is not None:
                self.diagnostics.incident(fault, source, self.server.DSP)
            if fault == 'dac_output_stalled' and self.on_dac_fault is not None:
                self.on_dac_fault()
        if fault != 'tidal_input_stalled' or not self.auto_recover:
            return
        if self.attempts >= MAX_RETRIES:
            if self.report['recovery'] != 'failed':
                logging.warning('Tidal input is still stalled after %s retries', self.attempts)
                self.report = dict(self.report, recovery='failed', attempts=self.attempts)
            return
        self.attempts += 1
        self.report = dict(self.report, recovery='retrying', attempts=self.attempts)
        try:
            self.recover_tidal(new)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            logging.warning('Tidal input retry %s/%s could not complete: %s', self.attempts, MAX_RETRIES, error)
        else:
            logging.info('Tidal input retry %s/%s resumed MPD. Checking PCM progress.',
                         self.attempts, MAX_RETRIES)
        self.previous = None

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
                try:
                    self.resume_pending = dict(playlist=current['playlist'], songid=current['songid'],
                                               generation=getattr(server, 'AUDIO_INTENT', 0))
                    server.mpd('pause 1')
                    self.sleep(.4)
                    if self.read_pcm()[0] != CLOSED:
                        raise RuntimeError('MPD Loopback release could not be verified')
                finally:
                    # Even an interrupted pause command may have reached MPD.
                    self._resume()
            finally:
                self.lease.release()

    def run(self):
        warned = False
        while True:
            try:
                self.tick()
            except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                self.previous = self.suspect = None
                self.checks = self.healthy_checks = 0
                if not warned:
                    logging.exception('Audio watchdog could not read the audio state')
                warned = True
            else:
                warned = False
            self.sleep(INTERVAL)
