"""Overlay the saved responses of several measurements: compare.py out.png name1 name2 ...
Optionally "--diff A B" adds a panel with A minus B."""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

root = Path(__file__).resolve().parent.parent / "measurements"
args = sys.argv[1:]
out = args.pop(0)
diff = None
if "--diff" in args:
    i = args.index("--diff")
    diff = args[i + 1:i + 3]
    args = args[:i]


def load(name):
    d = np.genfromtxt(root / name / "response.csv", delimiter=",", names=True)
    return d["freq_hz"], d["mag_db"]


rows = 2 if diff else 1
fig, ax = plt.subplots(rows, 1, figsize=(10, 4.5 * rows), squeeze=False)
for n in args:
    f, m = load(n)
    ax[0, 0].semilogx(f, m, lw=1.5, label=n)
ax[0, 0].set(ylabel="dB (re full scale)", title="Frequency response comparison")
ax[0, 0].grid(True, which="both", alpha=0.3)
ax[0, 0].legend()
if diff:
    fa, ma = load(diff[0])
    fb, mb = load(diff[1])
    ax[1, 0].semilogx(fa, ma - np.interp(fa, fb, mb), lw=1.5, color="C3")
    ax[1, 0].axhline(0, color="k", lw=0.6)
    ax[1, 0].set(ylabel=f"{diff[0]} - {diff[1]} (dB)", xlabel="Hz")
    ax[1, 0].grid(True, which="both", alpha=0.3)
else:
    ax[0, 0].set_xlabel("Hz")
fig.tight_layout()
fig.savefig(out, dpi=110)
print("wrote", out)
