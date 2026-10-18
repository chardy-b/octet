"""Running a measurement, a level test, a noise check or a clock check, reporting progress as events.

Everything the web app does with the microphone goes through here, so it behaves the same with real hardware and
with the simulated one. `emit(event_dict)` receives events of type: state, level, player, done, error.
"""
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

from . import analysis, calibration, clock, levels, store
from .sweep import make_sweep, stereo_playback


@dataclass
class RecordConfig:
    name: str = "measurement"
    kind: str = "sweep"              # sweep | level_test
    fs: int = 44100
    duration: float = 10.0
    f1: float = 60.0
    f2: float = 20000.0
    level: float = -18.0
    player: str = "dlna"             # dlna | local | manual | demo
    renderer: str = ""
    in_device: str = ""
    out_device: str = ""
    mic_ch: int = 0
    channels: int = 2
    channel: str = "both"            # left | right | both
    invert: str = "none"             # none | left | right | both
    bits: int = 24
    seconds: float | None = None
    trigger: str | None = None
    trigger_delay: float = 1.0
    ref: bool = False
    note: str = ""
    save: bool = True
    repeat: int = 2                  # clock check only
    tone: float = 1000.0             # clock check only

    @classmethod
    def from_dict(cls, d: dict) -> "RecordConfig":
        kinds = {f.name: f.type for f in fields(cls)}
        out = {}
        for k, v in d.items():
            if k not in kinds or v is None:
                continue
            t = str(kinds[k])
            out[k] = (int(v) if "int" in t and "float" not in t else float(v) if "float" in t else
                      bool(v) if "bool" in t else str(v))
        return cls(**out)

    def validate(self) -> None:
        if self.kind == "sweep" and self.save:
            store.check_name(self.name)
        if self.player not in ("dlna", "local", "manual", "demo"):
            raise ValueError("Choose how the sweep will be played.")
        if self.player == "dlna" and not self.renderer:
            raise ValueError("Choose a network player.")
        if not 0.5 <= self.duration <= 60:
            raise ValueError("The sweep should last between 0.5 and 60 seconds.")
        if not 10 <= self.f1 < self.f2 <= self.fs / 2:
            raise ValueError("The sweep range must sit between 10 Hz and half the sample rate.")
        if not -60 <= self.level <= -3:
            raise ValueError("Sweep level must be between -60 and -3 dBFS.")
        if self.channel not in ("left", "right", "both") or self.invert not in ("none", "left", "right", "both"):
            raise ValueError("Channel or polarity setting is not valid.")
        if not 0 <= self.mic_ch < self.channels:
            raise ValueError("The microphone channel is outside the recorded channels.")


def default_seconds(cfg: RecordConfig) -> float:
    if cfg.seconds:
        return cfg.seconds
    return cfg.duration + {"dlna": 16.0, "local": 6.0, "demo": 9.0, "manual": 15.0}[cfg.player]


class Session:
    def __init__(self, io, emit, cancel: threading.Event | None = None):
        self.io = io
        self.emit = emit
        self.cancel = cancel or threading.Event()

    def _ev(self, type_, **kw):
        self.emit(dict(type=type_, **kw))

    def _state(self, state, message=""):
        self._ev("state", state=state, message=message)

    # ------------------------------------------------------------ recording with playback
    def _capture(self, cfg: RecordConfig, signal: np.ndarray | None, seconds: float, label: str):
        """Record `seconds`, starting playback of `signal` a moment after recording begins."""
        io = self.io
        dev = io.resolve(cfg.in_device, "input")
        meter = levels.BlockMeter(cfg.fs, cfg.channels)
        problems: list[str] = []
        player = io.make_player(cfg, signal, cfg.fs, Path(tempfile.mkdtemp(prefix="octet_"))) if signal is not None else None
        if player is not None:
            self._ev("player", label=player.label)
        trigger_thread: list[threading.Thread] = []

        def fire():
            time.sleep(cfg.trigger_delay / max(getattr(io, "speed", 1.0), 1e-3))
            if self.cancel.is_set():
                return
            try:
                self._state("playing", f"Playing {label} on {player.label}")
                player.start()
            except Exception as e:                                                  # reported once recording ends
                problems.append(f"{type(e).__name__}: {e}")

        def on_started():
            if player is not None:
                th = threading.Thread(target=fire, daemon=True)
                th.start()
                trigger_thread.append(th)

        def on_block(block, t):
            self._ev("level", t=round(t, 3), total=seconds, ch=meter.process(block), mic=cfg.mic_ch)

        self._state("recording", f"Recording for {seconds:.0f} seconds")
        try:
            rec = io.record(dev, seconds, cfg.fs, cfg.channels, on_block, on_started, self.cancel)
        finally:
            for th in trigger_thread:
                th.join(timeout=5)
            if player is not None:
                try:
                    player.stop()
                except Exception:
                    pass
        fetched = player.fetched() if player is not None else True
        if player is not None:
            player.close()
        if problems:
            raise RuntimeError(problems[0])
        return rec, fetched

    # ------------------------------------------------------------ the four jobs
    def run_record(self, cfg: RecordConfig) -> dict | None:
        try:
            if cfg.kind == "level_test":
                cfg = RecordConfig.from_dict({**asdict(cfg), "duration": 4.0, "save": False})
            cfg.validate()
            self._state("preparing", "Preparing the sweep")
            sw = make_sweep(cfg.fs, cfg.duration, cfg.f1, cfg.f2, cfg.level)
            lead, tail = (0.5, 1.0) if cfg.player == "local" else (2.0, 3.0)
            signal = stereo_playback(sw, cfg.channel, lead, tail, cfg.invert)
            seconds = default_seconds(cfg)
            rec, fetched = self._capture(cfg, signal, seconds, "the sweep")
            if self.cancel.is_set():
                self._state("cancelled", "Stopped")
                return None
            if cfg.player == "dlna" and not fetched:
                raise RuntimeError("The player never fetched the sweep. Check that this computer and the player are on "
                                   "the same network, and that the firewall allows this app on private networks.")

            summary = levels.summarize(rec, cfg.fs, cfg.mic_ch)
            result = dict(kind=cfg.kind, band_peak=summary["band_peak"], peak=summary["peak"],
                          band_rms=summary["band_rms"], advice=levels.advice(summary["band_peak"]),
                          seconds=round(len(rec) / cfg.fs, 1), saved=False)
            if cfg.kind == "sweep" and cfg.save:
                self._state("analyzing", "Saving and analysing")
                player_label = {"local": "local", "manual": "external", "demo": "Demo streamer"}.get(cfg.player, cfg.renderer)
                store.save_recording(cfg.name, rec, sw, in_device=cfg.in_device, ref_ch=1 if cfg.ref else None,
                                     mic_ch=cfg.mic_ch, note=cfg.note, channel=cfg.channel, invert=cfg.invert,
                                     player=player_label)
                result.update(name=cfg.name, saved=True)
                result.update(self._quick_analysis(cfg.name))
            self._ev("done", result=result)
            return result
        except Exception as e:
            self._ev("error", message=_friendly(e))
            return None

    def _quick_analysis(self, name: str) -> dict:
        from . import service
        try:
            a = service.analyze(name, service.AnalysisOptions())
            return dict(snr_db=a["snr_db"], snr_worst=a["snr_worst"], clipped=a["clipped"], clock_ppm=a["clock_ppm"],
                        delay_ms=a["delay_ms"])
        except Exception as e:
            return dict(analysis_error=_friendly(e))

    def run_noise(self, cfg: RecordConfig) -> dict | None:
        try:
            self._state("recording", "Listening to the room, stay quiet")
            rec, _ = self._capture(cfg, None, 4.0, "")
            if self.cancel.is_set():
                self._state("cancelled", "Stopped")
                return None
            s = levels.summarize(rec, cfg.fs, cfg.mic_ch)
            result = dict(kind="noise", band_rms=s["band_rms"], rms=s["rms"], peak=s["peak"],
                          rating=levels.noise_rating(s["band_rms"]))
            self._ev("done", result=result)
            return result
        except Exception as e:
            self._ev("error", message=_friendly(e))
            return None

    def run_monitor(self, cfg: RecordConfig) -> None:
        try:
            io = self.io
            dev = io.resolve(cfg.in_device, "input")
            meter = levels.BlockMeter(cfg.fs, cfg.channels)
            self._state("monitoring", "Listening")
            io.stream(dev, cfg.fs, cfg.channels,
                      lambda block, t: self._ev("level", t=round(t, 3), total=None, ch=meter.process(block), mic=cfg.mic_ch),
                      self.cancel)
            self._state("idle", "")
        except Exception as e:
            self._ev("error", message=_friendly(e))

    def run_clock(self, cfg: RecordConfig, save: bool = True) -> dict | None:
        try:
            cfg.validate()
            fs = cfg.fs
            tone = clock.make_tone(fs, cfg.tone, 14.0, -24.0)
            sig = np.concatenate([np.zeros(int(1.5 * fs)), tone, np.zeros(int(1.0 * fs))])
            signal = np.column_stack([sig, sig])
            results, label = [], cfg.renderer or cfg.player
            for k in range(cfg.repeat):
                self._state("recording", f"Tone {k + 1} of {cfg.repeat}")
                rec, _ = self._capture(cfg, signal, 14.0 + 14.0, "a test tone")
                if self.cancel.is_set():
                    self._state("cancelled", "Stopped")
                    return None
                x = clock.find_tone(rec[:, cfg.mic_ch], fs, cfg.tone)
                ppm, spread = clock.tone_ppm(x, fs, cfg.tone)
                results.append(ppm)
            med = float(np.median(results))
            if save:
                store.set_clock_ppm(med, player=label, source=f"tone {cfg.tone:g} Hz", runs=[round(v, 2) for v in results])
            result = dict(kind="clock", ppm=round(med, 2), runs=[round(v, 2) for v in results], player=label, saved=save,
                          spread=round(max(results) - min(results), 2))
            self._ev("done", result=result)
            return result
        except Exception as e:
            self._ev("error", message=_friendly(e))
            return None


def _friendly(e: Exception) -> str:
    """A message a person can act on."""
    msg = str(e)
    if isinstance(e, SystemExit):
        return msg or "Could not find that audio device."
    low = msg.lower()
    if "no renderer matching" in low:
        return "Could not find that network player. Check that it is on, and on the same network as this computer."
    if "invalid sample rate" in low or "paerrorcode -9997" in low:
        return "The audio device did not accept that sample rate. Try 48000 or 44100."
    if "device unavailable" in low or "paerrorcode -9985" in low:
        return "The audio device is busy or unavailable. Close other programs that use it and try again."
    if "invalid number of channels" in low or "-9998" in low:
        return "The audio device does not have that many input channels."
    return msg or type(e).__name__
