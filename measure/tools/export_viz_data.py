"""Export the dml2_* measurements, the target and the per-channel PEQ fits as JSON for the visualization page."""
import json
import sys
from pathlib import Path

import numpy as np

from octetmeasure import analysis, peq as peqmod, peq_workflow, store
from octetmeasure.cal import load_cal

ROOT = Path(__file__).resolve().parent.parent
cal = load_cal(ROOT / "emm6_38327.txt")
ppm = store.get_clock_ppm()
runs = {"L": "dml2_L", "R": "dml2_R", "LR": "dml2_LR", "LRI": "dml2_LR_Rinv"}
r1 = lambda v: [round(float(x), 1) for x in v]

data = {"series": {}, "bands": None, "clock_ppm": ppm}
band_centres = None
for key, name in runs.items():
    rec, sw, meta = store.load_recording(name)
    res = analysis.measure(rec, sw, mic_ch=0, ref_ch=None, cal=cal, smoothing=12, clock_ppm=ppm)
    r = res.response
    fc, snr = res.snr_bands
    snr_at = np.interp(np.log(r.freq), np.log(fc), np.nan_to_num(snr, nan=-30.0))
    step = 2  # 48 points/octave -> 24
    data["series"][key] = dict(
        name=name, f=r1(r.freq[::step]), db=r1(r.mag_db[::step]), snr=r1(snr_at[::step]),
        peak_dbfs=round(float(20 * np.log10(np.max(np.abs(rec[:, 0])))), 1), snr_median=round(float(res.snr_db), 1))
    if band_centres is None:
        band_centres = [float(f) for f in fc]
        data["bands"] = [round(f, 1) for f in band_centres]
    data["series"][key]["band_db"] = [
        round(float(np.interp(np.log(f), np.log(r.freq), r.mag_db)), 1) for f in band_centres]
    data["series"][key]["band_snr"] = [round(float(v), 1) for v in np.nan_to_num(snr, nan=-30.0)]

# The target curve, exactly as the fitter uses it (before it is level-matched to each measurement)
params = dict(bass_db=4.0, corner=105.0, tilt=-0.4, tilt_start=1000.0)
ft = np.geomspace(20, 20000, 241)
data["target"] = dict(params=params, f=r1(ft), db=[round(float(v), 2) for v in peqmod.harman_target(ft, **{
    "bass_db": params["bass_db"], "corner": params["corner"], "tilt_db_oct": params["tilt"]})])

# PEQ fits per channel, with the same options as `peq --max-cut 8`
data["peq"] = {}
wide = np.geomspace(60, 20000, 300)
for ch, name in (("L", "dml2_L"), ("R", "dml2_R")):
    r = peq_workflow.fit_measurements([name], cal=cal, clock_ppm=ppm, max_cut=8.0)
    info = r["info"]
    sel = slice(None, None, 2)
    data["peq"][ch] = dict(
        name=name, f=r1(info["f"][sel]),
        measured=[round(float(v), 2) for v in info["measured"][sel]],
        target=[round(float(v), 2) for v in info["target"][sel]],
        predicted=[round(float(v), 2) for v in info["predicted"][sel]],
        eq_f=r1(wide), eq_db=[round(float(v), 2) for v in peqmod.eq_db(wide, 48000.0, r["filters"])],
        filters=[dict(kind=p.kind, fc=p.fc, gain=p.gain_db, q=p.q) for p in r["filters"]],
        f_lo=round(float(r["f_lo"]), 1), f_hi=round(float(r["f_hi"]), 1),
        rms_before=round(info["rms_before"], 2), rms_after=round(info["rms_after"], 2),
        preamp=r["preamp"], max_boost=round(info["max_boost_db"], 2))

out = Path(sys.argv[1])
out.write_text(json.dumps(data, separators=(",", ":")))
print("wrote", out, f"{out.stat().st_size / 1024:.1f} kB; clock {ppm:+.2f} ppm;",
      {k: (v["rms_before"], v["rms_after"], len(v["filters"])) for k, v in data["peq"].items()})
