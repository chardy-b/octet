"""Is the idle level dominated by DC / subsonic content? Looks at the noise-only part of a measurement."""
import sys

import numpy as np
from scipy.signal import welch

from octetmeasure import store

rec, sw, meta = store.load_recording(sys.argv[1])
fs = sw.fs
x = rec[int(5 * fs):, meta.get("mic_ch", 0)]       # tail: sweep is long over
x = x[-int(12 * fs):]
print(f"mean (DC offset): {np.mean(x):+.5f}  = {20 * np.log10(abs(np.mean(x)) + 1e-12):.1f} dBFS")
print(f"rms with DC removed: {20 * np.log10(np.std(x)):.1f} dBFS   rms incl. DC: {20 * np.log10(np.sqrt(np.mean(x ** 2))):.1f} dBFS")
f, p = welch(x - np.mean(x), fs, nperseg=1 << 15)
for lo, hi in ((0, 5), (5, 20), (20, 40), (40, 63), (63, 125), (125, 1000), (1000, 20000)):
    m = (f >= lo) & (f < hi)
    print(f"{lo:>5}-{hi:<5} Hz: {10 * np.log10(np.sum(p[m]) + 1e-30):7.1f} dBFS")
