import threading
import time

import numpy as np
import pytest

from octetmeasure import service, store
from octetmeasure.audio import DemoIO
from octetmeasure.session import RecordConfig, Session


def run(io, method, cfg, **kw):
    events = []
    s = Session(io, events.append)
    result = getattr(s, method)(cfg, **kw)
    return result, events


def cfg(**kw):
    base = dict(name="demo1", player="demo", duration=4.0, fs=44100, channel="both")
    base.update(kw)
    return RecordConfig.from_dict(base)


def test_record_saves_a_measurement_and_reports_levels(workspace):
    io = DemoIO(speed=60, mic_gain_db=0.0)
    result, events = run(io, "run_record", cfg())
    assert result and result["saved"] and result["name"] == "demo1"
    types = [e["type"] for e in events]
    assert types[0] == "state" and types[-1] == "done"
    assert [e["state"] for e in events if e["type"] == "state"][:3] == ["preparing", "recording", "playing"]
    assert sum(1 for t in types if t == "level") > 100                    # a stream of level readings
    assert result["advice"]["zone"] in ("ok", "target", "hot")            # the demo lands in a sensible range at 0 dB gain
    assert result["snr_db"] > 15 and result["clipped"] is False
    assert store.folder("demo1").joinpath("recording.wav").exists()


def test_the_demo_panel_shows_its_modes(workspace):
    io = DemoIO(speed=60, mic_gain_db=0.0)
    run(io, "run_record", cfg(name="modes", duration=6.0))
    a = service.analyze("modes", service.AnalysisOptions(cal="none", smoothing=6))
    f, m = np.array(a["f"]), np.array(a["mag"])
    at = lambda hz: m[np.argmin(np.abs(f - hz))]
    assert at(300) - at(1500) > 6                                         # the 300 Hz mode stands out
    assert at(5500) - at(2500) > 4                                        # and the 5.5 kHz one
    assert at(16000) < at(1500)                       # treble rolls off


def test_gain_changes_the_level_zone(workspace):
    quiet, _ = run(DemoIO(speed=60, mic_gain_db=-25.0), "run_record", cfg(name="quiet", kind="level_test"))
    loud, _ = run(DemoIO(speed=60, mic_gain_db=0.0), "run_record", cfg(name="loud", kind="level_test"))
    assert quiet["saved"] is False and loud["saved"] is False            # a level test stores nothing
    assert quiet["band_peak"] < loud["band_peak"] - 15
    assert quiet["advice"]["change_db"] > 0 and "Raise" in quiet["advice"]["message"]


def test_left_only_sweep_is_recorded_and_labelled(workspace):
    run(DemoIO(speed=60, mic_gain_db=0.0), "run_record", cfg(name="leftonly", channel="left", invert="right"))
    meta = service.list_measurements()[0]
    assert meta["name"] == "leftonly" and meta["channel"] == "left" and meta["invert"] == "right"


def test_cancel_stops_early_and_saves_nothing(workspace):
    io = DemoIO(speed=2)
    events = []
    s = Session(io, events.append)
    t = threading.Thread(target=lambda: s.run_record(cfg(name="stopme")))
    t.start()
    time.sleep(1.0)
    s.cancel.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert events[-1] == dict(type="state", state="cancelled", message="Stopped")
    assert store.list_measurements() == []


def test_bad_settings_are_reported_not_raised(workspace):
    result, events = run(DemoIO(speed=60), "run_record", cfg(duration=0.1))
    assert result is None and events[-1]["type"] == "error" and "between 0.5 and 60" in events[-1]["message"]
    result, events = run(DemoIO(speed=60), "run_record", cfg(f2=40000))
    assert events[-1]["type"] == "error"
    result, events = run(DemoIO(speed=60), "run_record", cfg(player="dlna", renderer=""))
    assert "network player" in events[-1]["message"]


def test_noise_check_rates_the_room(workspace):
    result, events = run(DemoIO(speed=60), "run_noise", cfg())
    assert result["kind"] == "noise" and result["rating"]["rating"] in ("quiet", "ok", "noisy")
    assert -90 < result["band_rms"] < -50


def test_monitor_streams_until_cancelled(workspace):
    io = DemoIO(speed=20)
    events = []
    s = Session(io, events.append)
    t = threading.Thread(target=lambda: s.run_monitor(cfg()))
    t.start()
    time.sleep(0.8)
    s.cancel.set()
    t.join(timeout=10)
    assert not t.is_alive()
    levels_seen = [e for e in events if e["type"] == "level"]
    assert len(levels_seen) > 10 and set(levels_seen[0]["ch"][0]) == {"peak", "rms", "band_peak", "band_rms"}
    assert events[-1]["state"] == "idle"


def test_clock_check_finds_no_offset_in_the_demo(workspace):
    result, events = run(DemoIO(speed=80, mic_gain_db=0.0), "run_clock", cfg(repeat=1, renderer="Demo streamer"), save=True)
    assert result and abs(result["ppm"]) < 3
    assert store.get_clock_ppm("Demo streamer") == pytest.approx(result["ppm"], abs=0.01)


def test_config_validation_messages():
    with pytest.raises(ValueError, match="name"):
        cfg(name=" ").validate()
    with pytest.raises(ValueError, match="-3 dBFS"):
        cfg(level=0).validate()
    with pytest.raises(ValueError, match="channel"):
        cfg(mic_ch=5).validate()

