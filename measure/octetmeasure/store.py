"""Measurement storage: one folder per measurement with the raw recording and metadata."""
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from . import paths
from .sweep import Sweep, make_sweep

ROOT = paths.data_dir()


_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def check_name(name: str) -> str:
    """A measurement name that is safe as a folder name on Windows, macOS and Linux. Returns it trimmed."""
    n = (name or "").strip()
    if not n:
        raise ValueError("Give the measurement a name.")
    if len(n) > 80 or _BAD_NAME.search(n) or n.startswith((".", "_")) or n.endswith("."):
        raise ValueError('Use up to 80 characters, without / \\ : * ? " < > |, and do not start with a dot or underscore.')
    return n


def folder(name: str) -> Path:
    return ROOT / name


def save_recording(name: str, rec: np.ndarray, sweep: Sweep, **meta) -> Path:
    d = folder(name)
    d.mkdir(parents=True, exist_ok=True)
    wavfile.write(d / "recording.wav", sweep.fs, rec.astype(np.float32))
    info = dict(name=name, created=datetime.now().isoformat(timespec="seconds"),
                sweep=sweep.params(), **meta)
    (d / "meta.json").write_text(json.dumps(info, indent=2))
    return d


def load_recording(name: str) -> tuple[np.ndarray, Sweep, dict]:
    d = folder(name)
    if not d.exists():
        raise SystemExit(f"No measurement named {name!r} in {ROOT}")
    fs, rec = wavfile.read(d / "recording.wav")
    meta = json.loads((d / "meta.json").read_text())
    return rec.astype(np.float64), make_sweep(**meta["sweep"]), meta


def list_measurements() -> list[str]:
    return sorted(p.name for p in ROOT.iterdir() if (p / "recording.wav").exists()) if ROOT.exists() else []


def save_response_csv(path: Path, resp) -> None:
    header = "freq_hz,mag_db,mag_db_unsmoothed,phase_deg"
    data = np.column_stack([resp.freq, resp.mag_db, resp.mag_db_raw, resp.phase_deg])
    np.savetxt(path, data, delimiter=",", header=header, comments="", fmt="%.4f")


CLOCK_FILE = ROOT / "clock.json"


def get_clock_ppm(player: str | None = None) -> float:
    """Player-vs-recorder clock offset in ppm, as measured with a tone (see clock.py); 0 if never measured.
    `player` is what the recording says it was played from. The offset belongs to one player, so it is applied to
    recordings from that player (or with no player recorded, which is how the first ones were saved) and not to
    the PC's own playback or to anything else."""
    try:
        d = json.loads(CLOCK_FILE.read_text())
        ppm = float(d["ppm"])
    except (OSError, KeyError, ValueError):
        return 0.0
    if player is None:
        return ppm
    who = player.lower()
    if who.startswith(("local", "external")):
        return 0.0
    want = str(d.get("player") or "").lower()
    return ppm if (not want or want in who or who in want) else 0.0


def set_clock_ppm(ppm: float, **info) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    CLOCK_FILE.write_text(json.dumps(dict(ppm=round(float(ppm), 2), saved=datetime.now().isoformat(timespec="seconds"),
                                          **info), indent=2))
