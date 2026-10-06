"""Clock offset between a player and the recorder, measured from a pure tone.

A tone's frequency cannot be changed by the speaker or the room, so the frequency the recorder sees against the
nominal one is the ratio of the two clocks. The same ratio stretches a swept sine, and a few tens of ppm is enough to
smear the top of the impulse response; analysis corrects for it with `retimed` once the offset is known.
"""
import numpy as np


def make_tone(fs: int, f0: float = 1000.0, seconds: float = 14.0, level_dbfs: float = -24.0, fade_s: float = 0.5):
    n = int(seconds * fs)
    t = np.arange(n) / fs
    x = np.sin(2 * np.pi * f0 * t)
    k = int(fade_s * fs)
    ramp = 0.5 * (1 - np.cos(np.pi * np.arange(k) / k))
    x[:k] *= ramp
    x[-k:] *= ramp[::-1]
    return x * 10 ** (level_dbfs / 20)


def tone_ppm(x: np.ndarray, fs: int, f0: float = 1000.0, block_s: float = 0.25):
    """How many ppm longer (positive) the played material runs than nominal, from a recording of a steady tone.
    Returns (ppm, spread_ppm): the offset from a straight-line fit of the tone's phase, and the difference between
    fits to its first and second halves (a check that the tone really was steady)."""
    t = np.arange(len(x)) / fs
    z = x * np.exp(-2j * np.pi * f0 * t)
    nb = int(block_s * fs)
    nblk = len(x) // nb
    zb = z[:nblk * nb].reshape(nblk, nb).mean(axis=1)
    ph = np.unwrap(np.angle(zb))
    tb = (np.arange(nblk) + 0.5) * block_s

    def ppm(sl):
        slope_hz = np.polyfit(tb[sl], ph[sl], 1)[0] / (2 * np.pi)
        return (f0 / (f0 + slope_hz) - 1) * 1e6

    half = nblk // 2
    full = ppm(slice(0, nblk))
    return float(full), float(abs(ppm(slice(0, half)) - ppm(slice(half, nblk))))


def find_tone(x: np.ndarray, fs: int, f0: float = 1000.0, trim_s: float = 1.0):
    """The steady part of a recorded tone: where its level is within 6 dB of the maximum, minus trim_s at each end."""
    t = np.arange(len(x)) / fs
    z = x * np.exp(-2j * np.pi * f0 * t)
    nb = int(0.1 * fs)
    nblk = len(x) // nb
    env = np.abs(z[:nblk * nb].reshape(nblk, nb).mean(axis=1))
    on = np.where(env > env.max() / 2)[0]
    a = int((on[0] * 0.1 + trim_s) * fs)
    b = int(((on[-1] + 1) * 0.1 - trim_s) * fs)
    return x[a:b]
