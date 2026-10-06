"""From saved measurements to a PEQ: analysis, target, protective edge filters and the fit, in one place."""
import numpy as np

from . import analysis, peq as peqmod, store


def _edge(spec, auto, kind, lo, hi, step):
    if str(spec).lower() == "off":
        return None
    if str(spec).lower() == "auto":
        if auto is None:
            return None
        return peqmod.Peq(kind, float(min(max(round(auto / step) * step, lo), hi)), 0.0, 0.71)
    return peqmod.Peq(kind, float(spec), 0.0, 0.71)


def fit_measurements(names, cal=None, gate_ms=None, clock_ppm=0.0, clock_for=None, bass_db=4.0, corner=105.0, tilt=-0.4,
                     hp="auto", lp="auto", rolloff_db=10.0, bands=8, fmin=100.0, fmax=16000.0, max_boost=3.0,
                     max_cut=6.0, max_q=3.0, smooth=3.0, fs=48000.0) -> dict:
    mags, freq = [], None
    for name in names:
        rec, sw, meta = store.load_recording(name)
        res = analysis.measure(rec, sw, mic_ch=meta.get("mic_ch", 0), ref_ch=None, cal=cal, smoothing=None,
                               post_ms=gate_ms, f_lo=max(sw.f1, 60.0), f_hi=sw.f2,
                               clock_ppm=clock_for(meta) if clock_for else clock_ppm)
        freq = res.response.freq
        mags.append(res.response.mag_db_raw)
    # several positions are averaged in power, which is what a DML needs: one spot is not representative
    mag = 10 * np.log10(np.mean([10 ** (m / 10) for m in mags], axis=0))
    target = peqmod.harman_target(freq, bass_db, corner, tilt)

    hp_f = _edge(hp, peqmod.rolloff_freq(freq, mag, "low", rolloff_db), "HP", 60.0, 200.0, 5)
    lp_f = _edge(lp, peqmod.rolloff_freq(freq, mag, "high", rolloff_db), "LP", 1500.0, 12000.0, 50)
    fixed = [p for p in (hp_f, lp_f) if p]
    # keep the peaking filters clear of the edge filters, so they do not spend boost fighting a corner
    f_lo = max(fmin, hp_f.fc * 1.15) if hp_f else fmin
    f_hi = min(fmax, lp_f.fc / 1.15) if lp_f else fmax
    filters, info = peqmod.fit_peq(freq, mag, target, fs, min(bands, 10 - len(fixed)), f_lo, f_hi,
                                   max_boost, max_cut, max_q, smooth=smooth, fixed=fixed)
    allf = ([hp_f] if hp_f else []) + filters + ([lp_f] if lp_f else [])
    return dict(names=list(names), freq=freq, mag=mag, target=target, filters=allf, peaking=filters, hp=hp_f, lp=lp_f,
                info=info, f_lo=f_lo, f_hi=f_hi, preamp=-round(max(info.get("max_boost_db", 0.0), 0.0), 1), fs=fs)
