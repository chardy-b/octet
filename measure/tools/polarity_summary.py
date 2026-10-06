"""Summarise normal vs inverted polarity for both-exciters measurements: polarity_summary.py normal inverted left right"""
import sys
from pathlib import Path

import numpy as np

root = Path(__file__).resolve().parent.parent / "measurements"
normal, inverted, left, right = sys.argv[1:5]


def load(n):
    d = np.genfromtxt(root / n / "response.csv", delimiter=",", names=True)
    return d["freq_hz"], d["mag_db"]


f, N = load(normal)
_, I = load(inverted)
_, L = load(left)
_, R = load(right)
best = np.maximum(L, R)
print("band (Hz)     normal vs louder-single | inverted vs louder-single | inverted - normal")
for lo, hi in ((100, 200), (200, 400), (400, 1000), (1000, 3000), (3000, 10000)):
    m = (f >= lo) & (f < hi)
    print(f"{lo:>5}-{hi:<6}   {np.mean(N[m] - best[m]):+6.1f} dB            | {np.mean(I[m] - best[m]):+6.1f} dB                | {np.mean(I[m] - N[m]):+6.1f} dB")
m = (f >= 150) & (f <= 500)
for label, X in (("normal", N), ("inverted", I)):
    d = (X - best)[m]
    print(f"worst point 150-500 Hz, {label}: {d.min():+.1f} dB at {f[m][d.argmin()]:.0f} Hz; best {d.max():+.1f} dB at {f[m][d.argmax()]:.0f} Hz")
