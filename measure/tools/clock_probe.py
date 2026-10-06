"""Does a recording show a clock offset between the player and the recorder?
A swept-sine impulse response is sharpest when the sweep used for deconvolution runs at the rate it was played."""
import sys

import numpy as np
from scipy.signal import fftconvolve

from octetmeasure import analysis, store
from octetmeasure.sweep import make_sweep

for name in sys.argv[1:]:
    rec, sw, meta = store.load_recording(name)
    mic = rec[:, 0]
    fs = sw.fs
    ir0 = analysis.deconvolve(mic, sw)
    pk = int(round(analysis.find_peak(ir0)))
    n = len(sw.signal)
    seg = mic[max(0, pk - fs // 5): pk + n + fs]          # just the part that holds the sweep
    off = pk - max(0, pk - fs // 5)
    res = []
    for ppm in range(-200, 201, 4):
        s = make_sweep(fs, sw.duration * (1 + ppm * 1e-6), sw.f1, sw.f2, sw.level_dbfs)
        full = fftconvolve(seg, s.inverse) / s.level_lin
        ir = full[len(s.inverse) - 1:]
        lo, hi = max(0, off - 130), off + 131
        res.append((ppm, float(np.max(np.abs(ir[lo:hi])))))
    pp, hh = np.array(res).T
    best = int(np.argmax(hh))
    print(f"{name:<14} peak height at 0 ppm {hh[len(hh) // 2]:.5f}; best {hh[best]:.5f} at {pp[best]:+.0f} ppm "
          f"({20 * np.log10(hh[best] / hh[len(hh) // 2]):+.1f} dB)   neighbours: "
          + " ".join(f"{int(p):+d}:{h / hh[best]:.2f}" for p, h in zip(pp[max(0, best - 3):best + 4], hh[max(0, best - 3):best + 4])))
