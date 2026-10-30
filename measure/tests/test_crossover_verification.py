"""A room's fine comb filtering must be compared at the same spectral resolution."""
import json

import numpy as np
import pytest

from octetmeasure import peq
from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.runner import Runner
from octetmeasure.autotune.web import Controller


@pytest.mark.parametrize('mismatch', [False, True])
def test_verification_matches_smoothing_but_still_rejects_broad_errors(tmp_path, monkeypatch, mismatch):
    cfg = config.example()
    cfg['selected_pairs'] = ['a']
    runner = Runner(tmp_path, demo=True)
    candidate = dict(low=200, high=4200, score=0)
    runner.state = dict(id='a'*32, cfg=cfg, candidates={side:[candidate] for side in ('L','R')},
                        eq={'L_a':[], 'R_a':[]})
    runner.directory.mkdir()
    f = np.geomspace(180,10000,720)
    response = 1+.98*np.exp(-2j*np.pi*f*.025)
    raw = 20*np.log10(abs(response))
    measured = peq.smooth_log(f,raw,6)
    assert np.std(raw-measured) > cfg['verification_db']
    if mismatch:
        measured += 10*np.sin(2*np.pi*np.log2(f/180)/3)
    np.savez(runner.directory/'drivers.npz',freq=f,L_a=response,R_a=response)
    monkeypatch.setattr(dsp,'branch_response',lambda cfg,d,c,eq,f:np.ones(len(f)))
    monkeypatch.setattr(runner,'_sum',lambda *args:(f,measured,np.zeros(10),np.zeros(10)))
    if mismatch:
        with pytest.raises(ValueError,match='no crossover candidate'):
            runner._verify()
    else:
        runner._verify()
        assert runner.state['status'] == 'await_room'
        assert runner.state['verification']['L']['prediction_error_db'] < 1e-10


def test_retry_resumes_existing_stage_without_new_profile_or_snapshot(tmp_path, monkeypatch):
    original = dict(id='b'*32, status='failed', stage='verify', running=False,
                    cfg=dict(config.example(), selected_pairs=['a','sub','tweeter']))
    (tmp_path/'current.json').write_text(json.dumps(original))
    controller = Controller(tmp_path, demo=True)
    calls = []
    monkeypatch.setattr(controller.runner,'start',lambda action:calls.append(action))
    monkeypatch.setattr(controller,'snapshot',lambda *args:pytest.fail('Retry must preserve the original backup'))
    controller.retry()
    assert calls == ['resume']
    assert json.loads((tmp_path/'current.json').read_text()) == original
