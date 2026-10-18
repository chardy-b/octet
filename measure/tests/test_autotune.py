import copy
import json
from pathlib import Path

import yaml
import threading
import time

import numpy as np
import pytest
from scipy.signal import sosfreqz

from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.backend import Simulated
from octetmeasure.autotune.camilla import Deployment, export
from octetmeasure.autotune.runner import Runner
from octetmeasure.sweep import make_sweep
from octetmeasure.analysis import deconvolve


def profile():
    cfg = config.example()
    cfg.update(duration=1,room_positions=1,peq_bands=2)
    for d in cfg['drivers']:
        d['start_db'] = d['max_db'] = -30
    return cfg


@pytest.mark.parametrize('lo,hi',[(30,300),(2500,20000),(500,1500)])
def test_narrow_sweep_inverse(lo,hi):
    sw = make_sweep(48000,1,lo,hi,-24)
    assert np.isfinite(sw.inverse).all()
    ir = deconvolve(np.pad(sw.signal,(100,1000)),sw)
    assert abs(np.argmax(abs(ir))-100) < 3


@pytest.mark.parametrize('mutate',[
    lambda c:c['drivers'][0].update(output=2),
    lambda c:c['drivers'][0].update(max_db=float('nan')),
    lambda c:c['drivers'][0].update(min_hz=1000,max_hz=100),
    lambda c:c.update(max_boost_db=4),
    lambda c:c.update(mic_channel=2),
])
def test_invalid_profiles(mutate):
    cfg=profile(); mutate(cfg)
    with pytest.raises(ValueError):config.validate(cfg)


def test_hardware_needs_explicit_setup():
    with pytest.raises(ValueError,match='Confirm'):
        config.validate(profile(),hardware=True)


def test_ramp_ceiling_and_protected_routing():
    cfg=config.example(); d=cfg['drivers'][0]
    d.update(start_db=-47,max_db=-24,step_db=3)
    levels=list(config.ramp(d))
    assert levels[0]==-47 and levels[-1]==-24 and np.max(np.diff(levels))<=3
    signal,slots=dsp.sequence(cfg,{d['id']:d['max_db'] for d in cfg['drivers']})
    for slot,d in zip(slots[1:-1],cfg['drivers']):
        block=signal[slot['start']:slot['start']+slot['length']]
        assert np.count_nonzero(np.max(abs(block),axis=0))==1
        assert np.max(abs(block[:,d['output']-1]))<=10**(d['max_db']/20)*1.00001
    _,h=sosfreqz(dsp.protection(cfg['drivers'][-1],cfg['fs']),worN=[100,4000],fs=cfg['fs'])
    assert 20*np.log10(abs(h[0]/h[1])) < -90


@pytest.mark.parametrize('ppm',[-80,0,65])
def test_clock_and_relative_driver_delays(ppm):
    cfg=profile(); levels={d['id']:d['max_db'] for d in cfg['drivers']}
    signal,slots=dsp.sequence(cfg,levels)
    rec=Simulated(cfg,ppm=ppm).capture(signal,threading.Event())
    f,responses,impulses,estimate=dsp.extract(rec,signal,slots,cfg)
    assert abs(estimate-ppm)<4
    assert all(np.isfinite(r['h']).all() for r in responses.values())
    # Tweeter arrives before panel A; its delay must not be independently zeroed.
    band=(f>3500)&(f<6000)
    relative=responses['L_tweeter']['h'][band]/responses['L_a']['h'][band]
    slope=np.polyfit(f[band],np.unwrap(np.angle(relative)),1)[0]
    assert slope > 0


def candidate():
    return dict(strategy='both',order=4,low=200,high=3000,split=1000,
                delays={r:0. for r in ('sub','a','b','tweeter')},
                gains={r:0. for r in ('sub','a','b','tweeter')},
                polarity={r:1 for r in ('sub','a','b','tweeter')},score=0)


def test_export_maps_all_outputs_and_retains_limits():
    cfg=profile(); chosen={s:candidate() for s in ('L','R')}; eq={d['id']:[] for d in cfg['drivers']}
    doc,scales=export(cfg,chosen,eq,{})
    assert len(doc['mixers']['drivers']['mapping'])==8
    expected = {'L_a': 1, 'R_a': 2, 'L_b': 3, 'R_b': 4,
                'L_sub': 5, 'R_sub': 6, 'L_tweeter': 7, 'R_tweeter': 8}
    assert {d['id']: d['output'] for d in cfg['drivers']} == expected
    example = yaml.safe_load((Path(__file__).resolve().parents[2]/'pi/speaker.example.yaml').read_text())
    assert {d['id']: d['output'] for d in example['drivers']} == expected
    for d in cfg['drivers']:
        route = next(m for m in doc['mixers']['drivers']['mapping'] if m['dest'] == expected[d['id']] - 1)
        assert route['sources'][0]['channel'] == (0 if d['side'] == 'L' else 1)
        stage = next(s for s in doc['pipeline'] if s.get('channels') == [expected[d['id']] - 1])
        assert d['id'] + '_gain' in stage['names']
    for d in cfg['drivers']:
        assert doc['filters'][d['id']+'_limit']['parameters']['clip_limit']==d['max_db']
        assert doc['filters'][d['id']+'_gain']['parameters']['gain']<0
    assert all(0<v<1 for v in scales.values())


@pytest.mark.parametrize('order',[4,8])
@pytest.mark.parametrize('kind',['highpass','lowpass'])
def test_optimizer_filter_matches_export(order,kind):
    f=np.geomspace(20,20000,500)
    expected=sosfreqz(dsp.lr(2300,kind,order,48000),worN=f,fs=48000)[1]
    assert np.allclose(dsp.fast_lr(f,2300,kind,order,48000),expected,atol=1e-12)


def test_fractional_delay_export_matches_renderer():
    cfg=profile();chosen={s:candidate() for s in ('L','R')}
    chosen['L']['delays']['a']=.127
    eq={d['id']:[] for d in cfg['drivers']}
    doc,_=export(cfg,chosen,eq,{})
    coeff=doc['filters']['L_a_fractional']['parameters']['values']
    delay=doc['filters']['L_a_delay']['parameters']['delay']
    f=np.geomspace(20,20000,500);z=np.exp(-2j*np.pi*f/cfg['fs'])
    assert np.allclose(z**delay*(coeff[0]+coeff[1]*z),dsp.delay_response(f,.127,cfg['fs']))


def test_second_controller_cannot_overwrite_live_run(tmp_path):
    entered=threading.Event()
    class Slow(Simulated):
        def capture(self,signal,cancel,meter=None,**kw):
            entered.set();cancel.wait(5)
            raise InterruptedError('Stopped')
    first=Runner(tmp_path,demo=True,backend_factory=Slow)
    first.start(profile=profile());assert entered.wait(3)
    original=first.state['id']
    second=Runner(tmp_path,demo=True)
    assert second.state['status']=='running'
    with pytest.raises(RuntimeError,match='Another'):
        second.start(profile=profile())
    first.mute();first.thread.join(5)
    assert json.loads((tmp_path/'current.json').read_text())['id']==original


def test_auto_api_profile_and_job_exclusion(workspace,monkeypatch):
    from fastapi.testclient import TestClient
    from octetmeasure.ui.server import create_app
    from octetmeasure.audio import DemoIO
    app=create_app(io=DemoIO(speed=80))
    runner=app.state.autotune
    entered=threading.Event()
    class Slow(Simulated):
        def capture(self,signal,cancel,meter=None,**kw):
            entered.set();cancel.wait(5)
            raise InterruptedError('Stopped')
    runner.backend_factory=Slow
    with TestClient(app) as client:
        assert client.get('/api/auto').json()['status']=='idle'
        assert client.put('/api/auto/profile',json={'yaml':'drivers: ['}).status_code==400
        assert 'max_boost_db: 3' in client.get('/api/auto/profile').json()['yaml']
        assert client.post('/api/auto/start').status_code==200
        assert entered.wait(3)
        assert client.post('/api/job/monitor/start',json={}).status_code==409
        assert client.post('/api/auto/mute').status_code==200
        runner.thread.join(5)
        assert client.get('/api/auto').json()['status']=='cancelled'


def wait(runner,timeout=180):
    runner.thread.join(timeout)
    assert not runner.running,runner.status()
    assert runner.state['status'] not in ('failed','cancelled'),runner.state.get('message')


def test_full_simulated_workflow(tmp_path,monkeypatch):
    # Keep optimizer testing separate so the orchestration test is fast/deterministic.
    monkeypatch.setattr(dsp,'search',lambda *a,**k:[candidate()])
    runner=Runner(tmp_path/'auto',demo=True)
    runner.start(profile=profile());wait(runner)
    assert runner.state['status']=='await_levels'
    runner.start('continue');wait(runner)
    assert runner.state['status']=='await_room'
    runner=Runner(tmp_path/'auto',demo=True)
    runner.start('continue');wait(runner)
    assert runner.state['status']=='await_reference'
    runner.start('continue');wait(runner)
    assert runner.state['status']=='ready'
    assert (runner.directory/'camilladsp.yml').is_file()
    runner.start('apply');wait(runner)
    assert runner.state['status']=='applied'


def test_cancel_during_capture(tmp_path):
    class Slow(Simulated):
        def capture(self,signal,cancel,meter=None,**kw):
            cancel.wait(5)
            raise InterruptedError('Stopped')
    runner=Runner(tmp_path,demo=True,backend_factory=Slow)
    runner.start(profile=profile())
    runner.mute();runner.thread.join(10)
    assert runner.state['status']=='cancelled'
    assert runner.state['running'] is False


def test_deployment_failure_restores_state_and_stops(tmp_path,monkeypatch):
    import yaml
    cfg=profile();state=tmp_path/'state.yml';original='config_path: old.yml\n'
    state.write_text(original)
    cfg['camilla']['statefile']=str(state)
    conf=tmp_path/'new.yml';conf.write_text('filters: {}')
    class Backend:
        demo=False
        actions=[]
        def systemctl(self,action):self.actions.append(action)
    backend=Backend()
    monkeypatch.setattr('octetmeasure.autotune.camilla.subprocess.run',lambda *a,**k:None)
    monkeypatch.setattr('octetmeasure.autotune.camilla.time.sleep',lambda *a:None)
    def fail():raise ValueError('acoustic mismatch')
    with pytest.raises(ValueError,match='acoustic mismatch'):
        Deployment(cfg,backend,tmp_path/'run').apply(conf,fail)
    assert state.read_text()==original
    assert backend.actions[-1]=='stop'
    assert not (tmp_path/'run'/'last-good.yml').exists()
