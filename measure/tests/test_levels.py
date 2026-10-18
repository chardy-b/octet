import numpy as np
import pytest

from octetmeasure.levels import ZONES, BlockMeter, advice, noise_rating, summarize, zone_of

FS = 44100


def tone(f, db, seconds=1.0):
    t = np.arange(int(seconds * FS)) / FS
    return (10 ** (db / 20)) * np.sin(2 * np.pi * f * t)


def test_zones_cover_the_whole_range_without_gaps():
    edges = sorted({e for z in ZONES for e in (z[1], z[2])})
    assert edges[0] <= -100 and edges[-1] == 0.0
    for lo, hi in zip(edges, edges[1:]):
        assert sum(1 for z in ZONES if z[1] <= lo and hi <= z[2]) == 1       # each stretch belongs to exactly one zone


@pytest.mark.parametrize("db,zone", [(-80, "too_low"), (-35, "low"), (-25, "ok"), (-20, "target"), (-12, "target"),
                                     (-6.5, "target"), (-6, "hot"), (-3, "hot"), (-1, "clip"), (0, "clip")])
def test_zone_of(db, zone):
    assert zone_of(db) == zone


def test_advice_direction_and_size():
    quiet = advice(-34)
    assert quiet["tone"] == "warn" and quiet["change_db"] == 22 and "Raise" in quiet["message"]
    hot = advice(-3)
    assert hot["change_db"] == -9 and "Lower" in hot["message"] and "9 dB" in hot["message"]
    assert advice(-12)["zone"] == "target" and "Leave" in advice(-12)["message"]
    assert advice(-1)["tone"] == "bad"


def test_noise_rating():
    assert noise_rating(-70)["rating"] == "quiet"
    assert noise_rating(-60)["rating"] == "ok"
    assert noise_rating(-50)["rating"] == "noisy"


def test_meter_levels_and_the_60_hz_split():
    m = BlockMeter(FS, 2)
    block = np.column_stack([tone(1000, -20), tone(20, -20)])           # a mid tone, and rumble
    for _ in range(3):
        out = m.process(block)                                           # let the filter settle
    mid, rumble = out
    assert mid["peak"] == pytest.approx(-20.0, abs=0.2) and mid["rms"] == pytest.approx(-23.0, abs=0.2)
    assert mid["band_peak"] == pytest.approx(-20.0, abs=0.6)             # passes the filter
    assert rumble["peak"] == pytest.approx(-20.0, abs=0.2)
    assert rumble["band_peak"] < -40.0                                   # removed above 60 Hz


def test_meter_silence_is_floored():
    out = BlockMeter(FS, 1).process(np.zeros((1000, 1)))[0]
    assert out["peak"] == -120.0 and out["band_rms"] == -120.0


def test_summarize_matches_meter():
    x = tone(2000, -15, 2.0)[:, None]
    s = summarize(x, FS)
    assert s["peak"] == pytest.approx(-15.0, abs=0.2)
