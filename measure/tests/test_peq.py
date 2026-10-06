import numpy as np
import pytest

from octetmeasure.peq import Peq, biquad_db, eq_db, fit_peq, harman_target, smooth_log

FS = 48000


def test_biquad_shapes():
    f = np.array([20.0, 1000.0, 20000.0])
    pk = biquad_db(f, FS, Peq("PK", 1000, 6.0, 2.0))
    assert pk[1] == pytest.approx(6.0, abs=0.05) and abs(pk[0]) < 0.1
    ls = biquad_db(f, FS, Peq("LS", 200, 5.0, 0.707))
    assert ls[0] == pytest.approx(5.0, abs=0.2) and abs(ls[2]) < 0.2
    hs = biquad_db(f, FS, Peq("HS", 4000, -4.0, 0.707))
    assert hs[2] == pytest.approx(-4.0, abs=0.3) and abs(hs[0]) < 0.1


def test_harman_target_shape():
    t = harman_target(np.array([20.0, 50.0, 105.0, 1000.0, 14000.0]))
    assert t[0] == pytest.approx(4.0, abs=0.2) and t[1] == pytest.approx(3.6, abs=0.5)
    assert t[2] == pytest.approx(2.0, abs=0.3)             # half the shelf at the corner
    assert t[3] == pytest.approx(0.0, abs=0.1)
    assert -2.0 < t[4] < -0.5                               # gentle treble roll-off


def test_fit_recovers_known_filters():
    f = 100 * 2 ** (np.arange(0, 7.4 * 24) / 24)             # 100 Hz .. ~16 kHz
    truth = [Peq("PK", 250, -6.0, 2.0), Peq("PK", 1200, 3.0, 1.5), Peq("PK", 5000, -5.0, 3.0)]
    target = np.zeros_like(f)
    measured = target - eq_db(f, FS, truth)                  # a response that the truth filters would flatten
    filters, info = fit_peq(f, measured, target, FS, n_bands=6, smooth=12)
    assert info["rms_after"] < 0.35 * info["rms_before"]
    assert info["rms_after"] < 0.7
    assert len(filters) <= 6


def test_limits_are_respected():
    f = 100 * 2 ** (np.arange(0, 7.4 * 24) / 24)
    measured = np.zeros_like(f)
    measured[(f > 800) & (f < 1500)] = -20.0                  # a deep dip: boosting it fully would be foolish
    filters, _ = fit_peq(f, measured, np.zeros_like(f), FS, max_boost=4.0, max_cut=6.0)
    assert all(-6.0 <= p.gain_db <= 4.0 for p in filters)
    curve = eq_db(f, FS, filters)
    assert curve.max() <= 4.0 + 0.7 and curve.min() >= -6.0 - 0.7        # the combined curve respects the limits too
    assert all(100 <= p.fc <= 16000 and 0.5 <= p.q <= 6.0 for p in filters)


def test_smooth_log_flat():
    f = np.geomspace(100, 16000, 200)
    assert np.allclose(smooth_log(f, np.full_like(f, 3.0), 6), 3.0)


def test_no_stacked_duplicates():
    f = 100 * 2 ** (np.arange(0, 7.4 * 24) / 24)
    measured = -10.0 * np.exp(-0.5 * (np.log2(f / 300) / 0.3) ** 2)       # one broad dip, bigger than a single boost allows
    filters, _ = fit_peq(f, measured, np.zeros_like(f), FS, max_boost=4.0)
    fl = sorted(filters, key=lambda p: p.fc)
    # an adjacent boost and cut can legitimately shape a curve; two filters of the same sign must not stack
    assert all(np.log2(b.fc / a.fc) > 0.2 for a, b in zip(fl, fl[1:]) if np.sign(a.gain_db) == np.sign(b.gain_db))
