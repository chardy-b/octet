"""Microphone calibration file parsing (Dayton Audio / REW style text files)."""
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

_NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")


@dataclass(frozen=True)
class MicCal:
    freq: np.ndarray
    db: np.ndarray
    sens_factor_db: float | None = None
    header: str = ""

    def at(self, f: np.ndarray) -> np.ndarray:
        """Mic deviation in dB at frequencies f (clamped outside the file's range)."""
        return np.interp(f, self.freq, self.db)


def load_cal(path: str | Path) -> MicCal:
    freqs, dbs, header = [], [], []
    sens = None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parts = [p for p in re.split(r"[\s,;]+", line.strip()) if p]
        if len(parts) >= 2 and _NUM.match(parts[0]) and _NUM.match(parts[1]):
            freqs.append(float(parts[0]))
            dbs.append(float(parts[1]))
        elif line.strip():
            header.append(line.strip())
            m = re.search(r"Sens\s*Factor\s*=\s*([-+]?\d+\.?\d*)", line, re.I)
            if not m:  # Dayton style: "*1000Hz<TAB>-39.8" = sensitivity in dBV/Pa at 1 kHz
                m = re.match(r"\*\s*1000\s*Hz\s+([-+]?\d+\.?\d*)", line.strip(), re.I)
            if m:
                sens = float(m.group(1))
    if len(freqs) < 2:
        raise ValueError(f"No calibration data found in {path}")
    order = np.argsort(freqs)
    return MicCal(np.array(freqs)[order], np.array(dbs)[order], sens, " | ".join(header))
