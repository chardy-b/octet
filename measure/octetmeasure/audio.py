"""Audio back ends for the app: real hardware, and a simulated one for demos and tests.

An IO object offers:
    devices(), renderers()
    resolve(name_or_index, kind)                      -> device handle
    make_player(cfg, signal, fs, workdir)             -> Player (start / stop / close)
    record(dev, seconds, fs, channels, on_block, on_started, cancel)   -> recording (frames x channels)
    stream(dev, fs, channels, on_block, cancel)       -> None, runs until cancel is set
"""
import queue
import subprocess
import sys
import threading
import time

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

from . import capture, upnp
from .capture import sd
from .peq import Peq, sos_for
from .wavio import write_pcm_wav

BLOCK_S = 0.05


def ensure_com() -> bool:
    """Windows only: PortAudio's WASAPI backend fails to open a stream on a thread where COM has not been initialised
    (the error mentions WDM-KS). Every thread that opens an audio stream calls this first. Returns True if COM is
    ready; it does nothing on macOS and Linux. The thread is left initialised until it ends, which is harmless."""
    if sys.platform != "win32":
        return False
    import ctypes
    hr = ctypes.windll.ole32.CoInitializeEx(None, 0)          # multithreaded apartment
    return (hr & 0xFFFFFFFF) in (0, 1)                        # S_OK, or S_FALSE: already initialised on this thread


# ---------------------------------------------------------------- players
class Player:
    label = ""

    def start(self):
        raise NotImplementedError

    def stop(self):
        pass

    def close(self):
        pass

    def fetched(self) -> bool:
        return True


class DlnaPlayer(Player):
    """Plays a WAV on a UPnP/DLNA renderer such as a WiiM, served from this computer."""

    def __init__(self, renderer, wav_path):
        self.r = renderer
        self.label = renderer.name
        self.srv = upnp.FileServer(wav_path, upnp.local_ip_for(renderer.ip))

    def start(self):
        upnp.play(self.r, self.srv.url)

    def stop(self):
        upnp.stop(self.r)

    def fetched(self) -> bool:
        return bool(self.srv.requests)

    def close(self):
        self.srv.close()


class LocalPlayer(Player):
    """Plays through an output of this computer."""

    def __init__(self, signal: np.ndarray, fs: int, device: int):
        self.label = "this computer"
        out_ch = sd.query_devices(device)["max_output_channels"]
        self.buf = np.zeros((len(signal), max(out_ch, 2)), dtype=np.float32)
        self.buf[:, :2] = signal[:, :2]
        self.fs, self.device = fs, device

    def start(self):
        ensure_com()
        sd.play(self.buf, self.fs, device=self.device, extra_settings=capture._settings(self.device))

    def stop(self):
        ensure_com()
        sd.stop()


class ManualPlayer(Player):
    """The user starts playback, or a command does (for example one that starts MPD on a Raspberry Pi)."""

    def __init__(self, trigger: str | None = None):
        self.trigger = trigger
        self.label = "manual"

    def start(self):
        if self.trigger:
            p = subprocess.run(self.trigger, shell=True, capture_output=True, text=True, timeout=60)
            if p.returncode != 0:
                raise RuntimeError(f"The start command failed ({p.returncode}): {p.stderr.strip()[:200]}")


# ---------------------------------------------------------------- real hardware
class RealIO:
    name = "hardware"
    speed = 1.0

    def devices(self) -> list[dict]:
        api = capture.preferred_api()
        din, dout = capture.default_device("input"), capture.default_device("output")
        out = []
        for d in capture.list_devices():
            if d["api"] != api or (d["inputs"] == 0 and d["outputs"] == 0):
                continue
            out.append(dict(d, default_input=d["index"] == din, default_output=d["index"] == dout))
        return out

    def renderers(self, timeout: float = 3.0) -> list[dict]:
        return [dict(name=r.name, ip=r.ip, model=r.model) for r in upnp.discover(timeout)]

    def resolve(self, ref, kind: str) -> int:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            return int(ref)
        return capture.find_device(ref or "", kind)

    def make_player(self, cfg, signal: np.ndarray, fs: int, workdir) -> Player:
        if cfg.player == "dlna":
            r = upnp.find_renderer(cfg.renderer)
            wav = workdir / "sweep.wav"
            write_pcm_wav(wav, fs, signal, cfg.bits, 2)
            return DlnaPlayer(r, wav)
        if cfg.player == "local":
            return LocalPlayer(signal, fs, self.resolve(cfg.out_device, "output"))
        return ManualPlayer(cfg.trigger)

    def stream(self, dev, fs, channels, on_block, cancel):
        ensure_com()
        q: queue.SimpleQueue = queue.SimpleQueue()

        def cb(indata, frames, t, status):
            q.put(indata.copy())

        with sd.InputStream(device=dev, channels=channels, samplerate=fs, dtype="float32",
                            blocksize=int(BLOCK_S * fs), callback=cb, extra_settings=capture._settings(dev)):
            t0 = time.monotonic()
            while not cancel.is_set():
                try:
                    block = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                on_block(block, time.monotonic() - t0)

    def record(self, dev, seconds, fs, channels, on_block=None, on_started=None, cancel=None):
        ensure_com()
        frames = int(seconds * fs)
        buf = np.zeros((frames, channels), dtype=np.float32)
        q: queue.SimpleQueue = queue.SimpleQueue()
        pos = [0]

        def cb(indata, n, t, status):
            k = min(n, frames - pos[0])
            buf[pos[0]:pos[0] + k] = indata[:k]
            pos[0] += k
            q.put(indata[:k].copy())
            if pos[0] >= frames:
                raise sd.CallbackStop

        with sd.InputStream(device=dev, channels=channels, samplerate=fs, dtype="float32",
                            blocksize=int(BLOCK_S * fs), callback=cb, extra_settings=capture._settings(dev)) as s:
            if on_started:
                on_started()
            shown = 0
            while True:
                if cancel is not None and cancel.is_set():
                    break
                try:
                    block = q.get(timeout=0.1)
                    shown += len(block)
                    if on_block:
                        on_block(block, shown / fs)
                except queue.Empty:
                    if not s.active and q.empty():
                        break
        return buf[:pos[0]].astype(np.float64)


# ---------------------------------------------------------------- simulated hardware
def _panel_system(fs: int, which: str) -> np.ndarray:
    """Second-order sections for a panel-like response: bass roll-off, a few strong modes, and a treble roll-off.
    The 'R' variant is bass-heavy and runs out of output above a few kHz, like a thumper."""
    f = [Peq("HP", 120, 0, 0.71), Peq("PK", 300, 12, 2.0), Peq("PK", 930, 5, 3.0), Peq("PK", 5500, 8, 2.0),
         Peq("LP", 16000, 0, 0.71)]
    if which == "R":
        f = [Peq("HP", 110, 0, 0.71), Peq("PK", 300, 13, 1.8), Peq("PK", 880, 4, 3.0), Peq("LP", 2600, 0, 0.71)]
    return sos_for(f, fs)


class DemoPlayer(Player):
    def __init__(self, io, signal, fs):
        self.io, self.signal, self.fs = io, signal, fs
        self.label = "Demo streamer"

    def start(self):
        self.io.inject(self.signal, self.fs)


class DemoIO:
    """Simulated microphone, panel and streamer: lets the whole app run, and be tested, with no hardware."""
    name = "demo"

    def __init__(self, speed: float = 1.0, mic_gain_db: float = -10.0, seed: int = 0, latency_s: float = 0.6):
        self.speed = speed
        self.mic_gain_db = mic_gain_db
        self.latency_s = latency_s
        self.rng = np.random.default_rng(seed)
        self._buf = None
        self._fs = 0
        self._pos = 0
        self._lock = threading.Lock()

    def devices(self):
        return [dict(index=1000, name="Demo microphone (2 inputs)", api="demo", inputs=2, outputs=0, rate=44100,
                     default_input=True, default_output=False),
                dict(index=1001, name="Demo speakers (2 outputs)", api="demo", inputs=0, outputs=2, rate=44100,
                     default_input=False, default_output=True)]

    def renderers(self, timeout: float = 0.0):
        return [dict(name="Demo streamer", ip="192.0.2.10", model="Simulated")]

    def resolve(self, ref, kind):
        return 1000 if kind == "input" else 1001

    def make_player(self, cfg, signal, fs, workdir):
        return DemoPlayer(self, signal, fs)

    # --- the simulated room
    def _noise(self, n: int, fs: int, channels: int) -> np.ndarray:
        gain = 10 ** (self.mic_gain_db / 20)
        white = self.rng.normal(0, 1, (n, channels))
        hp = butter(2, 60, "hp", fs=fs, output="sos")
        room = sosfilt(hp, white, axis=0) * (10 ** (-68 / 20)) * gain * 1.1       # background noise above 60 Hz
        rumble = sosfilt(butter(2, 30, "lp", fs=fs, output="sos"), white, axis=0) * (10 ** (-52 / 20)) * gain
        floor = self.rng.normal(0, 10 ** (-95 / 20), (n, channels))              # electronics, independent of gain
        return room + rumble + floor

    def _hear(self, signal: np.ndarray, fs: int) -> np.ndarray:
        """What the microphone picks up when `signal` (frames x 2) is played."""
        out = np.zeros(len(signal))
        for ch, which in ((0, "L"), (1, "R")):
            if np.max(np.abs(signal[:, ch])) > 0:
                out += sosfilt(_panel_system(fs, which), signal[:, ch])
        n_tail = int(0.35 * fs)                                                  # a diffuse, decaying room tail
        ir = np.exp(-np.arange(n_tail) / (0.07 * fs)) * self.rng.normal(0, 1, n_tail)
        ir[0] = 0.0
        ir /= np.sqrt(np.sum(ir ** 2))
        out = out + 0.3 * fftconvolve(out, ir)[:len(out)]
        return out * 10 ** (self.mic_gain_db / 20) * 0.35

    def inject(self, signal: np.ndarray, fs: int):
        """Called by the demo player: the sound arrives at the microphone `latency_s` after the call."""
        with self._lock:
            if self._buf is None:
                return
            heard = self._hear(signal, fs)
            start = self._pos + int(self.latency_s * fs)
            end = min(start + len(heard), len(self._buf))
            if end > start:
                self._buf[start:end, 0] += heard[:end - start]

    # --- streaming and recording
    def stream(self, dev, fs, channels, on_block, cancel):
        bs = int(BLOCK_S * fs)
        t = 0.0
        next_burst = 1.0
        t0 = time.monotonic()
        while not cancel.is_set():
            block = self._noise(bs, fs, channels)
            if t >= next_burst:                                         # now and then a clap, so the meter has something to show
                k = np.arange(bs) / fs
                block[:, 0] += 10 ** ((-30 + self.mic_gain_db / 2) / 20) * np.sin(2 * np.pi * 440 * k) * np.exp(-k * 8)
                next_burst = t + 2.5 + self.rng.random() * 2
            t += bs / fs
            on_block(block, t)
            time.sleep(max(0.0, t0 + t / self.speed - time.monotonic()))

    def record(self, dev, seconds, fs, channels, on_block=None, on_started=None, cancel=None):
        frames = int(seconds * fs)
        with self._lock:
            self._buf = self._noise(frames, fs, channels)
            self._fs, self._pos = fs, 0
        if on_started:
            on_started()
        bs = int(BLOCK_S * fs)
        pos = 0
        t0 = time.monotonic()
        while pos < frames:
            if cancel is not None and cancel.is_set():
                break
            n = min(bs, frames - pos)
            with self._lock:
                block = self._buf[pos:pos + n].copy()
                pos += n
                self._pos = pos
            if on_block:
                on_block(block, pos / fs)
            time.sleep(max(0.0, t0 + pos / fs / self.speed - time.monotonic()))
        with self._lock:
            rec = self._buf[:pos].copy()
            self._buf = None
        return rec
