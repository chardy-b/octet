"""Follow-up checks on the three things the first audit left as suspicions."""
import re
from pathlib import Path

import numpy as np
from scipy.signal import sosfilt

from octetmeasure import analysis, store
from octetmeasure.cal import load_cal
from octetmeasure.peq import Peq, eq_db, smooth_log
from audit_helpers import analog_sos, load_filters

ROOT = Path(__file__).resolve().parent.parent
cal = load_cal(ROOT / "emm6_38327.txt")
ppm = store.get_clock_ppm()

print("A. Right channel: deconvolution vs spectral division, by band (SNR explains the spread?)")
rec, sw, meta = store.load_recording("dml2_R")
res = analysis.measure(rec, sw, mic_ch=0, smoothing=None, post_ms=2500, f_lo=100, f_hi=16000, clock_ppm=ppm)
s0 = int(round(res.peak))
n = len(sw.signal) + int(2.5 * sw.fs)
y = rec[s0:s0 + n, 0]
x = np.zeros(n)
x[:len(sw.signal)] = sw.signal
nf = 1 << int(np.ceil(np.log2(n)))
fr = np.fft.rfftfreq(nf, 1 / sw.fs)
h = 20 * np.log10(np.maximum(np.abs(np.fft.rfft(y, nf)) / np.maximum(np.abs(np.fft.rfft(x, nf)), 1e-9), 1e-12))
g = res.response.freq
a = smooth_log(g, res.response.mag_db_raw, 3)
b = smooth_log(g, np.interp(g, fr, h), 3)
fc, snr = res.snr_bands
for lo, hi in ((150, 1000), (1000, 2500), (2500, 5000), (5000, 12000)):
    m = (g >= lo) & (g < hi)
    mb = (fc >= lo) & (fc < hi)
    d = (a - b)[m]
    print(f"   {lo:>5}-{hi:<5} Hz: rms difference {np.sqrt(np.mean(d ** 2)):.2f} dB, bias {d.mean():+.2f} dB;  "
          f"SNR of this recording in the band: median {np.nanmedian(snr[mb]):.0f} dB")

print("\nB. 48 kHz vs 44.1 kHz design, inside each channel's passband only")
for ch, band in (("dml2_L", (110, 16000)), ("dml2_R", (120, 2850))):
    fl = load_filters(ROOT / "measurements" / ch / "peq" / "peq_filters.txt")
    f = np.geomspace(*band, 400)
    d = eq_db(f, 48000.0, fl) - eq_db(f, 44100.0, fl)
    print(f"   {ch}: {band[0]}-{band[1]} Hz: max {np.max(np.abs(d)):.2f} dB at {f[np.argmax(np.abs(d))]:.0f} Hz, rms {np.sqrt(np.mean(d ** 2)):.3f} dB")

print("\nC. Time-domain check inside each channel's passband")
for ch, band in (("dml2_L", (150, 12000)), ("dml2_R", (150, 2500))):
    rec, sw, meta = store.load_recording(ch)
    ir = analysis.deconvolve(rec[:, 0], __import__("octetmeasure.sweep", fromlist=["retimed"]).retimed(sw, ppm))
    peak = analysis.find_peak(ir)
    fl = load_filters(ROOT / "measurements" / ch / "peq" / "peq_filters.txt")
    ir_eq = sosfilt(np.array([analog_sos(p, sw.fs) for p in fl]), ir)
    kw = dict(smoothing=None, cal=cal, f_lo=100, f_hi=16000, post_ms=500)
    r0 = analysis.frequency_response(ir, sw.fs, peak, **kw)
    r1 = analysis.frequency_response(ir_eq, sw.fs, peak, **kw)
    sel = (r0.freq >= band[0]) & (r0.freq <= band[1])
    e = ((r1.mag_db_raw - r0.mag_db_raw) - eq_db(r0.freq, sw.fs, fl))[sel]
    print(f"   {ch}: {band[0]}-{band[1]} Hz: applied EQ vs eq_db() prediction: mean {e.mean():+.3f} dB, rms {np.sqrt(np.mean(e ** 2)):.3f} dB, "
          f"max |{np.max(np.abs(e)):.2f}| dB")
