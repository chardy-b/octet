"""Explicit streams: eight DAC outputs and an independently clocked microphone."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import numpy as np
from scipy.signal import sosfilt

from ..audio import ensure_com
from .. import peq


@contextmanager
def exclusive(path):
    """An OS lock, released even if the process crashes."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as handle:
        handle.seek(0)
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError('Another Octet process owns the audio devices.') from e
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Hardware:
    demo = False

    def __init__(self, cfg):
        from .config import validate
        validate(cfg, hardware=True)
        if sys.platform != 'linux':
            raise ValueError('Auto Tune hardware playback runs on the Linux Pi; use demo on this computer.')
        self.cfg = cfg
        self.cancel = threading.Event()

    def systemctl(self, action, service=None):
        subprocess.run(['sudo', '-n', 'systemctl', action, service or self.cfg['camilla']['service']], check=True,
                       capture_output=True, text=True, timeout=20)

    def take_dac(self):
        # Stop producers as well: otherwise MPD and the verification stimulus can
        # contend for the same ALSA loopback playback endpoint during activation.
        for service in self.cfg['quiesce_services']:
            self.systemctl('stop',service)
        self.systemctl('stop')

    def stop(self):
        self.cancel.set()

    def capture(self, signal, cancel, meter=None, loopback=False):
        import sounddevice as sd
        ensure_com()
        cfg, fs = self.cfg, self.cfg['fs']
        if cancel.is_set() or self.cancel.is_set():
            raise InterruptedError('Playback stopped.')
        signal = np.asarray(signal, dtype=np.float32)
        if signal.ndim != 2 or signal.shape[1] != (2 if loopback else 8) or not np.isfinite(signal).all():
            raise ValueError('Invalid output buffer.')
        if not loopback:
            for d in cfg['drivers']:
                if np.max(abs(signal[:,d['output']-1])) > 10**(d['max_db']/20)*1.00001:
                    raise ValueError(f"Output ceiling exceeded on {d['id']}.")
        input_dev = cfg['input_device']
        output_dev = cfg['camilla']['loopback_output'] if loopback else cfg['output_device']
        sd.check_input_settings(device=input_dev, channels=cfg['input_channels'], samplerate=fs)
        sd.check_output_settings(device=output_dev, channels=signal.shape[1], samplerate=fs)
        # Capture a lead-in before opening playback, plus enough tail for clock skew.
        buffer = np.zeros((len(signal)+3*fs, cfg['input_channels']), dtype=np.float32)
        pos, sent, last_input = [0], [0], [time.monotonic()]
        errors, done = [], threading.Event()

        def input_cb(data, frames, timing, status):
            last_input[0] = time.monotonic()
            if status:
                errors.append(f'Capture discontinuity: {status}')
            n = min(frames, len(buffer)-pos[0])
            buffer[pos[0]:pos[0]+n] = data[:n]
            pos[0] += n
            peak = float(np.max(abs(data[:,cfg['mic_channel']])))
            if not np.isfinite(data).all() or peak >= 10**(cfg['clip_db']/20):
                errors.append('Microphone clipping or invalid samples; lower the gain.')
            if errors:
                done.set()
            if n < frames:
                raise sd.CallbackStop

        def output_cb(data, frames, timing, status):
            data.fill(0)
            if status:
                errors.append(f'Playback discontinuity: {status}')
            if cancel.is_set() or self.cancel.is_set() or errors:
                done.set()
                raise sd.CallbackAbort
            n = min(frames, len(signal)-sent[0])
            if n:
                data[:n] = signal[sent[0]:sent[0]+n]
                sent[0] += n
            if n < frames:
                done.set()
                raise sd.CallbackStop

        with sd.InputStream(device=input_dev, channels=cfg['input_channels'], samplerate=fs,
                            dtype='float32', blocksize=256, callback=input_cb):
            if cancel.wait(.25):
                raise InterruptedError('Playback stopped.')
            with sd.OutputStream(device=output_dev, channels=signal.shape[1], samplerate=fs,
                                 dtype='float32', blocksize=256, callback=output_cb):
                deadline = time.monotonic()+len(signal)/fs+5
                while not done.wait(.05):
                    if time.monotonic()-last_input[0] > 1 or time.monotonic() > deadline:
                        errors.append('Audio stream stopped responding.')
                        break
                    if cancel.is_set() or self.cancel.is_set():
                        break
                    if meter and pos[0]:
                        x = buffer[max(0,pos[0]-fs//10):pos[0],cfg['mic_channel']]
                        meter(float(20*np.log10(max(np.max(abs(x)), 1e-12))))
            if not errors and not cancel.is_set() and not self.cancel.is_set():
                cancel.wait(1)
        if cancel.is_set() or self.cancel.is_set():
            raise InterruptedError('Playback stopped.')
        if errors:
            raise RuntimeError(errors[0])
        return buffer[:pos[0],cfg['mic_channel']].astype(float)


class Simulated:
    demo = True

    def __init__(self, cfg, ppm=45, seed=0):
        self.cfg, self.ppm = cfg, ppm
        self.rng = np.random.default_rng(seed)
        self.position = 0
        self.cancel = threading.Event()
        self.applied = None

    def take_dac(self):
        pass

    def stop(self):
        self.cancel.set()

    def capture(self, signal, cancel, meter=None, loopback=False):
        from .dsp import render_chain
        if cancel.is_set() or self.cancel.is_set():
            raise InterruptedError('Playback stopped.')
        cfg, fs = self.cfg, self.cfg['fs']
        if loopback:
            if self.applied is None:
                raise RuntimeError('No simulated configuration active.')
            rendered = []
            for index, side in enumerate(('L','R')):
                y, scale = render_chain(cfg, side, self.applied['chosen'][side], self.applied['eq'], signal[:,index], self.applied['room'].get(side, []))
                rendered.append(y*self.applied['preamp'][side]/scale)
            signal = sum(rendered)
        y = np.zeros(len(signal)+fs)
        for d in cfg['drivers']:
            x = signal[:,d['output']-1]
            role = d['role']
            filters = {'sub': [peq.Peq('LP',400,0,.71), peq.Peq('PK',65,7,2)],
                       'a': [peq.Peq('HP',130,0,.71), peq.Peq('PK',700,5,1.5)],
                       'b': [peq.Peq('HP',170,0,.71), peq.Peq('PK',1100,4,2)],
                       'tweeter': [peq.Peq('HP',2200,0,.71)]}[role]
            v = sosfilt(peq.sos_for(filters, fs), x)
            gain = {'sub':.8,'a':.7,'b':.6,'tweeter':1.1}[role]
            delay = int(fs*({'sub':.004,'a':.001,'b':.0018,'tweeter':.0005}[role]))
            y[delay:delay+len(v)] += gain*v
            echo = delay+int(fs*(.012+self.position*.0004))
            y[echo:echo+len(v)] += .12*gain*v
        y = np.pad(y, (int(.25*fs), 0))
        # Input clock produces a longer recording for positive ppm.
        ratio = 1+self.ppm*1e-6
        rec = np.interp(np.arange(int(len(y)*ratio))/ratio, np.arange(len(y)), y)
        rec += self.rng.normal(0, 1e-7, len(rec))
        if meter:
            meter(float(20*np.log10(max(np.max(abs(rec)),1e-12))))
        return rec
