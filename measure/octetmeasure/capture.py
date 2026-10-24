"""Audio I/O via sounddevice (PortAudio): WASAPI on Windows, Core Audio on macOS, ALSA on Linux."""
import subprocess
import sys
import threading
import time

import numpy as np

try:
    import sounddevice as sd
except (ImportError, OSError) as _err:            # PortAudio is missing (common on a fresh Linux install)
    _reason = str(_err)                            # the exception name itself is deleted when this block ends
    _hint = {"linux": "Install it with: sudo apt install libportaudio2 (or your distribution's equivalent).",
             "darwin": "Reinstall with: pip install --force-reinstall sounddevice."}.get(sys.platform, "Reinstall sounddevice.")

    class _NoAudio:
        """Stands in for sounddevice so that everything else, including demo mode, still works."""

        def __getattr__(self, name):
            raise RuntimeError(f"Audio support is not available ({_reason}). {_hint}")

    sd = _NoAudio()

# Host APIs in order of preference on each system.
PREFERRED_APIS = {
    "win32": ["Windows WASAPI", "Windows DirectSound", "MME"],
    "darwin": ["Core Audio"],
    "linux": ["ALSA", "JACK Audio Connection Kit", "PulseAudio", "OSS"],
}


def list_devices() -> list[dict]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        api = sd.query_hostapis(d["hostapi"])["name"]
        out.append(dict(index=i, name=d["name"], api=api, inputs=d["max_input_channels"],
                        outputs=d["max_output_channels"], rate=d["default_samplerate"]))
    return out


def preferred_api() -> str:
    """The host API this system should use, or the first one PortAudio reports."""
    available = [h["name"] for h in sd.query_hostapis()]
    for name in PREFERRED_APIS.get(sys.platform if sys.platform in PREFERRED_APIS else "linux", []):
        if name in available:
            return name
    return available[0] if available else ""


def default_device(kind: str) -> int | None:
    """Index of the system's default input or output device, or None."""
    idx = sd.default.device[0 if kind == "input" else 1]
    key = "inputs" if kind == "input" else "outputs"
    devs = list_devices()
    if idx is not None and 0 <= idx < len(devs) and devs[idx][key] > 0:
        return int(idx)
    api = preferred_api()
    for d in devs:
        if d[key] > 0 and d["api"] == api:
            return d["index"]
    return None


def find_device(name: str, kind: str, api: str | None = None) -> int:
    """Index of the device whose name contains `name` (case-insensitive) on the preferred host API.
    An empty name means the system default."""
    key = "inputs" if kind == "input" else "outputs"
    if not name:
        idx = default_device(kind)
        if idx is None:
            raise RuntimeError(f"No {kind} device found. Run `devices` to list them.")
        return idx
    api = api or preferred_api()
    hits = [d for d in list_devices()
            if name.lower() in d["name"].lower() and d["api"] == api and d[key] > 0]
    if not hits:
        raise RuntimeError(f"No {kind} device matching {name!r} on {api}. Run `devices` to list them.")
    return hits[0]["index"]


def _settings(device: int):
    """WASAPI shared mode needs auto_convert to accept a rate other than the device's current one."""
    api = sd.query_hostapis(sd.query_devices(device)["hostapi"])["name"]
    wasapi = getattr(sd, "WasapiSettings", None)
    return wasapi(auto_convert=True) if (wasapi and api == "Windows WASAPI") else None


def record(in_dev: int, seconds: float, fs: int, channels: int = 2,
           trigger_cmd=None, trigger_delay: float = 1.0) -> np.ndarray:
    """Record for `seconds`. `trigger_cmd` runs `trigger_delay` s after recording starts: either a shell
    command (e.g. an ssh command that starts playback on the Pi) or a Python callable."""
    frames = int(seconds * fs)
    proc = {}

    def fire():
        time.sleep(trigger_delay)
        if callable(trigger_cmd):
            try:
                trigger_cmd()
                proc["p"] = subprocess.CompletedProcess([], 0, "", "")
            except Exception as e:  # reported after the recording finishes
                proc["p"] = subprocess.CompletedProcess([], 1, "", f"{type(e).__name__}: {e}")
        else:
            proc["p"] = subprocess.run(trigger_cmd, shell=True, capture_output=True, text=True)

    rec = sd.rec(frames, samplerate=fs, channels=channels, dtype="float32",
                 device=in_dev, extra_settings=_settings(in_dev))
    t = None
    if trigger_cmd:
        t = threading.Thread(target=fire)
        t.start()
    sd.wait()
    if t:
        t.join()
        p = proc["p"]
        if p.returncode != 0:
            raise RuntimeError(f"Trigger command failed ({p.returncode}): {p.stderr.strip()}")
    return rec.astype(np.float64)


def play_and_record(in_dev: int, out_dev: int, signal: np.ndarray, fs: int,
                    in_channels: int = 2, out_channel: int = 0, tail_s: float = 1.0) -> np.ndarray:
    """Play `signal` from one output channel while recording. For self-tests and loopback checks."""
    out_channels = sd.query_devices(out_dev)["max_output_channels"]
    play = np.zeros((len(signal) + int(tail_s * fs), out_channels), dtype=np.float32)
    play[:len(signal), out_channel] = signal
    rec = sd.playrec(play, samplerate=fs, channels=in_channels, dtype="float32",
                     device=(in_dev, out_dev), extra_settings=(_settings(in_dev), _settings(out_dev)))
    sd.wait()
    return rec.astype(np.float64)


def band_levels(x: np.ndarray, fs: int, f_lo: float = 60.0) -> tuple[float, float]:
    """Peak and RMS in dBFS of the signal above f_lo, ignoring room rumble that fools a plain meter."""
    from scipy.signal import butter, sosfilt
    y = sosfilt(butter(4, f_lo, "hp", fs=fs, output="sos"), x)
    return (20 * np.log10(max(np.max(np.abs(y)), 1e-9)),
            20 * np.log10(max(np.sqrt(np.mean(y ** 2)), 1e-9)))


def level_check(in_dev: int, fs: int, seconds: float = 3.0, channels: int = 2) -> list[dict]:
    """Per channel: broadband and above-60-Hz peak/RMS in dBFS."""
    rec = record(in_dev, seconds, fs, channels)
    out = []
    for c in range(channels):
        x = rec[:, c]
        pk, rms = 20 * np.log10(max(np.max(np.abs(x)), 1e-9)), 20 * np.log10(max(np.sqrt(np.mean(x ** 2)), 1e-9))
        bpk, brms = band_levels(x, fs)
        out.append(dict(peak=pk, rms=rms, band_peak=bpk, band_rms=brms))
    return out
