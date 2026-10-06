"""Print what the fitter does with the synthetic broad-dip case used in tests/test_peq.py."""
import numpy as np

from octetmeasure.peq import eq_db, fit_peq

FS = 48000
f = 100 * 2 ** (np.arange(0, 7.4 * 24) / 24)
measured = -10.0 * np.exp(-0.5 * (np.log2(f / 300) / 0.3) ** 2)
filters, info = fit_peq(f, measured, np.zeros_like(f), FS, max_boost=4.0)
for p in filters:
    print(p)
fcs = sorted(p.fc for p in filters)
print("octave gaps:", [round(float(np.log2(b / a)), 2) for a, b in zip(fcs, fcs[1:])])
print("rms", info["rms_before"], "->", info["rms_after"], " max boost", info["max_boost_db"])
