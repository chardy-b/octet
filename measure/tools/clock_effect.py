"""How much does correcting for the stored clock offset change each measurement? (1/3-octave smoothed, calibrated)"""
import sys

import numpy as np

from octetmeasure import analysis, store
from octetmeasure.cal import load_cal
from octetmeasure.peq import smooth_log

cal = load_cal("emm6_38327.txt")
ppm = store.get_clock_ppm()
print(f"stored clock offset: {ppm:+.2f} ppm")
for name in sys.argv[1:]:
    rec, sw, meta = store.load_recording(name)
    kw = dict(mic_ch=0, smoothing=None, cal=cal, f_lo=100, f_hi=16000, post_ms=500)
    a = analysis.measure(rec, sw, clock_ppm=0.0, **kw)
    b = analysis.measure(rec, sw, clock_ppm=ppm, **kw)
    f = a.response.freq
    d = smooth_log(f, b.response.mag_db_raw, 3) - smooth_log(f, a.response.mag_db_raw, 3)
    bands = ((100, 500), (500, 2000), (2000, 6000), (6000, 16000))
    cells = []
    for lo, hi in bands:
        m = (f >= lo) & (f < hi)
        cells.append(f"{lo / 1000:g}-{hi / 1000:g}k: mean {d[m].mean():+.2f} max |{np.max(np.abs(d[m])):.1f}|")
    print(f"{name:<14} corrected - uncorrected:  " + "   ".join(cells)
          + f"   | impulse peak {np.max(np.abs(a.ir)):.2e} -> {np.max(np.abs(b.ir)):.2e}")
