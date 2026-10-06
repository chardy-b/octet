"""Where does each exciter's acoustic output fall away? Smoothed level re the 300 Hz - 3 kHz median."""
import sys

import numpy as np

from octetmeasure import analysis, store
from octetmeasure.cal import load_cal
from octetmeasure.peq import smooth_log

cal = load_cal("emm6_38327.txt")
for name in sys.argv[1:]:
    rec, sw, meta = store.load_recording(name)
    r = analysis.measure(rec, sw, mic_ch=0, cal=cal, smoothing=None, f_lo=60, f_hi=20000)
    f, m = r.response.freq, smooth_log(r.response.freq, r.response.mag_db_raw, 3)
    ref = np.median(m[(f >= 300) & (f <= 3000)])
    rel = m - ref
    print(f"== {name}  (median 300-3k = {ref:.1f} dBFS)")
    print("   level re mid-band:", "  ".join(f"{fr}Hz {rel[np.argmin(abs(f - fr))]:+.0f}" for fr in (60, 80, 100, 130, 160, 200, 1000, 2000, 3000, 5000, 8000, 12000, 16000)))
    for drop in (6, 10):
        lows = np.where((f < 300) & (rel < -drop))[0]
        low = f[lows.max()] if len(lows) else None
        highs = np.where((f > 2000) & (rel < -drop))[0]
        high = f[highs.min()] if len(highs) else None
        print(f"   {drop:>2} dB below mid-band: below {low and round(low)} Hz, above {high and round(high)} Hz")
