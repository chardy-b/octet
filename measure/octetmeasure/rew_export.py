"""Export a measurement in formats REW can import: a frequency response text file (freq, SPL, phase) and an
impulse response WAV. REW's native .mdat is not documented, so it is not written here; import these into REW
and use File > Save all measurements to get an .mdat."""
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import fftconvolve

from . import analysis
from .cal import MicCal
from .sweep import Sweep, retimed


def cal_correction_ir(cal: MicCal, fs: int, nfft: int = 4096) -> np.ndarray:
    """Zero-phase FIR that removes the mic's deviation from flat (REW cannot apply a cal file to an imported IR)."""
    f = np.fft.rfftfreq(nfft, 1 / fs)
    h = np.fft.irfft(10 ** (-cal.at(f) / 20), nfft)
    h = np.roll(h, nfft // 2) * np.hanning(nfft)
    return h


def export_rew(name: str, rec: np.ndarray, sweep: Sweep, out_dir: Path, mic_ch: int = 0,
               cal: MicCal | None = None, cal_name: str = "", gate_ms: float | None = None,
               ppo: int = 96, pre_ms: float = 5.0, ir_ms: float = 1000.0, offset_db: float = 0.0,
               normalize: bool = False, note: str = "", clock_ppm: float = 0.0) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fs = sweep.fs
    sweep = retimed(sweep, clock_ppm)
    ir = analysis.deconvolve(rec[:, mic_ch], sweep)
    peak = analysis.find_peak(ir)

    # Frequency response: unsmoothed (REW applies its own smoothing), mic calibration applied.
    resp = analysis.frequency_response(ir, fs, peak, None, post_ms=gate_ms, smoothing=None, cal=cal,
                                       f_lo=sweep.f1, f_hi=sweep.f2, points_per_octave=ppo)
    fr_path = out_dir / f"{name}_fr.txt"
    header = [
        f"* Measurement: {name}",
        f"* Exported by octetmeasure; sweep {sweep.f1:g}-{sweep.f2:g} Hz at {fs} Hz, level {sweep.level_dbfs:g} dBFS",
        f"* Window after the main impulse: {gate_ms if gate_ms else analysis.DEFAULT_WINDOW_MS:g} ms",
        f"* Mic calibration: {cal_name or 'none'}",
        f"* Player clock offset corrected: {clock_ppm:+.1f} ppm",
        f"* Level offset applied: {offset_db:+g} dB (levels are dB re digital full scale unless offset)",
        "* Phase is relative to the main impulse arrival (no timing reference)",
        *([f"* Note: {note}"] if note else []),
        "* Freq(Hz)\tSPL(dB)\tPhase(degrees)",
    ]
    wrapped = ((resp.phase_deg + 180) % 360) - 180
    rows = [f"{f:.4f}\t{m + offset_db:.3f}\t{p:.2f}" for f, m, p in zip(resp.freq, resp.mag_db_raw, wrapped)]
    fr_path.write_text("\n".join(header + rows) + "\n", encoding="utf-8")

    # Impulse response: a window starting just before the main impulse, mic calibration applied.
    h = ir
    if cal is not None:
        h = fftconvolve(ir, cal_correction_ir(cal, fs), mode="same")
    start = max(0, int(round(peak)) - int(pre_ms * 1e-3 * fs))
    seg = h[start:start + int((pre_ms + ir_ms) * 1e-3 * fs)].copy()
    scale = 1.0
    if normalize:
        scale = 0.9 / max(np.max(np.abs(seg)), 1e-12)
    ir_path = out_dir / f"{name}_ir.wav"
    wavfile.write(ir_path, fs, (seg * scale).astype(np.float32))
    return dict(fr=fr_path, ir=ir_path, peak_index=int(round(peak)) - start, points=len(resp.freq),
                ir_samples=len(seg), normalized=normalize)
