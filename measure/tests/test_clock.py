import numpy as np
import pytest
from scipy.signal import butter, sosfilt, sosfreqz

from octetmeasure.analysis import measure
from octetmeasure.sweep import make_sweep, retimed

FS = 24000
DUR = 4.0
NOMINAL = make_sweep(FS, DUR, 60, 10000, -18)
SOS = np.vstack([butter(2, 150, "hp", fs=FS, output="sos"), butter(4, 6000, "lp", fs=FS, output="sos")])


def case(ppm, echoes=False, noise=1e-6, seed=0):
    """Record a nominal-length sweep that was actually played ppm longer than nominal."""
    played = make_sweep(FS, DUR * (1 + ppm * 1e-6), 60, 10000, -18)
    n = int((1.5 + DUR + 3.0) * FS)
    x = np.zeros(n)
    x[int(1.5 * FS):int(1.5 * FS) + len(played.signal)] = played.signal
    y = sosfilt(SOS, np.concatenate([np.zeros(200), x])[:n])
    if echoes:                                    # a diffuse, panel-like tail
        y = y + 0.5 * np.roll(y, int(0.002 * FS)) + 0.3 * np.roll(y, int(0.007 * FS))
    return y + np.random.default_rng(seed).normal(0, noise, n)


def truth(f):
    return 20 * np.log10(np.abs(sosfreqz(SOS, worN=2 * np.pi * np.asarray(f) / FS)[1]))


def mag_error(res):
    f = res.response.freq
    sel = (f >= 150) & (f <= 5000)
    return (res.response.mag_db[sel] - truth(f[sel]))


def test_retimed_changes_length_only():
    s = retimed(NOMINAL, 100.0)
    assert len(s.signal) == pytest.approx(len(NOMINAL.signal) * (1 + 1e-4), abs=1.5)
    assert retimed(NOMINAL, 0.0) is NOMINAL


def test_correction_removes_the_bias():
    rec = case(100.0)[:, None]
    wrong = measure(rec, NOMINAL, smoothing=6, post_ms=500, f_lo=100, f_hi=8000, clock_ppm=0.0)
    right = measure(rec, NOMINAL, smoothing=6, post_ms=500, f_lo=100, f_hi=8000, clock_ppm=100.0)
    assert np.abs(mag_error(wrong)).max() > 0.7           # uncorrected: visibly wrong
    assert np.abs(mag_error(right)).max() < 0.5
    assert right.clock_ppm == 100.0


def test_zero_offset_is_unchanged():
    rec = case(0.0)[:, None]
    a = measure(rec, NOMINAL, smoothing=6, post_ms=500, f_lo=100, f_hi=8000)
    b = measure(rec, NOMINAL, smoothing=6, post_ms=500, f_lo=100, f_hi=8000, clock_ppm=0.0)
    assert np.allclose(a.response.mag_db, b.response.mag_db)
    assert np.abs(mag_error(a)).max() < 0.5
