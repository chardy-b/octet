"""Level metering, and the ranges a recording should land in.

The number that matters is the peak of the sweep as recorded, measured above 60 Hz. Room rumble below that can dominate
a plain meter without carrying any of the measurement, so every reading here is given both ways.
"""
import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

FLOOR_DB = -120.0
TARGET = (-20.0, -6.0)          # recorded sweep peak, dBFS: plenty of signal, room to spare
TARGET_CENTRE = -12.0

# (id, lower edge, upper edge, label, tone) - tone is what the interface colours it
ZONES = [
    ("too_low", FLOOR_DB, -40.0, "Too quiet", "bad"),
    ("low", -40.0, -30.0, "Quiet", "warn"),
    ("ok", -30.0, -20.0, "Usable", "ok"),
    ("target", -20.0, -6.0, "Target range", "good"),
    ("hot", -6.0, -2.0, "Hot", "warn"),
    ("clip", -2.0, 0.0, "Clipping risk", "bad"),
]


def to_db(x: float) -> float:
    return float(max(20 * np.log10(max(float(x), 1e-12)), FLOOR_DB))


def zone_of(peak_db: float) -> str:
    for zid, lo, hi, _, _ in ZONES:
        if lo <= peak_db < hi:
            return zid
    return "clip"


def advice(peak_db: float) -> dict:
    """What to do about a recorded sweep peak (dBFS, above 60 Hz)."""
    zid = zone_of(peak_db)
    z = next(z for z in ZONES if z[0] == zid)
    change = round(TARGET_CENTRE - peak_db)
    if zid == "target":
        msg = "In the target range. Leave the gain and the volume where they are."
    elif zid in ("too_low", "low", "ok"):
        msg = f"Raise the mic gain by about {change} dB, or turn the speaker up a little. More signal means a cleaner measurement."
    elif zid == "hot":
        msg = f"Close to clipping. Lower the mic gain by about {-change} dB."
    else:
        msg = f"This will clip. Lower the mic gain by about {-change} dB before recording."
    return dict(zone=zid, label=z[3], tone=z[4], change_db=change, message=msg)


def noise_rating(band_rms_db: float) -> dict:
    """How quiet the room is, from the RMS above 60 Hz with nothing playing."""
    if band_rms_db < -65:
        return dict(rating="quiet", tone="good", message="Quiet room. Good for measuring.")
    if band_rms_db < -55:
        return dict(rating="ok", tone="ok", message="Some background noise. Fine for most measurements.")
    return dict(rating="noisy", tone="warn",
                message="Noisy. Turn off fans and air conditioning, or move the mic away from the noise.")


class BlockMeter:
    """Peak and RMS per channel for each block of audio, in dBFS, plain and above 60 Hz."""

    def __init__(self, fs: int, channels: int, f_hp: float = 60.0):
        self.sos = butter(4, f_hp, "hp", fs=fs, output="sos")
        self.zi = [sosfilt_zi(self.sos) * 0.0 for _ in range(channels)]

    def process(self, block: np.ndarray) -> list[dict]:
        out = []
        for c in range(block.shape[1]):
            x = block[:, c].astype(np.float64)
            y, self.zi[c] = sosfilt(self.sos, x, zi=self.zi[c])
            out.append(dict(peak=to_db(np.max(np.abs(x))), rms=to_db(np.sqrt(np.mean(x ** 2))),
                            band_peak=to_db(np.max(np.abs(y))), band_rms=to_db(np.sqrt(np.mean(y ** 2)))))
        return out


def summarize(rec: np.ndarray, fs: int, channel: int = 0, f_hp: float = 60.0) -> dict:
    """Peak and RMS of a finished recording, plain and above f_hp."""
    m = BlockMeter(fs, rec.shape[1], f_hp).process(rec)[channel]
    return m
