"""Impulse response extraction and frequency response analysis."""
from dataclasses import dataclass

import numpy as np
from scipy.signal import fftconvolve, hilbert, welch

from .cal import MicCal
from .sweep import Sweep, retimed


@dataclass
class Response:
    freq: np.ndarray          # Hz, log-spaced
    mag_db: np.ndarray        # smoothed, mic-calibrated, dB relative to digital full scale per unit input
    mag_db_raw: np.ndarray    # unsmoothed
    phase_deg: np.ndarray     # relative to the timing reference if one was used, else to the arrival


DEFAULT_WINDOW_MS = 500.0   # impulse response length analysed when no gate is given


@dataclass
class Result:
    ir: np.ndarray
    fs: int
    peak: float                # arrival of the main impulse, in samples from the start of the recording
    ref_peak: float | None     # arrival on the reference channel, if any
    delay_ms: float | None     # acoustic delay: mic arrival minus reference arrival
    snr_db: float | None       # median of snr_bands
    snr_bands: tuple | None    # (centre frequencies, SNR in dB), one point per 1/3 octave
    clipped: bool
    response: Response
    clock_ppm: float = 0.0     # player-vs-recorder clock offset that was corrected for


def deconvolve(rec: np.ndarray, sweep: Sweep) -> np.ndarray:
    """Impulse response, indexed from the start of the recording."""
    full = fftconvolve(rec, sweep.inverse)
    return full[len(sweep.inverse) - 1:] / sweep.level_lin


def find_peak(ir: np.ndarray) -> float:
    """Arrival time of the main impulse in samples, to sub-sample resolution."""
    env = np.abs(hilbert(ir))
    i = int(np.argmax(env))
    frac = 0.0
    if 0 < i < len(env) - 1:
        a, b, c = env[i - 1], env[i], env[i + 1]
        d = a - 2 * b + c
        if d != 0:
            frac = 0.5 * (a - c) / d
    return i + frac


def smooth_octave(freq: np.ndarray, mag_db: np.ndarray, n: float) -> np.ndarray:
    """1/n-octave smoothing, averaging power."""
    p = 10 ** (mag_db / 10)
    cs = np.concatenate([[0.0], np.cumsum(p)])
    half = 2 ** (1 / (2 * n))
    lo = np.searchsorted(freq, freq / half, "left")
    hi = np.searchsorted(freq, freq * half, "right")
    out = (cs[hi] - cs[lo]) / np.maximum(hi - lo, 1)
    return 10 * np.log10(np.maximum(out, 1e-30))


def frequency_response(ir: np.ndarray, fs: int, peak: float, ref_peak: float | None = None,
                       pre_ms: float = 1.0, post_ms: float | None = None,
                       smoothing: float | None = 12, cal: MicCal | None = None,
                       f_lo: float = 20.0, f_hi: float = 20000.0,
                       points_per_octave: int = 48) -> Response:
    """Windowed frequency response of an impulse response.

    post_ms is the gate after the main impulse: short values exclude room reflections
    (and lower the usable low-frequency limit to roughly 1/post).
    """
    p = int(round(peak))
    pre = int(pre_ms * 1e-3 * fs)
    post = int((DEFAULT_WINDOW_MS if post_ms is None else post_ms) * 1e-3 * fs)
    start = max(0, p - pre)
    end = min(len(ir), p + post)
    seg = ir[start:end].copy()

    n_left = p - start
    if n_left > 1:
        seg[:n_left] *= 0.5 * (1 - np.cos(np.pi * np.arange(n_left) / n_left))
    n_right = (end - p) // 2
    if n_right > 1:
        seg[-n_right:] *= 0.5 * (1 + np.cos(np.pi * np.arange(n_right) / n_right))

    nfft = 1 << int(np.ceil(np.log2(max(len(seg), fs))))
    spec = np.fft.rfft(seg, nfft)
    f = np.fft.rfftfreq(nfft, 1 / fs)

    # Shift the time origin to the reference arrival (or to the mic arrival).
    t0 = ref_peak if ref_peak is not None else peak
    spec = spec * np.exp(-2j * np.pi * f * (start - t0) / fs)

    mag_db = 20 * np.log10(np.maximum(np.abs(spec), 1e-12))
    if cal is not None:
        mag_db = mag_db - cal.at(f)
    sm = smooth_octave(f, mag_db, smoothing) if smoothing else mag_db

    phase = np.unwrap(np.angle(spec))
    n_oct = np.log2(f_hi / f_lo)
    grid = f_lo * 2 ** (np.arange(int(n_oct * points_per_octave) + 1) / points_per_octave)
    return Response(
        freq=grid,
        mag_db=np.interp(grid, f, sm),
        mag_db_raw=np.interp(grid, f, mag_db),
        phase_deg=np.degrees(np.interp(grid, f, phase)),
    )


def sweep_snr(mic: np.ndarray, sweep: Sweep, start: float, per_octave: int = 3):
    """SNR in 1/N-octave bands: the recording while the sweep passes each band, against the noise
    measured in the same band when nothing was playing. Returns (centre_freqs, snr_db) or None."""
    fs, n = sweep.fs, len(sweep.signal)
    s0 = int(round(start))
    before = mic[:max(0, s0 - int(0.3 * fs))]
    after = mic[s0 + n + int(2.0 * fs):]
    noise = before if len(before) >= len(after) else after
    if len(noise) < fs:
        return None
    nper = 1 << 13
    fn, pn = welch(noise, fs, nperseg=min(nper, len(noise)))
    big_l = np.log(sweep.f2 / sweep.f1)
    n_bands = int(np.log2(sweep.f2 / sweep.f1) * per_octave)
    centres = sweep.f1 * 2 ** ((np.arange(n_bands) + 0.5) / per_octave)
    half = 2 ** (0.5 / per_octave)
    out = []
    for fc in centres:
        t_lo = sweep.duration * np.log(fc / half / sweep.f1) / big_l
        t_hi = sweep.duration * np.log(fc * half / sweep.f1) / big_l
        seg = mic[s0 + int(t_lo * fs): s0 + int(t_hi * fs)]
        if len(seg) < 256:
            out.append(np.nan)
            continue
        fs_, ps = welch(seg, fs, nperseg=min(nper, len(seg)))
        band = lambda f, p: np.sum(p[(f >= fc / half) & (f < fc * half)])
        sig, noi = band(fs_, ps), band(fn, pn)
        out.append(10 * np.log10(max(sig - noi, 1e-3 * noi) / noi) if noi > 0 else np.nan)  # floor: -30 dB
    return centres, np.array(out)


def measure(rec: np.ndarray, sweep: Sweep, mic_ch: int = 0, ref_ch: int | None = None,
            clock_ppm: float = 0.0, **response_kwargs) -> Result:
    """Analyse a recording (frames x channels) made while the sweep played.
    clock_ppm corrects for a player whose clock differs from the recorder's (measure it with a tone: see clock.py)."""
    rec = np.atleast_2d(rec.T).T if rec.ndim == 1 else rec
    fs = sweep.fs
    mic = rec[:, mic_ch]
    sweep = retimed(sweep, clock_ppm)
    ir = deconvolve(mic, sweep)
    peak = find_peak(ir)

    ref_peak = delay_ms = None
    if ref_ch is not None:
        ref_ir = deconvolve(rec[:, ref_ch], sweep)
        ref_peak = find_peak(ref_ir)
        delay_ms = (peak - ref_peak) / fs * 1000

    bands = sweep_snr(mic, sweep, peak)
    snr = float(np.nanmedian(bands[1])) if bands is not None else None

    return Result(
        ir=ir, fs=fs, peak=peak, ref_peak=ref_peak, delay_ms=delay_ms, snr_db=snr,
        snr_bands=bands, clipped=bool(np.max(np.abs(rec)) > 0.98),
        response=frequency_response(ir, fs, peak, ref_peak, **{
            "f_lo": max(20.0, sweep.f1), "f_hi": min(20000.0, sweep.f2), **response_kwargs}),
        clock_ppm=float(clock_ppm),
    )
