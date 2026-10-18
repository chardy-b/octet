"""What the web app asks of the analysis code: JSON-ready, cached results for recordings and EQ fits."""
import json
import re
import shutil
import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass

import numpy as np

from . import analysis, calibration, levels, peq as peqmod, peq_workflow, store

_LOCK = threading.Lock()
_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()


@dataclass
class AnalysisOptions:
    smoothing: float = 12.0          # 1/N octave; 0 = none
    window_ms: float | None = None   # time after the main impulse that is analysed; None = 500 ms
    cal: str = "active"              # "active", "none", or a calibration file name
    clock: str = "stored"            # "stored", "off", or a number of ppm

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisOptions":
        o = cls()
        if d.get("smoothing") is not None:
            o.smoothing = float(d["smoothing"])
        if d.get("window_ms") not in (None, "", 0):
            o.window_ms = float(d["window_ms"])
        if d.get("cal"):
            o.cal = str(d["cal"])
        if d.get("clock") not in (None, ""):
            o.clock = str(d["clock"])
        return o


def _r(a, nd=2):
    a = np.asarray(a, dtype=float)
    return [None if not np.isfinite(v) else round(float(v), nd) for v in a]


def _stamp(name: str):
    d = store.folder(name)
    return (d / "recording.wav").stat().st_mtime_ns, (d / "meta.json").stat().st_mtime_ns


def resolve_cal(opt: str):
    if opt == "none":
        return None, None
    name = calibration.active_name() if opt == "active" else opt
    return calibration.load(name), name


def resolve_clock(opt: str, meta: dict) -> float:
    o = str(opt).lower()
    if o == "stored":
        return store.get_clock_ppm(meta.get("player"))
    if o in ("off", "none"):
        return 0.0
    return float(o)


# ---------------------------------------------------------------- listing and editing
def validate_name(name: str) -> str:
    return store.check_name(name)


def list_measurements() -> list[dict]:
    out = []
    for name in store.list_measurements():
        try:
            meta = json.loads((store.folder(name) / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        sw = meta.get("sweep", {})
        out.append(dict(
            name=name, created=meta.get("created"), note=meta.get("note", ""), channel=meta.get("channel", "both"),
            invert=meta.get("invert", "none"), player=meta.get("player"), mic_ch=meta.get("mic_ch", 0),
            fs=sw.get("fs"), duration=sw.get("duration"), f1=sw.get("f1"), f2=sw.get("f2"), level=sw.get("level_dbfs"),
            summary=cached_summary(name)))
    out.sort(key=lambda m: m["created"] or "", reverse=True)
    return out


def cached_summary(name: str):
    p = store.folder(name) / "summary.json"
    try:
        s = json.loads(p.read_text())
        return s if s.get("stamp") == list(_stamp(name)) else None
    except (OSError, ValueError, KeyError):
        return None


def summary(name: str) -> dict:
    """Peak level and signal-to-noise of a recording, stored beside it so listing stays quick."""
    s = cached_summary(name)
    if s:
        return s
    a = analyze(name, AnalysisOptions(smoothing=0))
    s = dict(stamp=list(_stamp(name)), band_peak=a["levels"]["band_peak"], peak=a["levels"]["peak"],
             snr_db=a["snr_db"], snr_worst=a["snr_worst"], clipped=a["clipped"])
    (store.folder(name) / "summary.json").write_text(json.dumps(s))
    return s


def delete(name: str) -> None:
    validate_existing(name)
    shutil.rmtree(store.folder(name))
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == name]:
            del _CACHE[k]


def validate_existing(name: str) -> None:
    try:
        store.check_name(name)
    except ValueError:
        raise FileNotFoundError(f"No measurement named {name!r}") from None
    if not (store.folder(name) / "recording.wav").exists():
        raise FileNotFoundError(f"No measurement named {name!r}")


def rename(name: str, new: str) -> str:
    validate_existing(name)
    new = validate_name(new)
    if store.folder(new).exists():
        raise ValueError(f"A measurement named {new!r} already exists.")
    store.folder(name).rename(store.folder(new))
    meta_p = store.folder(new) / "meta.json"
    meta = json.loads(meta_p.read_text())
    meta["name"] = new
    meta_p.write_text(json.dumps(meta, indent=2))
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == name]:
            del _CACHE[k]
    return new


def set_note(name: str, note: str) -> None:
    validate_existing(name)
    p = store.folder(name) / "meta.json"
    meta = json.loads(p.read_text())
    meta["note"] = note.strip()[:500]
    p.write_text(json.dumps(meta, indent=2))


# ---------------------------------------------------------------- analysis for the screen
def analyze(name: str, opts: AnalysisOptions) -> dict:
    validate_existing(name)
    cal, cal_name = resolve_cal(opts.cal)
    rec, sw, meta = store.load_recording(name)
    ppm = resolve_clock(opts.clock, meta)
    key = (name, _stamp(name), opts.smoothing, opts.window_ms, cal_name, ppm)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    mic = meta.get("mic_ch", 0)
    ref = meta.get("ref_ch")
    res = analysis.measure(rec, sw, mic_ch=mic, ref_ch=ref, post_ms=opts.window_ms, smoothing=opts.smoothing or None,
                           cal=cal, clock_ppm=ppm)
    r = res.response
    step = max(1, len(r.freq) // 420)
    fc, snr = res.snr_bands if res.snr_bands is not None else (np.array([]), np.array([]))
    snr = np.nan_to_num(snr, nan=-30.0)
    lo, hi = max(0, int(round(res.peak)) - int(0.002 * res.fs)), int(round(res.peak)) + int(0.025 * res.fs)
    seg = res.ir[lo:hi]
    t_ms = (np.arange(lo, lo + len(seg)) - res.peak) / res.fs * 1000
    lv = levels.summarize(rec, sw.fs, mic)
    out = dict(
        name=name, meta=meta, fs=sw.fs, f=_r(r.freq[::step], 1), mag=_r(r.mag_db[::step]), mag_raw=_r(r.mag_db_raw[::step]),
        phase=_r(((r.phase_deg[::step] + 180) % 360) - 180, 1),
        snr_f=_r(fc, 1), snr=_r(snr, 1), snr_db=None if res.snr_db is None else round(float(res.snr_db), 1),
        snr_worst=round(float(np.min(snr)), 1) if len(snr) else None,
        ir_t=_r(t_ms, 3), ir=[round(float(v), 7) for v in seg], peak_s=round(float(res.peak / res.fs), 3),
        delay_ms=None if res.delay_ms is None else round(float(res.delay_ms), 3), clipped=res.clipped,
        clock_ppm=round(float(ppm), 2), cal=cal_name, window_ms=opts.window_ms or analysis.DEFAULT_WINDOW_MS,
        smoothing=opts.smoothing, levels=dict(lv, advice=levels.advice(lv["band_peak"])),
        recording_seconds=round(len(rec) / sw.fs, 1))
    with _LOCK:
        _CACHE[key] = out
        while len(_CACHE) > 48:
            _CACHE.popitem(last=False)
    return out


def sweep_wav_bytes(cfg_dict: dict) -> bytes:
    """The sweep as a WAV file, for playing from a device the app cannot control."""
    import io
    import wave

    from .session import RecordConfig
    from .sweep import make_sweep, stereo_playback
    cfg = RecordConfig.from_dict({**cfg_dict, "player": "manual"})        # a file download needs no player
    cfg.validate()
    sw = make_sweep(cfg.fs, cfg.duration, cfg.f1, cfg.f2, cfg.level)
    y = np.clip(stereo_playback(sw, cfg.channel, 2.0, 3.0, cfg.invert), -1, 1)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(cfg.fs)
        v = (y * 8388607).astype("<i4")
        w.writeframes(v.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------- EQ
@dataclass
class PeqOptions:
    bass_db: float = 4.0
    corner: float = 105.0
    tilt: float = -0.4
    hp: str = "auto"
    lp: str = "auto"
    rolloff_db: float = 10.0
    bands: int = 8
    fmin: float = 100.0
    fmax: float = 16000.0
    max_boost: float = 3.0
    max_cut: float = 8.0
    max_q: float = 3.0
    smooth: float = 3.0
    window_ms: float | None = None
    cal: str = "active"
    clock: str = "stored"

    @classmethod
    def from_dict(cls, d: dict) -> "PeqOptions":
        o = cls()
        for k, v in d.items():
            if not hasattr(o, k) or v in (None, ""):
                continue
            cur = getattr(o, k)
            setattr(o, k, type(cur)(v) if cur is not None and not isinstance(cur, str) else (v if k == "window_ms" else str(v)))
        if o.window_ms is not None:
            o.window_ms = float(o.window_ms)
        return o

    def validate(self):
        if not 1 <= self.bands <= 10:
            raise ValueError("Use between 1 and 10 peaking filters.")
        if not 20 <= self.fmin < self.fmax <= 20000:
            raise ValueError("The fitted range must sit between 20 Hz and 20 kHz.")
        if not 0 <= self.max_boost <= 12 or not 0 <= self.max_cut <= 20:
            raise ValueError("Boost up to 12 dB and cut up to 20 dB are allowed.")
        if not 0.5 <= self.max_q <= 10:
            raise ValueError("The highest Q must be between 0.5 and 10.")


def fit_peq(names: list[str], opts: PeqOptions) -> dict:
    opts.validate()
    if not names:
        raise ValueError("Choose at least one measurement.")
    for n in names:
        validate_existing(n)
    cal, cal_name = resolve_cal(opts.cal)
    r = peq_workflow.fit_measurements(
        names, cal=cal, gate_ms=opts.window_ms, clock_for=lambda meta: resolve_clock(opts.clock, meta),
        bass_db=opts.bass_db, corner=opts.corner, tilt=opts.tilt, hp=opts.hp, lp=opts.lp, rolloff_db=opts.rolloff_db,
        bands=opts.bands, fmin=opts.fmin, fmax=opts.fmax, max_boost=opts.max_boost, max_cut=opts.max_cut,
        max_q=opts.max_q, smooth=opts.smooth)
    info = r["info"]
    wide = np.geomspace(60, 20000, 300)
    sel = slice(None, None, 2)
    filters = r["filters"]
    out = dict(
        names=names, cal=cal_name, f=_r(info["f"][sel], 1) if "f" in info else [],
        measured=_r(info["measured"][sel]) if "measured" in info else [],
        target=_r(info["target"][sel]) if "target" in info else [],
        predicted=_r(info["predicted"][sel]) if "predicted" in info else [],
        eq_f=_r(wide, 1), eq_db=_r(peqmod.eq_db(wide, 48000.0, filters)),
        filters=[dict(kind=p.kind, fc=p.fc, gain=p.gain_db, q=p.q) for p in filters],
        f_lo=round(float(r["f_lo"]), 1), f_hi=round(float(r["f_hi"]), 1),
        rms_before=round(float(info["rms_before"]), 2), rms_after=round(float(info["rms_after"]), 2),
        preamp=r["preamp"], max_boost=round(float(info.get("max_boost_db", 0.0)), 2),
        text=peqmod.apo_text(filters, r["preamp"]), table=_table(filters, r["preamp"]))
    return out


def _table(filters, preamp: float) -> str:
    rows = [f"Preamp: {preamp:+.1f} dB", "#  Type  Freq (Hz)  Gain (dB)  Q"]
    for i, p in enumerate(filters, 1):
        gain = "-" if p.kind in ("HP", "LP") else f"{p.gain_db:+.1f}"
        rows.append(f"{i:<2} {p.kind:<4}  {p.fc:<9g}  {gain:<9}  {p.q:g}")
    return "\n".join(rows)


def target_curve(opts: PeqOptions) -> dict:
    f = np.geomspace(20, 20000, 241)
    return dict(f=_r(f, 1), db=_r(peqmod.harman_target(f, opts.bass_db, opts.corner, opts.tilt)))
