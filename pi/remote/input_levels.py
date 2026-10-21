"""Shared, read-only USB input metering, including inputs not sent to speakers."""
import array
import json
import math
import subprocess
import sys
import threading
import time

FLOOR_DB = -120.0
CLIP_DB = -.01
OUTPUT_ROLES = ('EX30 · Left', 'EX30 · Right', 'EX32 · Left', 'EX32 · Right',
                'Subwoofer · Left', 'Subwoofer · Right', 'Tweeter · Left', 'Tweeter · Right')


def db(value):
    return max(FLOOR_DB, min(0.0, 20 * math.log10(value))) if value > 0 else FLOOR_DB


def safe_db(value):
    return max(FLOOR_DB, min(0.0, float(value))) if isinstance(value, (int, float)) and math.isfinite(value) else FLOOR_DB


def pcm_levels(data, channels, selected):
    samples = array.array('i', data)
    if sys.byteorder != 'little':
        samples.byteswap()
    result = []
    for channel in selected:
        values = samples[channel::channels]
        peak = max(map(abs, values), default=0) / 2147483648
        rms = math.sqrt(sum((value / 2147483648) ** 2 for value in values) / max(1, len(values)))
        result.append(dict(peak_db=db(peak), rms_db=db(rms), clipped=db(peak) >= CLIP_DB))
    return result


def input_devices(sources):
    devices = {}
    for source in sources:
        if not source['id'].startswith('usb:') or ':mono-' not in source['id']:
            continue
        device = devices.setdefault(source['device'], dict(source, inputs=[]))
        channel = source['mapping'][0]
        device['inputs'].append(dict(id=source['id'], channel=channel, label=f'Input {channel + 1}'))
    return devices


class CaptureProbe:
    """A single PCM reader per unused interface; no recording is written to disk."""
    def __init__(self, device):
        self.lock = threading.Lock()
        self.latest, self.pending, self.updated = None, None, 0
        rate = 48000 if 48000 in device['rates'] else device['rates'][0]
        self.channels = device['channels']
        self.selected = [item['channel'] for item in device['inputs']]
        self.block_bytes = max(1, rate // 10) * self.channels * 4
        self.process = subprocess.Popen(
            ['arecord', '-q', '-D', device['device'], '-f', 'S32_LE', '-r', str(rate),
             '-c', str(self.channels), '-t', 'raw'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while True:
                data = self.process.stdout.read(self.block_bytes)
                if not data or len(data) % (4 * self.channels):
                    return
                levels = pcm_levels(data, self.channels, self.selected)
                with self.lock:
                    self.latest, self.updated = levels, time.monotonic()
                    if self.pending is None:
                        self.pending = [dict(level) for level in levels]
                    else:
                        for saved, level in zip(self.pending, levels):
                            saved['peak_db'] = max(saved['peak_db'], level['peak_db'])
                            saved['clipped'] |= level['clipped']
                            saved['rms_db'] = level['rms_db']
        except (OSError, ValueError):
            pass

    def read(self):
        with self.lock:
            if self.latest is None or time.monotonic() - self.updated > 1:
                return None
            result = self.pending or [dict(level, clipped=False) for level in self.latest]
            self.pending = None
            return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.thread.join(timeout=1)
        self.process.stdout.close()


class InputLevels:
    """Visible viewers share sampling; capture stops when their leases expire."""
    def __init__(self, audio_lock, dsp_factory, discover, audio_owned, probe_factory=CaptureProbe):
        self.audio_lock, self.dsp_factory, self.discover, self.audio_owned_callback = audio_lock, dsp_factory, discover, audio_owned
        self.probe_factory = probe_factory
        self.lock = threading.Lock()
        self.thread = None
        self.last_view = 0
        self.sampled = 0
        self.probes, self.retry, self.clip_seq = {}, {}, {}
        self.dsp, self.config, self.devices, self.audio_owned = None, {}, {}, False
        self.checked = 0
        self.frame = dict(inputs=[], outputs=self._outputs(None, False), message='Checking input levels…',
                          output_message='Checking speaker output levels…')

    def snapshot(self):
        with self.lock:
            self.last_view = time.monotonic()
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
            return dict(self.frame, age_ms=(time.monotonic() - self.sampled) * 1000 if self.sampled else None)

    def release(self):
        """Called under audio_lock before another action needs exclusive capture."""
        for probe in self.probes.values():
            probe.close()
        self.probes.clear()
        self.retry.clear()
        self.checked = 0

    def _disconnect(self):
        if self.dsp is not None:
            self.dsp.__exit__(None, None, None)
            self.dsp = None
        self.checked = 0

    def _step(self):
        now = time.monotonic()
        if now - self.checked >= 1:
            self.audio_owned = bool(self.audio_owned_callback())
            self.devices = input_devices(self.discover())
            if not self.audio_owned:
                if self.dsp is None:
                    self.dsp = self.dsp_factory().__enter__()
                self.config = json.loads(self.dsp.call('GetConfigJson'))
            self.checked = now
        if self.audio_owned:
            self.release()
            self._disconnect()
            self.checked = now
            return dict(inputs=[self._row(device, item, None, 'Auto Tune is using this input')
                                for device in self.devices.values() for item in device['inputs']],
                        outputs=self._outputs(None, False),
                        message='Input meters pause while Auto Tune owns the audio devices.',
                        output_message='Output meters pause while Auto Tune owns the audio devices.')
        capture = self.config.get('devices', {}).get('capture', {}).get('device')
        # A selected interface is already open in CamillaDSP: use its input meters.
        active = self.devices.get(capture)
        levels = self.dsp.call('GetSignalLevelsSince', .25)
        running = self.dsp.call('GetState') == 'Running'
        if active:
            if capture in self.probes:
                self.probes.pop(capture).close()
        for name in list(self.probes):
            if name not in self.devices:
                self.probes.pop(name).close()
        rows = []
        for name, device in self.devices.items():
            measured = None
            if name == capture and running:
                rms, peak = levels.get('capture_rms', []), levels.get('capture_peak', [])
                measured = [dict(rms_db=safe_db(rms[item['channel']]), peak_db=safe_db(peak[item['channel']]),
                                 clipped=safe_db(peak[item['channel']]) >= CLIP_DB)
                            if item['channel'] < min(len(rms), len(peak)) else None for item in device['inputs']]
            elif name != capture:
                probe = self.probes.get(name)
                if probe is not None and probe.process.poll() is not None:
                    self.probes.pop(name).close()
                    self.retry[name] = now + 2
                    probe = None
                if probe is None and now >= self.retry.get(name, 0):
                    try:
                        probe = self.probe_factory(device)
                        self.probes[name] = probe
                    except OSError:
                        self.retry[name] = now + 2
                if probe is not None:
                    measured = probe.read()
            for index, item in enumerate(device['inputs']):
                metric = measured[index] if measured is not None and index < len(measured) else None
                rows.append(self._row(device, item, metric, 'Waiting for input / device busy'))
        return dict(inputs=rows, outputs=self._outputs(levels, running),
                    message='Levels before speaker volume and EQ · clipping stays lit until reset.' if rows else 'No USB audio inputs connected.',
                    output_message='Digital levels after speaker DSP · RMS and peak in dBFS · clipping stays lit until reset.')

    def _outputs(self, levels, running):
        playback = self.config.get('devices', {}).get('playback', {})
        count, labels = playback.get('channels', 0), playback.get('labels') or []
        rms, peak = (levels or {}).get('playback_rms', []), (levels or {}).get('playback_peak', [])
        rows = []
        for channel in range(8):
            metric = None
            if running and channel < min(count, len(rms), len(peak)):
                metric = dict(rms_db=safe_db(rms[channel]), peak_db=safe_db(peak[channel]),
                              clipped=safe_db(peak[channel]) >= CLIP_DB)
            role = labels[channel] if channel < len(labels) and labels[channel] else OUTPUT_ROLES[channel]
            device = dict(group=role)
            item = dict(id=f'output:{channel + 1}', label=f'Output {channel + 1}')
            rows.append(self._row(device, item, metric, 'DSP output unavailable'))
        return rows

    def _row(self, device, item, metric, unavailable):
        identifier = item['id']
        self.clip_seq.setdefault(identifier, 0)
        if metric and metric['clipped']:
            self.clip_seq[identifier] += 1
        return dict(id=identifier, device=device['group'], label=item['label'], available=metric is not None,
                    rms_db=metric['rms_db'] if metric else FLOOR_DB,
                    peak_db=metric['peak_db'] if metric else FLOOR_DB,
                    clip_seq=self.clip_seq[identifier],
                    status=('Signal' if metric['peak_db'] >= -80 else 'Quiet') if metric else unavailable)

    def _loop(self):
        try:
            while True:
                started = time.monotonic()
                with self.audio_lock:
                    with self.lock:
                        expired = started - self.last_view > 3
                    if expired:
                        self.release()
                        self._disconnect()
                        with self.lock:
                            # A new viewer may have arrived while capture was closing.
                            if time.monotonic() - self.last_view > 3:
                                self.frame = dict(inputs=[], outputs=self._outputs(None, False),
                                                  message='Input meters paused.', output_message='Output meters paused.')
                                return
                        continue
                    try:
                        frame = self._step()
                    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
                        self.release()
                        self._disconnect()
                        frame = dict(inputs=[], outputs=self._outputs(None, False),
                                     message='Input meters unavailable · reconnecting…', output_message='Output meters unavailable · reconnecting…')
                    with self.lock:
                        self.frame, self.sampled = frame, time.monotonic()
                time.sleep(max(.01, .1 - (time.monotonic() - started)))
        finally:
            with self.audio_lock:
                self.release()
                self._disconnect()
            with self.lock:
                self.thread = None
