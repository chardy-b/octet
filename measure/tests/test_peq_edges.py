import numpy as np
import pytest

from octetmeasure.peq import Peq, apo_text, biquad_db, eq_db, fit_peq, rolloff_freq

FS = 48000


def test_highpass_and_lowpass_shapes():
    f = np.array([25.0, 50.0, 100.0, 200.0, 1000.0])
    hp = biquad_db(f, FS, Peq("HP", 100, 0.0, 0.7071))
    assert hp[2] == pytest.approx(-3.0, abs=0.2)                 # -3 dB at the corner
    assert hp[1] == pytest.approx(-12.0, abs=1.0) and abs(hp[4]) < 0.1   # 12 dB/oct below, flat above
    lp = biquad_db(np.array([100.0, 3000.0, 6000.0, 12000.0]), FS, Peq("LP", 3000, 0.0, 0.7071))
    assert lp[1] == pytest.approx(-3.0, abs=0.2) and abs(lp[0]) < 0.1
    assert lp[3] < -20


def test_rolloff_freq_finds_corners():
    f = np.geomspace(60, 20000, 400)
    flat = np.zeros_like(f)
    shaped = flat + biquad_db(f, FS, Peq("HP", 150, 0.0, 0.7071)) + biquad_db(f, FS, Peq("LP", 4000, 0.0, 0.7071))
    lo = rolloff_freq(f, shaped, "low", 10.0)
    hi = rolloff_freq(f, shaped, "high", 10.0)
    assert 80 < lo < 150            # 10 dB down for a 2nd-order high-pass at 150 Hz sits well below the corner
    assert 4000 < hi < 9000
    assert rolloff_freq(f, flat, "low", 10.0) is None and rolloff_freq(f, flat, "high", 10.0) is None


def test_fit_accounts_for_fixed_highpass():
    f = 100 * 2 ** (np.arange(0, 7.4 * 24) / 24)
    hp = Peq("HP", 120, 0.0, 0.71)
    measured = np.zeros_like(f)
    filters, info = fit_peq(f, measured, np.zeros_like(f), FS, fixed=[hp])
    # the fixed high-pass already droops the low end; the fitter should use boosts there to compensate, not ignore it
    assert info["rms_after"] <= info["rms_before"] + 1e-9
    assert any(p.fc < 300 and p.gain_db > 0 for p in filters)


def test_apo_text_edge_filters():
    text = apo_text([Peq("HP", 100, 0.0, 0.71), Peq("PK", 300, -8.0, 1.9), Peq("LP", 2800, 0.0, 0.71)], -1.5)
    lines = text.splitlines()
    assert lines[0] == "Preamp: -1.5 dB"
    assert lines[1] == "Filter 1: ON HPQ Fc 100 Hz Q 0.71"
    assert lines[2] == "Filter 2: ON PK Fc 300 Hz Gain -8.0 dB Q 1.9"
    assert lines[3] == "Filter 3: ON LPQ Fc 2800 Hz Q 0.71"
