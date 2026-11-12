"""Bounded, read-only audio flight recorder. Never changes a route or playback.

Keep recent samples in RAM; persist only confirmed incidents, before recovery.
Do not collect track URLs, credentials, audio samples or the full DSP profile.
"""
from collections import deque
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import time

HISTORY = 180  # Six minutes at the watchdog's nominal two-second interval.
KEEP = 8
LIMIT = 16384


def read_text(path):
    try:
        with Path(path).open() as stream:
            return stream.read(LIMIT)
    except OSError as error:
        return {'unavailable': error.__class__.__name__}


def pcm_details(root=Path('/proc/asound')):
    result = {}
    # Numeric cards avoid collecting every symlink alias a second time.
    for card in sorted(root.glob('card[0-9]*')):
        for path in sorted(card.glob('pcm*/sub*/*')):
            if path.name in ('status', 'hw_params', 'sw_params'):
                result[path.relative_to(root).as_posix()] = read_text(path)
    return result


def pcm_counters(root=Path('/proc/asound')):
    """Include AirPlay sub1 and Bluetooth sub2, not just the Tidal endpoints."""
    result = {}
    for card in sorted(root.glob('card[0-9]*')):
        for path in sorted(card.glob('pcm*/sub*/status')):
            raw = read_text(path)
            fields = raw
            if isinstance(raw, str) and raw.strip() != 'closed':
                fields = {key.strip(): value.strip() for key, sep, value in
                          (line.partition(':') for line in raw.splitlines()) if sep
                          and key.strip() in ('state', 'owner_pid', 'trigger_time', 'hw_ptr', 'appl_ptr', 'delay')}
            result[path.parent.relative_to(root).as_posix()] = fields
    return result


def camilla_file_tail(path=Path('/opt/camilladsp/camilladsp.log')):
    try:
        with path.open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 256 * 1024))
            lines = stream.read(256 * 1024).decode('utf-8', errors='replace').splitlines()
        # WebSocket disconnect noise must not obscure underrun/clock evidence.
        return '\n'.join(line for line in lines if 'socketserver' not in line)[-LIMIT:]
    except OSError as error:
        return {'unavailable': error.__class__.__name__}


class AudioDiagnostics:
    def __init__(self, root, *, clock=time.time, monotonic=time.monotonic):
        self.root = Path(root) / 'audio-incidents'
        self.clock, self.monotonic = clock, monotonic
        self.history = deque(maxlen=HISTORY)
        self.last_devices = None
        self.warned = False
        self.metrics = {}
        self.next_metrics = 0

    def sample_metrics(self, dsp):
        now = self.monotonic()
        if now < self.next_metrics:
            return
        self.next_metrics = now + 10
        self.metrics = {'monotonic': now}
        for command in ('GetCaptureRate', 'GetRateAdjust', 'GetBufferLevel', 'GetProcessingLoad'):
            try:
                self.metrics[command] = dsp.call(command)
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
                self.metrics[command] = {'unavailable': error.__class__.__name__}
                break

    def observe(self, source, state, expected, config, samples):
        devices = config.get('devices', {})
        encoded = json.dumps(devices, sort_keys=True)
        entry = dict(time=self.clock(), monotonic=self.monotonic(), source=source,
                     dsp_state=state, expected=bool(expected), pcm=samples,
                     streams=pcm_counters(),
                     metrics=self.metrics.copy(),
                     devices_sha256=hashlib.sha256(encoded.encode()).hexdigest())
        # Retain clock/buffer/format settings when the route changes, not on every tick.
        if encoded != self.last_devices:
            entry['devices'] = json.loads(encoded)
            self.last_devices = encoded
        self.history.append(entry)

    def incident(self, fault, source, dsp_factory):
        """Diagnostics must never prevent the existing recovery callback."""
        try:
            self._incident(fault, source, dsp_factory)
        except Exception:
            if not self.warned:
                logging.exception('Could not save audio incident evidence; recovery remains available')
            self.warned = True
        else:
            self.warned = False

    def _incident(self, fault, source, dsp_factory):
        record = dict(version=1, time=self.clock(), fault=fault, source=source,
                      kernel=platform.release(), boot=read_text('/proc/sys/kernel/random/boot_id'),
                      history=list(self.history), pcm=pcm_details(),
                      devices=json.loads(self.last_devices) if self.last_devices is not None else None,
                      cards=read_text('/proc/asound/cards'),
                      camilla_file=camilla_file_tail(),
                      load=read_text('/proc/loadavg'), pressure=read_text('/proc/pressure/cpu'))
        # These are observational CamillaDSP commands. Unsupported commands are evidence too.
        metrics = record['dsp'] = {}
        try:
            with dsp_factory() as dsp:
                for command in ('GetConfigFilePath', 'GetState', 'GetCaptureRate', 'GetRateAdjust',
                                'GetBufferLevel', 'GetProcessingLoad', 'GetClippedSamples', 'GetMute', 'GetVolume'):
                    try:
                        metrics[command] = dsp.call(command)
                    except (OSError, RuntimeError, ValueError) as error:
                        metrics[command] = {'unavailable': error.__class__.__name__}
                        break  # Do not multiply a DSP timeout across all commands.
        except (OSError, RuntimeError, ValueError) as error:
            metrics['connection'] = {'unavailable': error.__class__.__name__}
        # Filter remote logs to audio events: HTTP logs can contain private URLs.
        queries = {
            'camilla_log': ['-u', 'camilladsp'],
            'transitions': ['-u', 'octet-remote', '--grep',
                            '^(INFO (AirPlay |DSP route|DSP mute|Bluetooth )|WARNING Audio watchdog:)'],
            'kernel_dma': ['-k', '--grep', 'dma.*(failed to stop|non-idle|i2s)'],
        }
        for name, query in queries.items():
            try:
                logs = subprocess.run(['/usr/bin/journalctl', '-q', '-b', *query,
                                       '-n', '80', '--no-pager', '-o', 'short-monotonic'],
                                      capture_output=True, text=True, timeout=2)
                record[name] = dict(text=logs.stdout[-LIMIT:], returncode=logs.returncode,
                                    stderr=logs.stderr[-1024:])
            except (OSError, subprocess.SubprocessError) as error:
                record[name] = {'unavailable': error.__class__.__name__}
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = json.dumps(record, allow_nan=False).encode()
        if len(data) > 1024 * 1024:
            raise ValueError('Audio incident exceeded the one MiB limit')
        # Unique, owner-only files; rename + fsync survives a recovery reboot.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix='.incident-', delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            target = self.root / (temporary.name.removeprefix('.') + '.json')
            temporary.replace(target)
            incidents = sorted(self.root.glob('incident-*.json'), key=lambda path: path.stat().st_mtime_ns)
            for old in incidents[:-KEEP]:
                old.unlink()
            if hasattr(os, 'O_DIRECTORY'):
                fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            logging.warning('Audio incident evidence saved: %s', target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
