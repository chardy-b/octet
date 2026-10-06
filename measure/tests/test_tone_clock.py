import numpy as np
import pytest

from octetmeasure import store
from octetmeasure.clock import find_tone, make_tone, tone_ppm

FS = 44100


def record_tone(ppm, f0=1000.0, noise=1e-4, reverb=True, lead=1.5, seconds=14.0, seed=0):
    """What the recorder hears when the player's clock runs `ppm` longer than nominal (so its tone is lower)."""
    n = int((lead + seconds + 3.0) * FS)
    t = np.arange(n) / FS
    f_heard = f0 / (1 + ppm * 1e-6)
    tone = make_tone(FS, 1.0, seconds)                       # envelope shape only; frequency set below
    env = np.zeros(n)
    env[int(lead * FS):int(lead * FS) + len(tone)] = np.abs(tone) / np.max(np.abs(tone)) * 0 + 1.0
    k = int(0.5 * FS)
    ramp = 0.5 * (1 - np.cos(np.pi * np.arange(k) / k))
    s = int(lead * FS)
    env[s:s + k] = ramp
    env[s + len(tone) - k:s + len(tone)] = ramp[::-1]
    y = 0.05 * env * np.sin(2 * np.pi * f_heard * t + 0.7)
    if reverb:                                               # a panel and a room: delayed, scaled copies
        y = y + 0.6 * np.roll(y, int(0.0031 * FS)) + 0.4 * np.roll(y, int(0.0117 * FS))
    return y + np.random.default_rng(seed).normal(0, noise, n)


@pytest.mark.parametrize("ppm", [-80.0, -45.0, 0.0, 30.0, 120.0])
def test_tone_ppm_recovers_offset(ppm):
    x = find_tone(record_tone(ppm), FS)
    est, spread = tone_ppm(x, FS)
    assert est == pytest.approx(ppm, abs=1.0)
    assert spread < 2.0


def test_find_tone_trims_fades_and_silence():
    x = find_tone(record_tone(0.0), FS, trim_s=1.0)
    assert 11.0 < len(x) / FS < 13.5                         # about 14 s of tone minus fades and the 1 s trim each end


def test_clock_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.setattr(store, "CLOCK_FILE", tmp_path / "clock.json")
    assert store.get_clock_ppm() == 0.0
    store.set_clock_ppm(-44.8, source="test")
    assert store.get_clock_ppm() == pytest.approx(-44.8)


def test_stored_clock_applies_only_to_its_own_player(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ROOT", tmp_path)
    monkeypatch.setattr(store, "CLOCK_FILE", tmp_path / "clock.json")
    store.set_clock_ppm(-42.8, player="WiiM Pro-C09E")
    assert store.get_clock_ppm(None) == pytest.approx(-42.8)                 # recordings saved before players were noted
    assert store.get_clock_ppm("WiiM Pro-C09E") == pytest.approx(-42.8)
    assert store.get_clock_ppm("wiim") == pytest.approx(-42.8)
    assert store.get_clock_ppm("local") == 0.0                               # the PC playing to itself: same clock
    assert store.get_clock_ppm("external: ssh octet@octet mpc play") == 0.0   # the Pi has its own clock
    assert store.get_clock_ppm("Some Other Renderer") == 0.0
    store.set_clock_ppm(-42.8)                                               # no player named: applies to DLNA players
    assert store.get_clock_ppm("Some Other Renderer") == pytest.approx(-42.8)
    assert store.get_clock_ppm("local") == 0.0
