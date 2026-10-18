"""Fit a small parametric EQ (peaking filters) that moves a measured response toward a target curve."""
from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares


def harman_target(f, bass_db: float = 4.0, corner: float = 105.0, tilt_db_oct: float = -0.4,
                  tilt_start: float = 1000.0) -> np.ndarray:
    """Harman-style in-room target, relative level in dB: a bass rise below ~100 Hz and a gentle treble tilt.
    An approximation of the published curve (about +4 dB at 50 Hz, corner near 105 Hz, a slight HF roll-off)."""
    f = np.asarray(f, dtype=float)
    shelf = bass_db / (1 + (f / corner) ** 2)
    tilt = np.where(f > tilt_start, tilt_db_oct * np.log2(np.maximum(f, 1.0) / tilt_start), 0.0)
    return shelf + tilt


@dataclass(frozen=True)
class Peq:
    kind: str      # PK, LS or HS
    fc: float
    gain_db: float
    q: float


def biquad_coeffs(fs: float, p: Peq):
    """RBJ-cookbook coefficients (b, a), normalised so that a[0] == 1."""
    a = 10 ** (p.gain_db / 40)
    w0 = 2 * np.pi * p.fc / fs
    cw, sw = np.cos(w0), np.sin(w0)
    al = sw / (2 * p.q)
    if p.kind == "PK":
        b = [1 + al * a, -2 * cw, 1 - al * a]
        d = [1 + al / a, -2 * cw, 1 - al / a]
    elif p.kind == "LS":
        r = 2 * np.sqrt(a) * al
        b = [a * ((a + 1) - (a - 1) * cw + r), 2 * a * ((a - 1) - (a + 1) * cw), a * ((a + 1) - (a - 1) * cw - r)]
        d = [(a + 1) + (a - 1) * cw + r, -2 * ((a - 1) + (a + 1) * cw), (a + 1) + (a - 1) * cw - r]
    elif p.kind == "HS":
        r = 2 * np.sqrt(a) * al
        b = [a * ((a + 1) + (a - 1) * cw + r), -2 * a * ((a - 1) + (a + 1) * cw), a * ((a + 1) + (a - 1) * cw - r)]
        d = [(a + 1) - (a - 1) * cw + r, 2 * ((a - 1) - (a + 1) * cw), (a + 1) - (a - 1) * cw - r]
    elif p.kind == "HP":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        d = [1 + al, -2 * cw, 1 - al]
    elif p.kind == "LP":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        d = [1 + al, -2 * cw, 1 - al]
    else:
        raise ValueError(p.kind)
    b = np.asarray(b, dtype=float)
    d = np.asarray(d, dtype=float)
    return b / d[0], d / d[0]


def sos_for(filters, fs: float) -> np.ndarray:
    """Second-order sections for scipy.signal.sosfilt: the filters applied in series."""
    rows = []
    for p in filters:
        b, d = biquad_coeffs(fs, p)
        rows.append(np.concatenate([b, d]))
    return np.array(rows)


def biquad_db(f, fs: float, p: Peq) -> np.ndarray:
    """Magnitude response of an RBJ-cookbook biquad in dB."""
    f = np.asarray(f, dtype=float)
    b, d = biquad_coeffs(fs, p)
    z = np.exp(-1j * 2 * np.pi * f / fs)
    h = (b[0] + b[1] * z + b[2] * z ** 2) / (d[0] + d[1] * z + d[2] * z ** 2)
    return 20 * np.log10(np.abs(h))


def eq_db(f, fs: float, filters) -> np.ndarray:
    return sum((biquad_db(f, fs, p) for p in filters), np.zeros(len(np.atleast_1d(f))))


def smooth_log(f: np.ndarray, y: np.ndarray, n: float) -> np.ndarray:
    """1/n-octave moving average of dB values on an (approximately) log-spaced grid."""
    out = np.empty_like(y)
    half = 2 ** (1 / (2 * n))
    for i, fc in enumerate(f):
        m = (f >= fc / half) & (f <= fc * half)
        out[i] = np.mean(y[m])
    return out


def _merge_close(filters, min_sep_oct: float = 0.25):
    """Combine same-sign filters that sit closer than min_sep_oct into one broader filter."""
    fl = sorted(filters, key=lambda p: p.fc)
    out = []
    for p in fl:
        if out and np.log2(p.fc / out[-1].fc) < min_sep_oct and np.sign(p.gain_db) == np.sign(out[-1].gain_db):
            a = out.pop()
            wa, wb = abs(a.gain_db), abs(p.gain_db)
            fc = float(np.exp((np.log(a.fc) * wa + np.log(p.fc) * wb) / (wa + wb)))
            out.append(Peq("PK", fc, a.gain_db + p.gain_db, min(a.q, p.q)))
        else:
            out.append(p)
    return out


def rolloff_freq(freq, db, side: str = "low", drop_db: float = 10.0, ref_band=(300.0, 3000.0),
                 smooth: float = 3.0):
    """Frequency where the smoothed response falls drop_db below its mid-band median, scanning in from the
    mid-band toward the low or high end. None if it never does within the measured range."""
    freq = np.asarray(freq, dtype=float)
    m = smooth_log(freq, np.asarray(db, dtype=float), smooth)
    ref = np.median(m[(freq >= ref_band[0]) & (freq <= ref_band[1])])
    if side == "low":
        idx = np.where((freq < ref_band[0]) & (m < ref - drop_db))[0]
        return float(freq[idx.max()]) if len(idx) else None
    idx = np.where((freq > ref_band[1] * 0.67) & (m < ref - drop_db))[0]
    return float(freq[idx.min()]) if len(idx) else None


def fit_peq(freq, measured_db, target_db, fs: float = 48000.0, n_bands: int = 8,
            f_min: float = 100.0, f_max: float = 16000.0, max_boost: float = 3.0, max_cut: float = 6.0,
            max_q: float = 3.0, min_q: float = 0.5, ref_band=(300.0, 3000.0), smooth: float = 3.0,
            fixed=(), weights=None):
    """Return (filters, info). measured_db and target_db are dB arrays on `freq`; the target is shifted
    to the same average level as the measurement over ref_band, so only the shape is corrected.
    Smoothing and the Q ceiling keep the fit to broad corrections: narrow peaks and dips on a panel shift with
    mic position and are not worth chasing."""
    freq = np.asarray(freq, dtype=float)
    sel = (freq >= f_min) & (freq <= f_max)
    f = freq[sel]
    meas = smooth_log(f, np.asarray(measured_db)[sel], smooth)
    ref = (f >= ref_band[0]) & (f <= ref_band[1])
    if not np.any(ref):
        ref = np.ones(len(f), dtype=bool)
    if len(f) < 4:
        raise ValueError('Not enough response points in the requested EQ band.')
    tgt = np.asarray(target_db)[sel]
    fixed_curve = eq_db(f, fs, list(fixed)) if fixed else np.zeros_like(f)   # e.g. a high-pass chosen up front
    err = (tgt - tgt[ref].mean()) - (meas - meas[ref].mean()) - fixed_curve   # what the peaking filters still have to add
    w = np.clip(np.minimum(np.log2(f / f_min) + 0.5, np.log2(f_max / f) + 0.5), 0.3, 1.0)  # soften the edges
    if weights is not None:
        weights = np.asarray(weights,dtype=float)
        if weights.shape != freq.shape or not np.isfinite(weights).all() or np.any(weights <= 0):
            raise ValueError('EQ weights must be finite, positive and match the frequency grid.')
        w *= weights[sel]
    rms0 = float(np.sqrt(np.mean((w * err) ** 2)))

    def greedy():
        filt, resid = [], err.copy()
        for _ in range(n_bands):
            r = smooth_log(f, resid, 3.0)
            free = np.ones_like(f)
            for q_ in filt:
                free[np.abs(np.log2(f / q_.fc)) < 1 / 3] = 0.0
            allowed = ((r < 0) & (max_cut > 0)) | ((r > 0) & (max_boost > 0))
            free *= allowed
            i = int(np.argmax(np.abs(r) * w * free))
            if free[i] == 0.0:
                break
            g = float(np.clip(0.8 * r[i], -max_cut, max_boost))
            if abs(g) < 0.3:
                break
            filt.append(Peq("PK", float(f[i]), g, 1.2))
            resid = err - eq_db(f, fs, filt)
        return filt

    def refine(filt):
        def unpack(x):
            return [Peq("PK", float(np.exp(x[3 * k])), float(x[3 * k + 1]), float(x[3 * k + 2])) for k in range(len(filt))]

        def cost(x):
            curve = eq_db(f, fs, unpack(x))
            over = np.maximum(curve - max_boost, 0.0) + np.maximum(-max_cut - curve, 0.0)
            return np.concatenate([w * (err - curve), 10.0 * over, 0.02 * x[1::3]])

        x0 = np.concatenate([[np.log(p.fc), p.gain_db, p.q] for p in filt])
        lo = np.tile([np.log(f_min), -max_cut, min_q], len(filt))
        hi = np.tile([np.log(f_max), max_boost, max_q], len(filt))
        res = least_squares(cost, np.clip(x0, lo, hi), bounds=(lo, hi), max_nfev=600)
        return unpack(res.x)

    filt = greedy()
    if not filt:
        return [], dict(rms_before=rms0, rms_after=rms0, max_boost_db=0.0)
    for _ in range(4):
        filt = refine(filt)
        merged = _merge_close(filt)
        if len(merged) == len(filt):
            break
        filt = merged
    # last guard: if the combined curve still exceeds the boost limit, scale the boosts down to fit
    curve = eq_db(f, fs, filt)
    if curve.max() > max_boost + 0.3:
        k = max_boost / curve.max()
        filt = [Peq(p.kind, p.fc, p.gain_db * k if p.gain_db > 0 else p.gain_db, p.q) for p in filt]
    out = sorted(filt, key=lambda p: p.fc)
    out = [Peq(p.kind, round(p.fc), round(p.gain_db, 1), round(p.q, 2)) for p in out if abs(p.gain_db) >= 0.5]
    final = err - eq_db(f, fs, out)
    info = dict(
        rms_before=rms0, rms_after=float(np.sqrt(np.mean((w * final) ** 2))),
        max_boost_db=float(np.max(eq_db(freq, fs, out + list(fixed)))) if out else 0.0, f=f,
        measured=meas - meas[ref].mean(), target=tgt - tgt[ref].mean(),
        predicted=meas - meas[ref].mean() + eq_db(f, fs, out + list(fixed)))
    return out, info


def apo_text(filters, preamp_db: float = 0.0) -> str:
    """Equalizer APO / AutoEq style lines (also easy to copy by hand into an app)."""
    lines = [f"Preamp: {preamp_db:.1f} dB"]
    for i, p in enumerate(filters, 1):
        if p.kind in ("HP", "LP"):
            lines.append(f"Filter {i}: ON {p.kind}Q Fc {p.fc:g} Hz Q {p.q:g}")
        else:
            lines.append(f"Filter {i}: ON {p.kind} Fc {p.fc:g} Hz Gain {p.gain_db:+.1f} dB Q {p.q:g}")
    return "\n".join(lines)
