"""Show noise-only vs sweep levels of a saved measurement, per octave band."""
import sys

import numpy as np
from scipy.signal import welch

from octetmeasure import store
from octetmeasure.analysis import deconvolve, find_peak

name = sys.argv[1]
rec, sw, meta = store.load_recording(name)
mic = rec[:, meta.get("mic_ch", 0)]
fs = sw.fs
start = int(round(find_peak(deconvolve(mic, sw))))
n = len(sw.signal)

before = mic[:max(0, start - int(0.3 * fs))]
during = mic[start:start + n]
after = mic[start + n + int(2 * fs):]
print(f"recording {len(mic) / fs:.1f} s; sweep starts at {start / fs:.2f} s, ends {(start + n) / fs:.2f} s")
db = lambda x: 20 * np.log10(max(float(np.sqrt(np.mean(x ** 2))), 1e-12))
print(f"idle before: {db(before):.1f} dBFS rms ({len(before) / fs:.1f} s)   "
      f"during sweep: {db(during):.1f} dBFS rms   idle after: {db(after):.1f} dBFS rms ({len(after) / fs:.1f} s)")
print(f"peak during sweep {20 * np.log10(np.max(np.abs(during))):.1f} dBFS")


def octave_levels(x):
    f, p = welch(x, fs, nperseg=1 << 14)
    out = {}
    for fc in (63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000):
        m = (f >= fc / 2 ** 0.5) & (f < fc * 2 ** 0.5)
        out[fc] = 10 * np.log10(np.sum(p[m]) + 1e-30)
    return out


nb, na = (octave_levels(x) if len(x) > fs else None for x in (before, after))
print("\nidle noise per octave band (dBFS):")
for fc in (63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000):
    row = f"{fc:>6} Hz"
    if nb:
        row += f"   before {nb[fc]:7.1f}"
    if na:
        row += f"   after {na[fc]:7.1f}"
    print(row)
