"""Exercise the real audio paths of a running app (default port 8766) without playing anything:
the live monitor, a room-noise check, and a full recording run with the 'manual' player and no start command."""
import json
import sys
import time
import urllib.request
from pathlib import Path

B = "http://127.0.0.1:" + (sys.argv[1] if len(sys.argv) > 1 else "8766")
MIC = "Input 1/2 (Audient iD14)"


def call(path, body=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def wait_done(limit=60):
    for _ in range(int(limit / 0.5)):
        time.sleep(0.5)
        st = call("/api/job")
        if not st["running"]:
            return st
    raise SystemExit("job did not finish")


cfg = dict(player="manual", in_device=MIC, mic_ch=0, channels=2, fs=44100)

print("1. live monitor, 3 s")
call("/api/job/monitor/start", cfg)
time.sleep(3)
st = call("/api/job")
print(f"   running={st['running']} kind={st['kind']} state={st['state']} error={st['last_error']}")
call("/api/job/cancel", {})
time.sleep(1.2)
print("   stopped:", not call("/api/job")["running"])

print("2. room noise check, 4 s")
call("/api/job/noise/start", cfg)
st = wait_done()
r = st["last_result"]
print("   error:", st["last_error"], "| band rms", r and round(r["band_rms"], 1), "dBFS |", r and r["rating"]["rating"])

print("3. level test through the full recording path (manual player, nothing is played, nothing is saved), ~19 s")
call("/api/job/record/start", dict(cfg, kind="level_test", name="x", save=False))
st = wait_done(60)
r = st["last_result"]
print("   error:", st["last_error"], "| peak above 60 Hz", r and round(r["band_peak"], 1), "dBFS |", r and r["advice"]["label"], "| seconds", r and r["seconds"], "| saved", r and r["saved"])

text = Path(r"D:\dev\octet\measure\emm6_38327.txt").read_text(encoding="utf-8")
c = call("/api/calibrations", dict(name="EMM-6 38327.txt", text=text, activate=True))
print("4. calibration imported:", c["name"], c["points"], "points, serial", c["serial"], ", active:", c["active"])
a = call("/api/measurements/dml2_L/analysis?smoothing=12")
print(f"5. dml2_L in the app: cal={a['cal']}  clock={a['clock_ppm']} ppm  SNR median={a['snr_db']} dB  peak={a['levels']['band_peak']:.1f} dBFS")
