import json

import numpy as np
from fastapi.testclient import TestClient

from octetmeasure.autotune import config
from octetmeasure.autotune.web import Controller, create_app


def saved_run(root, identifier, offset=0):
    cfg = config.example()
    roles = ('sub', 'a', 'b', 'tweeter')
    chosen = {side: dict(strategy='both', order=4, low=400, high=4200, split=1000,
                         gains=dict.fromkeys(roles, -3.), delays=dict.fromkeys(roles, .5),
                         polarity=dict.fromkeys(roles, 1)) for side in ('L', 'R')}
    directory = root/identifier; directory.mkdir()
    record = dict(id=identifier, kind='tuned', name='Profile '+identifier[0], created='2026-10-20', demo=True)
    presets = root/'presets';presets.mkdir(exist_ok=True)
    (presets/f'{identifier}.json').write_text(json.dumps(record))
    state = dict(cfg=cfg, chosen=chosen, eq={d['id']: [] for d in cfg['drivers']}, room={},
                 final_verification={'L': {'prediction_error_db': .25}})
    (directory/'report.json').write_text(json.dumps(state))
    f = np.geomspace(36, 18000, 80)
    for side in ('L', 'R'):
        np.savez(directory/f'final-{side}.npz', freq=f, before=np.full(80, -40.),
                 after=np.full(80, -40.+offset), capture=np.zeros(10000))
    np.savez(directory/'drivers.npz', freq=f, **{d['id']: np.exp(-2j*np.pi*f*.001) for d in cfg['drivers']})
    return record


def test_results_are_selected_by_profile_not_current_run_and_are_read_only(tmp_path):
    first = saved_run(tmp_path, 'a'*32, offset=2)
    second = saved_run(tmp_path, 'b'*32, offset=-5)
    current = json.dumps(dict(status='idle', id=second['id'], running=False))
    (tmp_path/'current.json').write_text(current)
    with TestClient(create_app(tmp_path, demo=True)) as client:
        response = client.get(f"/api/auto/profiles/{first['id']}/measurements")
        assert response.status_code == 200
        data = response.json()
        assert data['profile']['id'] == first['id']
        assert data['available'] and len(data['systems']) == 2 and len(data['drivers']) == 8
        assert data['systems'][0]['after'][0] == 2
        assert data['drivers'][0]['delay_ms'] == .5
        assert 'capture' not in response.text
        other = client.get(f"/api/auto/profiles/{second['id']}/measurements").json()
        assert other['systems'][0]['after'][0] == -5
        assert client.get('/api/auto/profiles/unknown/measurements').status_code == 404
    assert (tmp_path/'current.json').read_text() == current


def test_manual_backup_does_not_invent_measurement_data(tmp_path):
    presets = tmp_path/'presets';presets.mkdir()
    (presets/'manual.json').write_text(json.dumps(dict(id='manual', name='Before tuning', created='2026-10-20', kind='manual', demo=True)))
    data = Controller(tmp_path, demo=True).measurements('manual')
    assert not data['available'] and not data['systems'] and not data['drivers']
    assert 'manual' in data['message']


def test_profile_record_cannot_escape_measurement_directory(tmp_path):
    presets = tmp_path/'presets';presets.mkdir()
    (presets/'bad.json').write_text(json.dumps(dict(id='../elsewhere', name='Invalid', created='2026-10-20', kind='tuned', demo=True)))
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/auto/profiles/unknown/measurements').status_code == 404
    import pytest
    with pytest.raises(ValueError, match='valid'):
        Controller(tmp_path, demo=True).measurements('../elsewhere')
