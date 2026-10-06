import numpy as np
import pytest
from scipy.io import wavfile
from scipy.signal import butter, sosfilt

from octetmeasure import analysis
from octetmeasure.cal import load_cal
from octetmeasure.rew_export import export_rew
from octetmeasure.sweep import make_sweep

FS = 48000


@pytest.fixture(scope="module")
def setup():
    sw = make_sweep(FS, duration=6.0)
    n = int((2.0 + sw.duration + 4.0) * FS)
    x = np.zeros(n)
    s = int(2.0 * FS)
    x[s:s + len(sw.signal)] = sw.signal
    y = sosfilt(butter(4, 1000, fs=FS, output="sos"), np.concatenate([np.zeros(300), x])[:n]) * 0.4
    y = y + np.random.default_rng(3).normal(0, 1e-4, n)
    return sw, y[:, None]


@pytest.fixture(scope="module")
def flat(setup):
    """Same timing as `setup` but a flat system, so every frequency carries signal."""
    sw, _ = setup
    n = int((2.0 + sw.duration + 4.0) * FS)
    x = np.zeros(n)
    s = int(2.0 * FS)
    x[s:s + len(sw.signal)] = sw.signal
    y = np.concatenate([np.zeros(300), x])[:n] * 0.4
    y = y + np.random.default_rng(4).normal(0, 1e-4, n)
    return sw, y[:, None]


def parse_rew_text(path):
    """What REW does: only lines that begin with a number are data."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.replace(",", " ").split()
        if parts and parts[0][0] in "0123456789+-.":
            rows.append([float(v) for v in parts])
    return np.array(rows)


def test_fr_text_is_rew_importable(setup, tmp_path):
    sw, rec = setup
    out = export_rew("t", rec, sw, tmp_path)
    text = out["fr"].read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("*")
    d = parse_rew_text(out["fr"])
    assert d.shape == (out["points"], 3)
    assert np.all(np.diff(d[:, 0]) > 0) and d[0, 0] == pytest.approx(sw.f1, rel=0.01)
    assert np.all(np.abs(d[:, 2]) <= 180.01)
    # 4th-order Butterworth at 1 kHz, gain 0.4 (-8 dB): about -8 dB at 200 Hz and -11 dB at 1 kHz
    at = lambda f: d[np.argmin(np.abs(d[:, 0] - f)), 1]
    assert at(200) == pytest.approx(-7.96, abs=0.7)
    assert at(1000) == pytest.approx(-10.97, abs=1.0)


def test_ir_wav_reproduces_response(setup, tmp_path):
    sw, rec = setup
    out = export_rew("t", rec, sw, tmp_path, pre_ms=5.0, ir_ms=500.0)
    fs, ir = wavfile.read(out["ir"])
    assert fs == FS and ir.ndim == 1 and ir.dtype == np.float32
    assert abs(np.argmax(np.abs(ir)) - out["peak_index"]) <= 30        # main impulse sits just after the lead-in
    assert out["peak_index"] == pytest.approx(0.005 * FS, abs=2)
    spec = np.abs(np.fft.rfft(ir, 1 << 16))
    f = np.fft.rfftfreq(1 << 16, 1 / FS)
    db = lambda fr: 20 * np.log10(spec[np.argmin(np.abs(f - fr))])
    assert db(200) == pytest.approx(-7.96, abs=0.8)
    assert db(2000) == pytest.approx(-0.4 + 20 * np.log10(0.4) - 24.1, abs=2.5)


def test_cal_applied_to_both_files(flat, tmp_path):
    sw, rec = flat
    cal_file = tmp_path / "cal.txt"
    cal_file.write_text("20 0.0\n1000 0.0\n10000 3.0\n20000 3.0\n")
    cal = load_cal(cal_file)
    raw = export_rew("raw", rec, sw, tmp_path)
    cald = export_rew("cal", rec, sw, tmp_path, cal=cal, cal_name="cal.txt")
    a, b = parse_rew_text(raw["fr"]), parse_rew_text(cald["fr"])
    i = np.argmin(np.abs(a[:, 0] - 10000))
    assert b[i, 1] - a[i, 1] == pytest.approx(-3.0, abs=0.05)         # mic reads 3 dB high -> corrected down
    assert "cal.txt" in cald["fr"].read_text(encoding="utf-8")
    ir_a, ir_b = (wavfile.read(o["ir"])[1].astype(float) for o in (raw, cald))
    spec = lambda x: np.abs(np.fft.rfft(x, 1 << 16))
    f = np.fft.rfftfreq(1 << 16, 1 / FS)
    k = np.argmin(np.abs(f - 10000))
    assert 20 * np.log10(spec(ir_b)[k] / spec(ir_a)[k]) == pytest.approx(-3.0, abs=0.4)
    j = np.argmin(np.abs(f - 300))
    assert 20 * np.log10(spec(ir_b)[j] / spec(ir_a)[j]) == pytest.approx(0.0, abs=0.3)


def test_normalize_and_offset(setup, tmp_path):
    sw, rec = setup
    out = export_rew("n", rec, sw, tmp_path, normalize=True, offset_db=100.0)
    ir = wavfile.read(out["ir"])[1]
    assert np.max(np.abs(ir)) == pytest.approx(0.9, abs=1e-3)
    d = parse_rew_text(out["fr"])
    ref = parse_rew_text(export_rew("m", rec, sw, tmp_path)["fr"])
    assert np.allclose(d[:, 1] - ref[:, 1], 100.0, atol=0.01)
