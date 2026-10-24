import copy
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import sosfreqz
import yaml

from octetmeasure import peq
from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.web import Controller, check_safety, create_app


def test_camilla_control_closes_websocket_without_context_manager(monkeypatch):
    import sys
    from types import SimpleNamespace
    from octetmeasure.autotune.camilla import control
    class Socket:
        closed = False
        def send(self, text):
            self.command = json.loads(text)
        def recv(self):
            return json.dumps({self.command: dict(result='Ok', value='Running')})
        def close(self):
            self.closed = True
    sock = Socket()
    monkeypatch.setitem(sys.modules, 'websocket', SimpleNamespace(create_connection=lambda *a, **k: sock))
    assert control([('GetState', None)]) == ['Running']
    assert sock.closed


def candidate():
    roles = ('sub', 'a', 'b', 'tweeter')
    return dict(strategy='both', order=4, low=400, high=4200, split=1000,
                delays=dict.fromkeys(roles, 0.), gains=dict.fromkeys(roles, 0.),
                polarity=dict.fromkeys(roles, 1), score=0)


def document():
    cfg = config.example()
    chosen = {s: candidate() for s in ('L', 'R')}
    eq = {d['id']: [] for d in cfg['drivers']}
    return cfg, chosen, eq


def test_tweeter_floor_enforced_in_profile_stimulus_and_export():
    cfg, chosen, eq = document()
    bad = next(d for d in cfg['drivers'] if d['role'] == 'tweeter')
    bad['min_hz'] = 100
    with pytest.raises(ValueError, match='Tweeter'):
        config.validate(cfg)
    with pytest.raises(ValueError, match='Tweeter'):
        dsp.protected_sweep(bad, cfg, -40)
    with pytest.raises(ValueError, match='Tweeter'):
        export(cfg, chosen, eq, {})


def test_named_safety_guard_export_matches_render_with_room_eq():
    cfg, chosen, eq = document()
    room = {'L': [dict(kind='PK', fc=500., gain_db=-4., q=1.)]}
    doc, _ = export(cfg, chosen, eq, room)
    check_safety(doc, limits=True)
    f = np.geomspace(50, 20000, 600)
    for d in cfg['drivers']:
        expected = dsp.branch_sos(cfg, d, chosen[d['side']], [])
        if d['side'] in room:
            expected = np.vstack([peq.sos_for([peq.Peq(**p) for p in room[d['side']]], cfg['fs']), expected])
        expected = sosfreqz(expected, worN=f, fs=cfg['fs'])[1]
        actual = np.ones(len(f), dtype=complex)
        stage = next(s for s in doc['pipeline'] if s.get('channels') == [d['output'] - 1])
        for name in stage['names']:
            fil = doc['filters'][name]; p = fil['parameters']
            if fil['type'] == 'BiquadCombo':
                actual *= dsp.fast_lr(f, p['freq'], 'highpass', p['order'], cfg['fs'])
            elif fil['type'] == 'Biquad':
                actual *= sosfreqz([[p['b0'], p['b1'], p['b2'], 1, p['a1'], p['a2']]], worN=f, fs=cfg['fs'])[1]
        assert np.allclose(actual, expected, atol=1e-10)
    unsafe = copy.deepcopy(doc)
    unsafe['filters']['L_tweeter_safety_hp']['parameters']['freq'] = 100
    with pytest.raises(ValueError, match='safety'):
        check_safety(unsafe)


def test_automatic_run_saves_and_recalls_complete_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(dsp, 'search', lambda *a, **k: [candidate()])
    controller = Controller(tmp_path, demo=True)
    cfg = config.example()
    cfg.update(duration=1, peq_bands=1)
    for driver in cfg['drivers']:
        driver.update(start_db=-30, max_db=-30)
    controller.runner.set_profile(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match='microphone'):
        controller.start({})
    controller.start(dict(microphone_ready=True, name='Desk'))
    controller.runner.thread.join(180)
    state = controller.status()
    assert not state['running']
    assert state['status'] == 'applied', state['message']
    assert not state['audio_owned']
    assert len(state['profiles']) == 1
    saved = state['profiles'][0]
    assert saved['name'] == 'Desk'
    assert saved['chosen']['L']['low'] == 400
    controller.activate(saved['id'])
    assert controller.status()['active']['id'] == saved['id']
    Path(saved['config_path']).write_text('filters: {}')
    with pytest.raises(ValueError, match='changed'):
        controller.activate(saved['id'])


def test_api_rejects_arbitrary_actions_and_profile_paths(tmp_path):
    from fastapi.testclient import TestClient
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/auto').json()['available']
        assert client.post('/api/auto/select', json={'id': '../../etc/passwd'}).status_code == 400
        assert client.post('/api/auto/shell', json={}).status_code == 400
        assert client.post('/api/auto/start', json={}).status_code == 400


def test_recall_selects_backup_without_writing_shared_manual_config(tmp_path, monkeypatch):
    import octetmeasure.autotune.web as web
    cfg, chosen, eq = document()
    doc, _ = export(cfg, chosen, eq, {})
    backup = tmp_path/'backup.yml'
    original = tmp_path/'manual.yml'
    statefile = tmp_path/'state.yml'
    backup.write_text(yaml.safe_dump(doc))
    original.write_text('Original manual configuration must stay untouched')
    statefile.write_text('config_path: previous.yml\n')
    cfg['camilla']['statefile'] = str(statefile)
    class Backend:
        def __init__(self, cfg): pass
        def take_dac(self): pass
        def systemctl(self, action, service=None): pass
        def master(self, volume, muted):
            assert volume == -36 and muted
    monkeypatch.setattr(web, 'Hardware', Backend)
    monkeypatch.setattr(web.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(web.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(web, 'control', lambda *a: [str(backup), 'Running'])
    controller = Controller(tmp_path/'runs', demo=False)
    controller._activate_hardware(cfg, dict(kind='manual', config_path=str(backup), original_path=str(original)), restore=True)
    assert yaml.safe_load(statefile.read_text())['config_path'] == str(backup)
    assert original.read_text() == 'Original manual configuration must stay untouched'
