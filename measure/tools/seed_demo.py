"""Fill a running demo-mode app with a few recordings and a calibration file, for trying it out or taking screenshots.
Usage: python tools/seed_demo.py [http://127.0.0.1:8765]"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
CAL = Path(__file__).resolve().parent.parent / "emm6_38327.txt"


def call(path, body=None, method=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"}, method=method or ("POST" if body is not None else "GET"))
    return json.loads(urllib.request.urlopen(req, timeout=120).read() or "null")


def wait():
    while call("/api/job")["running"]:
        time.sleep(0.2)
    return call("/api/job")


call("/api/demo/gain", dict(db=-12))
if CAL.exists():
    call("/api/calibrations", dict(name="Demo microphone calibration.txt", text=CAL.read_text(), activate=True))
for name, channel, note in (("Both speakers", "both", "Mic at the sofa, 2 m from the speakers"),
                            ("Left speaker", "left", "Mic at the sofa"), ("Right speaker", "right", "Mic at the sofa")):
    call("/api/job/record/start", dict(name=name, player="demo", channel=channel, duration=8, fs=44100, note=note))
    st = wait()
    print(name, "->", (st["last_result"] or {}).get("band_peak"), st["last_error"])
call("/api/demo/gain", dict(db=-30))
