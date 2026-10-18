"""Microphone calibration files kept by the app: list, import, inspect, choose the active one."""
import re
from pathlib import Path

import numpy as np

from . import paths, store
from .cal import MicCal, load_cal

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def folder() -> Path:
    return store.ROOT / "_calibration"


def _path(name: str) -> Path:
    clean = _SAFE.sub("_", Path(name).name).strip("._") or "calibration"
    if not clean.lower().endswith((".txt", ".cal", ".frd")):
        clean += ".txt"
    return folder() / clean


def names() -> list[str]:
    return sorted(p.name for p in folder().glob("*") if p.is_file()) if folder().exists() else []


def active_name() -> str | None:
    n = paths.load_config().get("active_cal")
    return n if n in names() else None


def set_active(name: str | None) -> None:
    if name is not None and name not in names():
        raise ValueError(f"No calibration file named {name!r}")
    paths.save_config(active_cal=name)


def load(name: str | None) -> MicCal | None:
    if not name:
        return None
    p = folder() / Path(name).name
    return load_cal(p) if p.exists() else None


def load_active() -> MicCal | None:
    return load(active_name())


def save(name: str, text: str) -> str:
    """Store a calibration file. Raises ValueError if it holds no frequency/dB data."""
    p = _path(name)
    folder().mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        load_cal(tmp)
    except ValueError:
        tmp.unlink(missing_ok=True)
        raise ValueError("That file has no calibration data. Expected lines of frequency and dB values.")
    tmp.replace(p)
    return p.name


def delete(name: str) -> None:
    p = folder() / Path(name).name
    if p.exists():
        p.unlink()
    if active_name() is None and paths.load_config().get("active_cal") == name:
        paths.save_config(active_cal=None)


def info(name: str, max_points: int = 400) -> dict:
    p = folder() / Path(name).name
    cal = load_cal(p)
    text = p.read_text(encoding="utf-8", errors="replace")
    header = [ln.strip() for ln in text.splitlines()
              if ln.strip() and not re.match(r"^\s*[-+]?\d", ln)]
    idx = np.unique(np.linspace(0, len(cal.freq) - 1, min(max_points, len(cal.freq))).astype(int))
    serial = re.search(r"SERNO:?\s*(\w+)", text, re.I) or re.search(r"(\d{4,})", p.stem)
    return dict(name=p.name, points=len(cal.freq), f_min=float(cal.freq[0]), f_max=float(cal.freq[-1]),
                sens_db=cal.sens_factor_db, header=header, serial=serial.group(1) if serial else None,
                max_dev_db=float(np.max(np.abs(cal.db))), active=p.name == active_name(),
                f=[round(float(v), 2) for v in cal.freq[idx]], db=[round(float(v), 2) for v in cal.db[idx]],
                text=text)


def listing() -> list[dict]:
    out = []
    for n in names():
        try:
            i = info(n, max_points=2)
            out.append({k: i[k] for k in ("name", "points", "f_min", "f_max", "sens_db", "serial", "max_dev_db", "active")})
        except (ValueError, OSError):
            out.append(dict(name=n, points=0, f_min=None, f_max=None, sens_db=None, serial=None, max_dev_db=None,
                            active=False, error="Could not read this file"))
    return out
