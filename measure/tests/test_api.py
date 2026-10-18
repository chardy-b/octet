import time
import zipfile
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from octetmeasure import calibration, store
from octetmeasure.audio import DemoIO
from octetmeasure.ui.server import create_app

CAL = "*1000Hz\t-39.8\n\t\n20.00\t-0.8\n100.00\t1.5\n1000.00\t0.1\n10000.00\t-0.5\n20000.00\t-0.5\n"


@pytest.fixture
def client(workspace):
    app = create_app(io=DemoIO(speed=80, mic_gain_db=0.0))
    with TestClient(app) as c:
        yield c


def wait_idle(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get("/api/job").json()
        if not st["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("the job did not finish")


def record(client, name, **kw):
    body = {**dict(name=name, player="demo", duration=4, fs=44100), **kw}
    r = client.post("/api/job/record/start", json=body)
    assert r.status_code == 200, r.text
    return wait_idle(client)


def test_info_devices_and_renderers(client):
    info = client.get("/api/info").json()
    assert info["demo"] is True and info["version"] and info["data_dir"]
    assert [d["name"] for d in client.get("/api/devices").json()][0].startswith("Demo microphone")
    assert client.get("/api/renderers").json()[0]["name"] == "Demo streamer"
    assert client.get("/").status_code == 200


def test_record_then_browse_analyse_edit_and_delete(client):
    st = record(client, "first take")
    assert st["last_result"]["saved"] and st["last_error"] is None
    rows = client.get("/api/measurements").json()
    assert [r["name"] for r in rows] == ["first take"] and rows[0]["summary"] is None
    s = client.get("/api/measurements/first take/summary").json()
    assert s["band_peak"] < 0 and s["snr_db"] > 10
    assert client.get("/api/measurements").json()[0]["summary"]["snr_db"] == s["snr_db"]     # now cached

    a = client.get("/api/measurements/first take/analysis", params=dict(smoothing=6, cal="none")).json()
    assert len(a["f"]) == len(a["mag"]) == len(a["phase"]) and a["f"][0] >= 60
    assert len(a["snr"]) > 10 and a["ir_t"][0] < 0 < a["ir_t"][-1] and a["levels"]["advice"]["zone"]

    assert client.patch("/api/measurements/first take", json=dict(note="mic at the sofa")).status_code == 200
    assert client.get("/api/measurements").json()[0]["note"] == "mic at the sofa"
    assert client.patch("/api/measurements/first take", json=dict(name="sofa")).json() == dict(name="sofa")
    assert client.get("/api/measurements/first take/analysis").status_code == 404
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/measurements/sofa/rew.zip", params=dict(cal="none")).content))
    assert sorted(z.namelist()) == ["sofa_fr.txt", "sofa_ir.wav"]
    assert client.get("/api/measurements/sofa/recording.wav").headers["content-type"] == "audio/wav"
    assert client.delete("/api/measurements/sofa").status_code == 200
    assert client.get("/api/measurements").json() == []


def test_names_with_commas_and_brackets_work_end_to_end(client):
    st = record(client, "Left speaker, sofa (2 m)")
    assert st["last_result"]["saved"] and "analysis_error" not in st["last_result"] and st["last_result"]["snr_db"] > 10
    assert client.get("/api/measurements/Left speaker, sofa (2 m)/analysis").status_code == 200
    assert client.patch("/api/measurements/Left speaker, sofa (2 m)", json=dict(name="it's fine")).json() == dict(name="it's fine")


def test_unsafe_names_are_refused_before_recording(client):
    for bad in ("a/b", "..\\up", "x:y", ".hidden", "_private"):
        r = client.post("/api/job/record/start", json=dict(name=bad, player="demo", duration=4))
        assert r.status_code == 200
        assert "without" in wait_idle(client)["last_error"] or "Use up to" in wait_idle(client)["last_error"]
    assert client.get("/api/measurements").json() == []


def test_bad_requests_get_plain_messages(client):
    r = client.post("/api/job/record/start", json=dict(name="x", player="demo", duration=0.1))
    assert r.status_code == 200                                         # the job starts, then reports the problem as an event
    st = wait_idle(client)
    assert "between 0.5 and 60" in st["last_error"]
    assert client.get("/api/measurements/nope/analysis").status_code == 404
    assert client.get("/api/measurements/..%2Fetc/analysis").status_code in (400, 404)
    assert client.patch("/api/measurements/nope", json=dict(note="x")).status_code == 404
    assert client.post("/api/job/bogus/start", json={}).status_code == 404
    assert client.post("/api/peq", json=dict(names=[])).status_code == 400


def test_calibration_import_activate_and_inspect(client):
    assert client.get("/api/calibrations").json() == dict(files=[], active=None)
    r = client.post("/api/calibrations", json=dict(name="my mic 1234.txt", text=CAL))
    assert r.status_code == 200 and r.json()["name"] == "my_mic_1234.txt" and r.json()["active"] is True
    info = client.get("/api/calibrations/my_mic_1234.txt").json()
    assert info["points"] == 5 and info["sens_db"] == pytest.approx(-39.8) and info["serial"] == "1234" and info["header"]
    assert client.post("/api/calibrations", json=dict(name="junk.txt", text="hello")).status_code == 400
    assert client.post("/api/calibrations/active", json=dict(name=None)).json() == dict(active=None)
    assert client.delete("/api/calibrations/my_mic_1234.txt").status_code == 200
    assert client.get("/api/calibrations").json()["files"] == []


def test_the_active_calibration_changes_the_analysis(client):
    record(client, "withcal")
    client.post("/api/calibrations", json=dict(name="c.txt", text="20 0\n1000 0\n20000 6\n"))
    plain = client.get("/api/measurements/withcal/analysis", params=dict(cal="none", smoothing=0)).json()
    cald = client.get("/api/measurements/withcal/analysis", params=dict(cal="active", smoothing=0)).json()
    assert cald["cal"] == "c.txt" and plain["cal"] is None
    i = int(np.argmin(np.abs(np.array(plain["f"]) - 15000)))
    assert cald["mag"][i] < plain["mag"][i] - 2                         # the mic read high up there, so it is corrected down


def test_eq_fit_from_a_recording(client):
    record(client, "eqsrc", duration=6)
    out = client.post("/api/peq", json=dict(names=["eqsrc"], options=dict(cal="none", bands=6))).json()
    assert 1 <= len(out["filters"]) <= 10 and out["rms_after"] < out["rms_before"]
    assert out["text"].startswith("Preamp:") and "Filter 1:" in out["text"] and out["eq_f"] and out["predicted"]
    assert all(set(f) == {"kind", "fc", "gain", "q"} for f in out["filters"])
    tgt = client.post("/api/peq/target", json=dict(options=dict(bass_db=6))).json()
    assert tgt["db"][0] > 5                                             # the bass rise follows the option


def test_one_job_at_a_time_and_cancel(client):
    r = client.post("/api/job/record/start", json=dict(name="long", player="demo", duration=20, fs=44100))
    assert r.status_code == 200
    time.sleep(0.3)
    assert client.post("/api/job/noise/start", json=dict(player="demo")).status_code == 409
    assert client.post("/api/job/cancel").json() == dict(cancelled=True)
    st = wait_idle(client)
    assert client.get("/api/measurements").json() == []


def test_starting_a_recording_ends_the_live_meter(client):
    assert client.post("/api/job/monitor/start", json=dict(player="demo")).status_code == 200
    time.sleep(0.3)
    assert client.get("/api/job").json()["kind"] == "monitor"
    st = record(client, "after monitor")
    assert st["last_result"]["saved"]


def test_clock_and_settings(client):
    assert client.get("/api/clock").json() is None
    assert client.post("/api/clock", json=dict(ppm=-42.8, player="WiiM")).json()["ppm"] == -42.8
    assert client.get("/api/info").json()["clock_ppm"] == -42.8
    assert client.delete("/api/clock").status_code == 200 and client.get("/api/clock").json() is None
    assert client.post("/api/settings", json=dict(in_device="Mic", evil="x")).json() == dict(in_device="Mic")


def test_sweep_download_and_demo_gain(client):
    r = client.get("/api/sweep.wav", params=dict(duration=2, fs=44100, channel="left"))
    assert r.status_code == 200 and r.content[:4] == b"RIFF" and len(r.content) > 100000
    assert client.get("/api/sweep.wav", params=dict(duration=999)).status_code == 400
    assert client.post("/api/demo/gain", json=dict(db=-12)).json() == dict(db=-12.0)
