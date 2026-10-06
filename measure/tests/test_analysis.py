import numpy as np
import pytest
from scipy.signal import butter, sosfilt

from octetmeasure.analysis import frequency_response, measure, smooth_octave
from octetmeasure.cal import load_cal
from octetmeasure.sweep import make_sweep

FS = 48000


@pytest.fixture(scope="module")
def sweep():
    return make_sweep(FS, duration=6.0)


def simulate(sweep, delay, gain=1.0, sos=None, noise_db=None, tail=3.0, lead=1.0, seed=0):
    """Play the sweep through a fake system and return the recording."""
    n = int((lead + sweep.duration + tail) * FS)
    x = np.zeros(n)
    s = int(lead * FS)
    x[s:s + len(sweep.signal)] = sweep.signal
    y = sosfilt(sos, x) if sos is not None else x
    y = gain * np.concatenate([np.zeros(delay), y])[:n]
    if noise_db is not None:
        y = y + np.random.default_rng(seed).normal(0, 10 ** (noise_db / 20), n)
    return y


def at(resp, f):
    return resp.mag_db[np.argmin(np.abs(resp.freq - f))]


def test_flat_gain_and_delay_recovered(sweep):
    rec = simulate(sweep, delay=240, gain=0.5)
    # Recording starts 1 s before the sweep, so the arrival is lead + delay.
    res = measure(rec, sweep, smoothing=6)
    assert res.peak == pytest.approx(FS * 1.0 + 240, abs=0.5)
    for f in (100, 500, 2000, 8000, 15000):
        assert at(res.response, f) == pytest.approx(-6.02, abs=0.5), f


def test_lowpass_shape_recovered(sweep):
    sos = butter(4, 1000, fs=FS, output="sos")
    rec = simulate(sweep, delay=100, sos=sos)
    r = measure(rec, sweep, smoothing=6).response
    assert at(r, 200) == pytest.approx(0.0, abs=0.7)
    assert at(r, 1000) == pytest.approx(-3.0, abs=1.0)
    assert at(r, 2000) == pytest.approx(-24.1, abs=1.5)


def test_works_with_noise(sweep):
    sos = butter(4, 1000, fs=FS, output="sos")
    rec = simulate(sweep, delay=100, sos=sos, noise_db=-50, lead=2.0, tail=6.0)
    res = measure(rec, sweep, smoothing=6)
    assert at(res.response, 500) == pytest.approx(-0.1, abs=1.0)
    # White noise at -50 dBFS rms: a 1/3-octave band at 500 Hz holds about -73 dBFS of it, while the
    # sweep (-12 dBFS peak, -15 dBFS rms) passes through at full level -> roughly 58 dB.
    fc, snr = res.snr_bands
    assert snr[np.argmin(np.abs(fc - 500))] == pytest.approx(58, abs=4)
    assert res.snr_db > 20


def test_snr_unavailable_without_idle_noise(sweep):
    rec = simulate(sweep, delay=100, noise_db=-50, lead=0.5, tail=0.5)
    assert measure(rec, sweep).snr_bands is None


def test_reference_channel_gives_acoustic_delay(sweep):
    ref = simulate(sweep, delay=100)
    mic = simulate(sweep, delay=100 + 240, gain=0.3)
    res = measure(np.column_stack([mic, ref]), sweep, mic_ch=0, ref_ch=1)
    assert res.delay_ms == pytest.approx(240 / FS * 1000, abs=0.02)


def test_phase_is_relative_to_reference(sweep):
    # Pure delay of D samples against the reference -> phase = -360 * f * D / fs.
    d = 48
    ref = simulate(sweep, delay=100)
    mic = simulate(sweep, delay=100 + d)
    r = measure(np.column_stack([mic, ref]), sweep, ref_ch=1, smoothing=None).response
    for f in (200, 1000, 3000):
        i = np.argmin(np.abs(r.freq - f))
        expected = -360 * r.freq[i] * d / FS
        assert (r.phase_deg[i] - expected + 180) % 360 - 180 == pytest.approx(0, abs=5)


def test_gating_removes_late_reflection(sweep):
    echo = simulate(sweep, delay=100)
    rec = echo + 0.5 * np.concatenate([np.zeros(int(0.02 * FS)), echo])[:len(echo)]
    ungated = measure(rec, sweep, smoothing=None).response
    gated = measure(rec, sweep, smoothing=None, post_ms=10).response
    sel = (gated.freq > 1000) & (gated.freq < 10000)
    assert np.std(ungated.mag_db_raw[sel]) > 2.0          # comb filtering
    assert np.abs(gated.mag_db_raw[sel]).max() < 1.0      # clean


def test_smoothing_keeps_flat_flat():
    f = np.linspace(1, 24000, 24000)
    assert np.allclose(smooth_octave(f, np.full_like(f, -3.0), 6), -3.0)


def test_cal_file_parse_and_apply(tmp_path, sweep):
    p = tmp_path / "cal.txt"
    p.write_text('"Sens Factor =-1.23dB, SERNO: 7001234"\n20 0.0\n1000\t0.0\n10000 +2.0\n20000 4.0\n')
    cal = load_cal(p)
    assert cal.sens_factor_db == pytest.approx(-1.23)
    assert cal.at(np.array([10000.0]))[0] == pytest.approx(2.0)
    rec = simulate(sweep, delay=100)
    raw = measure(rec, sweep, smoothing=6).response
    corrected = measure(rec, sweep, smoothing=6, cal=cal).response
    assert at(corrected, 10000) == pytest.approx(at(raw, 10000) - 2.0, abs=0.1)


def test_frequency_response_grid():
    ir = np.zeros(FS)
    ir[100] = 1.0
    r = frequency_response(ir, FS, peak=100.0, smoothing=None)
    assert r.freq[0] == pytest.approx(20) and r.freq[-1] == pytest.approx(20000, rel=0.01)
    assert np.allclose(r.mag_db, 0, atol=0.01)


def test_dayton_style_header(tmp_path):
    p = tmp_path / "dayton.txt"
    p.write_text("*1000Hz\t-39.8\n\t\n20.00\t-0.8\n1000.00\t0.1\n20000.00\t-0.5\n")
    cal = load_cal(p)
    assert cal.sens_factor_db == pytest.approx(-39.8)
    assert len(cal.freq) == 3
