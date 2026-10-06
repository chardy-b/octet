"""Shared helpers for the audit scripts: an independent biquad design and a reader for peq_filters.txt."""
import re

import numpy as np

from octetmeasure.peq import Peq


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


def load_filters(path) -> list[Peq]:
    out = []
    for m in re.finditer(r"ON (HPQ|LPQ|PK) Fc (\S+) Hz (?:Gain (\S+) dB )?Q (\S+)", path.read_text()):
        kind = {"HPQ": "HP", "LPQ": "LP", "PK": "PK"}[m.group(1)]
        out.append(Peq(kind, float(m.group(2)), float(m.group(3) or 0), float(m.group(4))))
    return out
