"""Exponential sine sweep (Farina) generation with a matching inverse filter."""
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Sweep:
    fs: int
    duration: float
    f1: float
    f2: float
    level_dbfs: float
    signal: np.ndarray    # what gets played, scaled to level_dbfs
    inverse: np.ndarray   # convolve a recording with this to get the impulse response

    @property
    def level_lin(self) -> float:
        return 10 ** (self.level_dbfs / 20)

    def params(self) -> dict:
        return dict(fs=self.fs, duration=self.duration, f1=self.f1, f2=self.f2,
                    level_dbfs=self.level_dbfs)


def make_sweep(fs: int = 48000, duration: float = 10.0, f1: float = 20.0,
               f2: float = 20000.0, level_dbfs: float = -12.0,
               fade_in: float = 0.05, fade_out: float = 0.005) -> Sweep:
    n = int(round(duration * fs))
    t = np.arange(n) / fs
    big_l = np.log(f2 / f1)
    x = np.sin(2 * np.pi * f1 * duration / big_l * (np.exp(t / duration * big_l) - 1))

    # Inverse filter: time-reversed sweep with a -6 dB/oct envelope.
    inverse = x[::-1] * np.exp(-t / duration * big_l)

    # Fades reduce clicks at the band edges. Applied after the inverse is built.
    xs = x.copy()
    n_in, n_out = int(fade_in * fs), int(fade_out * fs)
    if n_in:
        xs[:n_in] *= 0.5 * (1 - np.cos(np.pi * np.arange(n_in) / n_in))
    if n_out:
        xs[-n_out:] *= 0.5 * (1 + np.cos(np.pi * np.arange(n_out) / n_out))

    # Scale the inverse so that sweep (x) inverse has unit gain in mid-band.
    nfft = 2 * n
    prod = np.fft.rfft(xs, nfft) * np.fft.rfft(inverse, nfft)
    f = np.fft.rfftfreq(nfft, 1 / fs)
    band = (f >= max(200.0, 2 * f1)) & (f <= min(5000.0, f2 / 2))
    if np.count_nonzero(band) < 8:
        # A sub-only or tweeter-only sweep has no conventional midrange band.
        lo, hi = np.exp(np.log(f1) + np.array([.3, .7]) * np.log(f2 / f1))
        band = (f >= lo) & (f <= hi)
    inverse = inverse / np.median(np.abs(prod[band]))

    return Sweep(fs, duration, f1, f2, level_dbfs,
                 xs * 10 ** (level_dbfs / 20), inverse)


def padded_playback(sweep: Sweep, lead_s: float = 2.0, tail_s: float = 3.0) -> np.ndarray:
    """Sweep with silence before and after, for playing from a file."""
    lead = np.zeros(int(lead_s * sweep.fs))
    tail = np.zeros(int(tail_s * sweep.fs))
    return np.concatenate([lead, sweep.signal, tail])


def stereo_playback(sweep: Sweep, channel: str = "both", lead_s: float = 2.0, tail_s: float = 3.0,
                    invert: str = "none") -> np.ndarray:
    """(frames, 2) array with the sweep on the left, right or both channels and silence on the other.
    invert flips the polarity of the named channel ("left", "right" or "both") to test exciter phasing."""
    if channel not in ("left", "right", "both"):
        raise ValueError("channel must be left, right or both")
    if invert not in ("none", "left", "right", "both"):
        raise ValueError("invert must be none, left, right or both")
    y = padded_playback(sweep, lead_s, tail_s)
    out = np.zeros((len(y), 2))
    if channel in ("left", "both"):
        out[:, 0] = -y if invert in ("left", "both") else y
    if channel in ("right", "both"):
        out[:, 1] = -y if invert in ("right", "both") else y
    return out


def retimed(sweep: Sweep, ppm: float) -> Sweep:
    """The sweep as it arrives when the player's clock differs from the recorder's: ppm > 0 means the played
    sweep lasts that many parts per million longer than nominal. Deconvolving with this version keeps the impulse
    response sharp; with the nominal sweep a few tens of ppm already smear the high frequencies."""
    if ppm == 0:
        return sweep
    return make_sweep(sweep.fs, sweep.duration * (1 + ppm * 1e-6), sweep.f1, sweep.f2, sweep.level_dbfs)
