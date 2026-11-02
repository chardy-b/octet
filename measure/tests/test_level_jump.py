"""The level-jump guard catches gain changes, not a knock or click in the room."""
import numpy as np
import pytest

from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.runner import Runner


def level_runner(tmp_path, monkeypatch, responses, click_on=None):
    cfg = config.example()
    cfg.update(duration=1, peq_bands=1, automatic=True, room_positions=1)
    for driver in cfg['drivers']:
        driver.update(start_db=-36, max_db=-30, step_db=3)
    cfg['excluded_drivers'] = [d['id'] for d in cfg['drivers'] if d['id'] != 'L_a']
    runner = Runner(tmp_path, demo=True)
    runner.state = dict(id='levels', cfg=cfg, stage='levels', levels={}, status='running')
    runner.directory.mkdir()
    calls = []
    def capture(signal):
        calls.append(len(calls))
        rec = np.full(len(signal)+4800, 1e-5)
        if calls[-1] == click_on:
            rec[len(rec)//2] = .5
        return rec
    monkeypatch.setattr(runner, '_capture', capture)
    monkeypatch.setattr(dsp, '_arrival', lambda *a: 0)
    # Response magnitude per check; a margin below target keeps the ramp climbing.
    monkeypatch.setattr(dsp, 'transfer', lambda rec,x,fs,f: (
        np.full(len(f), responses[min(len(calls), len(responses))-1]+0j), np.zeros(100)))
    return runner, calls


def test_a_click_in_the_room_does_not_stop_the_level_check(tmp_path, monkeypatch):
    runner, calls = level_runner(tmp_path, monkeypatch, [10**(-50/20)], click_on=1)
    runner._levels()
    assert len(calls) == 3
    assert 'L_a' in runner.state['levels']


def test_a_gain_change_between_steps_stops_the_level_check(tmp_path, monkeypatch):
    runner, calls = level_runner(tmp_path, monkeypatch, [10**(-50/20), 10**(-35/20)])
    with pytest.raises(ValueError, match='unexpected level jump'):
        runner._levels()
    assert len(calls) == 2
