"""Independent checks of the analysis chain and the PEQ maths. Run: python tools/audit.py"""
import re
import sys
from pathlib import Path

import numpy as np
from scipy.signal import sosfilt, sosfreqz

from octetmeasure import analysis, store
from octetmeasure.cal import load_cal
from octetmeasure.peq import Peq, biquad_db, eq_db, harman_target, smooth_log
from octetmeasure.sweep import make_sweep

ROOT = Path(__file__).resolve().parent.parent
FS48 = 48000.0


def analog_sos(p: Peq, fs: float) -> np.ndarray:
    """Second implementation of a biquad: bilinear transform (prewarped at fc) of the analog prototype."""
    a = 10 ** (p.gain_db / 40)
    q = p.q
    proto = {
        "PK": ((1, a / q, 1), (1, 1 / (a * q), 1)),
        "LS": ((a, a * np.sqrt(a) / q, a * a), (a, np.sqrt(a) / q, 1)),
        "HS": ((a * a, a * np.sqrt(a) / q, a), (1, np.sqrt(a) / q, a)),
        "HP": ((1, 0, 0), (1, 1 / q, 1)),
        "LP": ((0, 0, 1), (1, 1 / q, 1)),
    }[p.kind]
    k = np.tan(np.pi * p.fc / fs)

    def digital(c):
        p2, p1, p0 = c
        return np.array([p2 / k ** 2 + p1 / k + p0, -2 * p2 / k ** 2 + 2 * p0, p2 / k ** 2 - p1 / k + p0])

    n, d = digital(proto[0]), digital(proto[1])
    return np.concatenate([n / d[0], d / d[0]])


def cascade_db(f, fs, filters):
    sos = np.array([analog_sos(p, fs) for p in filters])
    w, h = sosfreqz(sos, worN=2 * np.pi * np.asarray(f) / fs)
    return 20 * np.log10(np.abs(h))


def section(t):
    print(f"\n=== {t}")


# ---------------------------------------------------------------- 1. biquad maths
section("1. PEQ biquad maths: peq.py vs an independent bilinear-transform design")
rng = np.random.default_rng(0)
f = np.geomspace(20, 20000, 600)
worst = 0.0
for kind in ("PK", "LS", "HS", "HP", "LP"):
    for _ in range(40):
        p = Peq(kind, float(rng.uniform(60, 12000)), float(rng.uniform(-9, 9)), float(rng.uniform(0.5, 4)))
        worst = max(worst, float(np.max(np.abs(biquad_db(f, FS48, p) - cascade_db(f, FS48, [p])))))
print(f"   max difference over 200 random filters: {worst:.2e} dB")

# peak filter: gain at fc and half-gain bandwidth ~ fc/Q
p = Peq("PK", 1000, 6.0, 2.0)
ff = np.geomspace(100, 10000, 20000)
g = biquad_db(ff, FS48, p)
half = ff[g >= 3.0]
print(f"   PK 1 kHz +6 dB Q=2: gain at fc {biquad_db([1000], FS48, p)[0]:.3f} dB; "
      f"half-gain bandwidth {half[-1] - half[0]:.0f} Hz (expected about fc/Q = 500 Hz)")

# ---------------------------------------------------------------- 2. sample rate
section("2. Designed at 48 kHz, but the WiiM may run at 44.1 kHz: how much does the curve change?")
for ch in ("dml2_L", "dml2_R"):
    txt = (ROOT / "measurements" / ch / "peq" / "peq_filters.txt").read_text()
    fl = []
    for m in re.finditer(r"ON (HPQ|LPQ|PK) Fc (\S+) Hz (?:Gain (\S+) dB )?Q (\S+)", txt):
        kind = {"HPQ": "HP", "LPQ": "LP", "PK": "PK"}[m.group(1)]
        fl.append(Peq(kind, float(m.group(2)), float(m.group(3) or 0), float(m.group(4))))
    ff = np.geomspace(100, 16000, 400)
    d = eq_db(ff, 48000.0, fl) - eq_db(ff, 44100.0, fl)
    print(f"   {ch}: max |48 kHz design - 44.1 kHz design| = {np.max(np.abs(d)):.2f} dB (at {ff[np.argmax(np.abs(d))]:.0f} Hz), "
          f"rms {np.sqrt(np.mean(d ** 2)):.2f} dB")

# ---------------------------------------------------------------- 3. end to end
section("3. End to end: apply the PEQ to the measured impulse response, re-analyse, compare with the prediction")
cal = load_cal(ROOT / "emm6_38327.txt")
for ch in ("dml2_L", "dml2_R"):
    rec, sw, meta = store.load_recording(ch)
    ir = analysis.deconvolve(rec[:, 0], sw)
    peak = analysis.find_peak(ir)
    txt = (ROOT / "measurements" / ch / "peq" / "peq_filters.txt").read_text()
    fl = []
    for m in re.finditer(r"ON (HPQ|LPQ|PK) Fc (\S+) Hz (?:Gain (\S+) dB )?Q (\S+)", txt):
        kind = {"HPQ": "HP", "LPQ": "LP", "PK": "PK"}[m.group(1)]
        fl.append(Peq(kind, float(m.group(2)), float(m.group(3) or 0), float(m.group(4))))
    sos = np.array([analog_sos(p, sw.fs) for p in fl])
    ir_eq = sosfilt(sos, ir)
    kw = dict(smoothing=None, cal=cal, f_lo=100, f_hi=16000, post_ms=500)
    a = analysis.frequency_response(ir, sw.fs, peak, **kw)
    b = analysis.frequency_response(ir_eq, sw.fs, peak, **kw)
    applied = b.mag_db_raw - a.mag_db_raw
    predicted = eq_db(a.freq, sw.fs, fl)
    sel = (a.freq >= 150) & (a.freq <= 12000)
    e = (applied - predicted)[sel]
    print(f"   {ch}: time-domain EQ'd response minus original vs eq_db() prediction: "
          f"mean {e.mean():+.2f} dB, rms {np.sqrt(np.mean(e ** 2)):.2f} dB, max |{np.max(np.abs(e)):.2f}| dB")

# ---------------------------------------------------------------- 4. spectral division cross-check
section("4. Deconvolution vs plain spectral division of the real recording (no gating, no cal)")
for ch in ("dml2_L", "dml2_R", "dml2_LR_Rinv"):
    rec, sw, meta = store.load_recording(ch)
    mic = rec[:, 0]
    res = analysis.measure(rec, sw, mic_ch=0, smoothing=None, post_ms=2500, f_lo=100, f_hi=16000)
    s0 = int(round(res.peak))
    n = len(sw.signal) + int(2.5 * sw.fs)
    y = mic[s0:s0 + n]
    x = np.zeros(n)
    x[:len(sw.signal)] = sw.signal
    nf = 1 << int(np.ceil(np.log2(n)))
    fr = np.fft.rfftfreq(nf, 1 / sw.fs)
    Y, X = np.fft.rfft(y, nf), np.fft.rfft(x, nf)
    h = 20 * np.log10(np.maximum(np.abs(Y) / np.maximum(np.abs(X), 1e-9), 1e-12))
    grid = res.response.freq
    a = smooth_log(grid, res.response.mag_db_raw, 3)
    b = smooth_log(grid, np.interp(grid, fr, h), 3)
    sel = (grid >= 150) & (grid <= 12000)
    d = (a - b)[sel]
    print(f"   {ch}: deconvolution - division, 1/3-oct smoothed, 150 Hz-12 kHz: mean {d.mean():+.2f} dB, "
          f"rms {np.sqrt(np.mean(d ** 2)):.2f} dB, max |{np.max(np.abs(d)):.2f}| dB")

# ---------------------------------------------------------------- 5. clock mismatch
section("5. WiiM and iD14 run on different clocks: simulate it with a known system")
fs = 44100
sw0 = make_sweep(fs, 10.0, 60, 20000, -18)
truth_f = [Peq("HP", 150, 0, 0.71), Peq("PK", 3000, 6, 3.0), Peq("PK", 400, -5, 2.0)]
sos = np.array([analog_sos(p, fs) for p in truth_f])
fgrid = np.geomspace(100, 16000, 400)
tru = cascade_db(fgrid, fs, truth_f)
for ppm in (0, 30, 100, 300):
    swp = make_sweep(fs, 10.0 * (1 + ppm * 1e-6), 60, 20000, -18)       # the played sweep runs slightly slow
    n = int(18 * fs)
    x = np.zeros(n)
    x[2 * fs:2 * fs + len(swp.signal)] = swp.signal
    y = sosfilt(sos, np.concatenate([np.zeros(500), x])[:n]) + np.random.default_rng(5).normal(0, 3e-6, n)
    r = analysis.measure(y[:, None], sw0, smoothing=6, f_lo=100, f_hi=16000, post_ms=500)
    err = np.interp(fgrid, r.response.freq, r.response.mag_db) - tru
    sel = (fgrid >= 150) & (fgrid <= 12000)
    print(f"   clock offset {ppm:>3} ppm: magnitude error 150 Hz-12 kHz  mean {err[sel].mean():+.2f} dB, max |{np.max(np.abs(err[sel])):.2f}| dB")

# ---------------------------------------------------------------- 6. window sensitivity
section("6. How much does the choice of analysis window change the result? (dml2_L, 1/3-oct smoothed)")
rec, sw, meta = store.load_recording("dml2_L")
base = analysis.measure(rec, sw, mic_ch=0, smoothing=None, post_ms=500, f_lo=100, f_hi=16000, cal=cal).response
bs = smooth_log(base.freq, base.mag_db_raw, 3)
for pm in (125, 250, 1000, 3000):
    r = analysis.measure(rec, sw, mic_ch=0, smoothing=None, post_ms=pm, f_lo=100, f_hi=16000, cal=cal).response
    d = smooth_log(r.freq, r.mag_db_raw, 3) - bs
    print(f"   window {pm:>4} ms vs 500 ms: mean abs {np.mean(np.abs(d)):.2f} dB, max {np.max(np.abs(d)):.2f} dB")

# ---------------------------------------------------------------- 7. target
section("7. Harman-style target values (dB, relative)")
fs_t = np.array([20, 50, 100, 105, 200, 500, 1000, 2000, 5000, 10000, 14000, 16000, 20000], float)
print("   " + "  ".join(f"{int(f)}Hz {v:+.2f}" for f, v in zip(fs_t, harman_target(fs_t))))
print("   (published description: about +4 dB at 50 Hz, corner near 105 Hz, a slight 1-14 kHz roll-off)")
