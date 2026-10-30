"""The test-level control limits every acoustic test without changing music DSP."""
import copy
import threading

import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.backend import Hardware, Simulated
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.runner import Runner
from octetmeasure.autotune.web import Controller, create_app


def candidate():
    roles = ('sub', 'a', 'b', 'tweeter')
    return dict(strategy='both', order=4, low=400, high=4200, split=1000,
                delays=dict.fromkeys(roles, 0.), gains=dict.fromkeys(roles, 0.),
                polarity=dict.fromkeys(roles, 1), score=0)


@pytest.mark.parametrize('value', [-1, 31, True, None, '9', float('nan'), float('inf')])
def test_invalid_attenuation_cannot_change_profile(value):
    with pytest.raises(ValueError, match='test_attenuation_db'):
        config.validate(dict(config.example(), test_attenuation_db=value))


def test_control_saves_without_starting_tones_and_survives_reload(tmp_path, monkeypatch):
    app = create_app(tmp_path, demo=True)
    controller = app.state.controller
    monkeypatch.setattr(controller.runner, 'start', lambda *a, **k: pytest.fail('Settings played tones'))
    with TestClient(app) as client:
        response = client.post('/api/auto/test-level', json=dict(test_attenuation_db=9))
        assert response.status_code == 200
        assert response.json()['test_attenuation_db'] == 9
        assert not response.json()['running']
        assert not (tmp_path/'current.json').exists()
        for invalid in [-1, 31, True, '9', None]:
            assert client.post('/api/auto/test-level', json=dict(test_attenuation_db=invalid)).status_code == 400
        assert client.get('/api/auto').json()['test_attenuation_db'] == 9
        monkeypatch.setattr(type(controller.runner), 'running', property(lambda self: True))
        assert client.post('/api/auto/test-level', json=dict(test_attenuation_db=12)).status_code == 400
        monkeypatch.undo()
    assert Controller(tmp_path, demo=True).test_attenuation() == 9


def test_invalid_start_level_is_rejected_before_snapshot(tmp_path, monkeypatch):
    controller = Controller(tmp_path, demo=True)
    monkeypatch.setattr(controller, 'snapshot', lambda *a: pytest.fail('Invalid level took a snapshot'))
    with pytest.raises(ValueError, match='test_attenuation_db'):
        controller.start(dict(microphone_ready=True, test_attenuation_db=-6))


def test_attenuated_limits_do_not_change_exported_playback():
    cfg = config.example()
    chosen = {side: candidate() for side in ('L', 'R')}
    eq = {d['id']: [] for d in cfg['drivers']}
    original = export(cfg, chosen, eq, {})
    cfg['test_attenuation_db'] = 9
    assert export(cfg, chosen, eq, {}) == original
    driver = cfg['drivers'][0]
    limits = config.measurement_driver(cfg, driver)
    assert limits['max_db'] == driver['max_db']-9
    assert limits['start_db'] == driver['start_db']-9
    with pytest.raises(ValueError, match='ramp limits'):
        dsp.protected_sweep(driver, cfg, driver['max_db'])
    sweep = dsp.protected_sweep(driver, cfg, limits['max_db'])
    assert np.max(abs(sweep)) <= 10**(limits['max_db']/20)*1.00001
    for render in [lambda x: dsp.render_safety(cfg, 'L', x),
                   lambda x: dsp.render_chain(cfg, 'L', chosen['L'], eq, x)]:
        output, _ = render(np.random.default_rng(0).normal(0, .5, cfg['fs']))
        for d in config.active_drivers(cfg):
            assert np.max(abs(output[:, d['output']-1])) <= 10**(config.measurement_ceiling(cfg, d)/20)*1.00001


def test_hardware_enforces_attenuated_ceiling_before_opening_streams():
    hardware = object.__new__(Hardware)
    hardware.cfg = dict(config.example(), test_attenuation_db=9)
    hardware.cancel = threading.Event()
    signal = np.zeros((100, 8))
    signal[:, 0] = 10**(-20/20)  # Below the old -15 limit, above the new -24 limit.
    with pytest.raises(ValueError, match='ceiling exceeded'):
        hardware.capture(signal, threading.Event())


def test_known_loud_level_is_clamped_and_weak_measurement_cannot_ramp_past_it(tmp_path, monkeypatch):
    cfg = dict(config.example(), automatic=True, room_positions=1, selected_pairs=['a'],
               test_attenuation_db=9, measurement_repeats=1)
    runner = Runner(tmp_path, demo=True)
    runner.state = dict(id='attenuation-cap', cfg=cfg, levels={}, status='running')
    runner.directory.mkdir()
    monkeypatch.setattr(runner, '_reference_levels', lambda: {
        d['id']: dict(output_db=-15, margin_db=30) for d in config.active_drivers(cfg)})
    peaks = []
    def capture(signal):
        peaks.append(float(np.max(abs(signal))))
        return np.full(len(signal)+48000, 1e-5)
    monkeypatch.setattr(runner, '_capture', capture)
    monkeypatch.setattr(dsp, '_arrival', lambda *a: 0)
    monkeypatch.setattr(dsp, 'transfer', lambda rec,x,fs,f: (np.ones(len(f),complex), np.zeros(10)))
    runner._levels()
    assert runner.state['levels'] == {'L_a': -24, 'R_a': -24}
    assert len(peaks) == 2
    assert max(peaks) <= 10**(-24/20)*1.00001
    f = np.geomspace(180, 10000, 50)
    responses = {key: dict(h=np.full(len(f), 1e-8+0j), flatten=np.full(len(f), 1e-8),
                           valid=np.ones(len(f), bool)) for key in runner.state['levels']}
    monkeypatch.setattr(dsp, 'extract', lambda *a: (f,responses,{key:np.zeros(100) for key in responses},0))
    with pytest.raises(ValueError, match='-24 dBFS measurement ceiling'):
        runner._measure()
    assert runner.state['levels'] == {'L_a': -24, 'R_a': -24}


def test_auto_run_caps_direct_and_active_dsp_tests_but_keeps_playback(tmp_path, monkeypatch):
    monkeypatch.setattr(dsp, 'search', lambda *a, **k: [candidate()])
    cfg = config.example()
    cfg.update(duration=1, peq_bands=1)
    for driver in cfg['drivers']:
        driver.update(start_db=-30, max_db=-30)
    captures = []

    class Checked(Simulated):
        def capture(self, signal, cancel, meter=None, loopback=False):
            rendered = signal
            if loopback:
                rendered = np.zeros((len(signal)+int(.1*self.cfg['fs']), 8))
                for index, side in enumerate(('L', 'R')):
                    out, scale = dsp.render_chain(self.cfg, side, self.applied['chosen'][side],
                        self.applied['eq'], signal[:, index], self.applied['room'][side], self.applied.get('stereo'))
                    rendered += out*self.applied['preamp'][side]/scale
            for driver in self.cfg['drivers']:
                peak = np.max(abs(rendered[:, driver['output']-1]))
                assert peak <= 10**(config.measurement_ceiling(self.cfg, driver)/20)*1.00002
            captures.append(loopback)
            return super().capture(signal, cancel, meter, loopback=loopback)

    controller = Controller(tmp_path, demo=True)
    controller.runner.set_profile(yaml.safe_dump(cfg))
    controller.runner.backend_factory = Checked
    controller.set_test_attenuation(dict(test_attenuation_db=9))
    controller.start(dict(microphone_ready=True, name='Quiet verification'))
    controller.runner.thread.join(180)
    state = controller.runner.status()
    assert not controller.runner.running
    assert state['status'] == 'applied', state['message']
    assert state['cfg']['test_attenuation_db'] == 9
    assert set(captures) == {True, False}
    assert all(level == -39 for level in state['levels'].values())
    saved = controller.status()['active']
    assert saved['test_attenuation_db'] == 9
    actual, _ = export(state['cfg'], state['chosen'], state['eq'], state['room'], state.get('stereo'))
    without = copy.deepcopy(state['cfg'])
    without['test_attenuation_db'] = 0
    expected, _ = export(without, state['chosen'], state['eq'], state['room'], state.get('stereo'))
    assert actual == expected
