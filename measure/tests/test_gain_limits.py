"""Playback headroom and SNR-driven measurements use independent limits."""
import copy

import numpy as np
import pytest

from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.runner import Runner


def setup_profile():
    cfg = config.example()
    cfg.update(automatic=True, room_positions=1, selected_pairs=['a','sub','tweeter'],playback_max_db=-10)
    for d in cfg['drivers']:
        d.update(start_db=-54, max_db=-30)
    roles = ('sub','a','tweeter')
    c = dict(strategy='single',order=4,low=300,high=4000,split=1000,
             gains=dict(sub=-3,a=-6,tweeter=0), delays=dict.fromkeys(roles,0),polarity=dict.fromkeys(roles,1))
    return cfg,{side:copy.deepcopy(c) for side in ('L','R')},{d['id']:[] for d in config.active_drivers(cfg)}


def test_playback_gain_does_not_depend_on_measurement_cap():
    cfg,chosen,eq = setup_profile()
    first,scales = export(cfg,chosen,eq,{})
    for d in cfg['drivers']:d['max_db']=-15
    second,other = export(cfg,chosen,eq,{})
    assert first == second and scales == other
    assert all(v['parameters']['clip_limit']==-10 for v in second['filters'].values() if v['type']=='Limiter')


def test_explicit_playback_revision_recovers_twenty_db_and_keeps_filters():
    cfg,chosen,eq = setup_profile()
    cfg.pop('playback_max_db')
    legacy,old_scale = export(cfg,chosen,eq,{})
    cfg['playback_max_db']=-10
    revised,new_scale = export(cfg,chosen,eq,{})
    assert np.allclose([new_scale[s]/old_scale[s] for s in ('L','R')],10)
    assert legacy['pipeline'] == revised['pipeline']
    assert legacy['mixers'] == revised['mixers']
    for name,fil in legacy['filters'].items():
        other = revised['filters'][name]
        if fil['type']=='Gain' and not fil['parameters']['mute']:
            assert other['parameters']['gain']-fil['parameters']['gain'] == pytest.approx(20)
            assert other['parameters']['inverted'] == fil['parameters']['inverted']
        elif fil['type']=='Limiter':
            assert other['parameters']['clip_limit']==-10
        else:
            assert fil == other
    validated=config.validate(dict(cfg,playback_max_db=-10))
    del validated['playback_max_db']
    assert 'playback_max_db' not in config.validate(validated)


def test_zero_dbfs_playback_ceiling_removes_ten_db_but_keeps_eq_headroom():
    cfg,chosen,eq=setup_profile()
    room={side:[dict(kind='PK',fc=10000,gain_db=3,q=1)] for side in ('L','R')}
    before,old=export(cfg,chosen,eq,room)
    cfg['playback_max_db']=0
    config.validate(cfg)
    after,new=export(cfg,chosen,eq,room)
    assert np.allclose([20*np.log10(new[s]/old[s]) for s in ('L','R')],10)
    assert all(0<value<1 for value in new.values())
    for key,fil in after['filters'].items():
        if fil['type']=='Limiter':
            assert fil['parameters']['clip_limit']==0
        elif fil['type']=='Gain' and not fil['parameters']['mute']:
            assert fil['parameters']['gain']-before['filters'][key]['parameters']['gain']==pytest.approx(10)
        else:
            assert fil==before['filters'][key]
    assert cfg['drivers'][0]['max_db']==-30


@pytest.mark.parametrize('changes',[dict(playback_max_db=1),dict(playback_max_db=float('nan')),
                                    dict(target_snr_db=10),dict(target_snr_db=80)])
def test_gain_and_quality_settings_are_validated(changes):
    cfg,_,_ = setup_profile()
    cfg.update(changes)
    with pytest.raises(ValueError):config.validate(cfg)


def test_level_check_starts_at_known_level_and_stops_at_target(tmp_path,monkeypatch):
    cfg,_,_ = setup_profile()
    cfg['selected_pairs']=['a']
    for d in cfg['drivers']:d['max_db']=-15
    runner=Runner(tmp_path,demo=True)
    runner.state=dict(id='c'*32,cfg=cfg,levels={},status='running')
    prior={d['id']:dict(output_db=-39,margin_db=21) for d in config.active_drivers(cfg)}
    monkeypatch.setattr(runner,'_reference_levels',lambda:prior)
    checks=[]
    def capture(signal):
        checks.append(float(np.max(abs(signal))))
        return np.full(48000,1e-4)
    monkeypatch.setattr(runner,'_capture',capture)
    monkeypatch.setattr(dsp,'_arrival',lambda *a:0)
    monkeypatch.setattr(dsp,'transfer',lambda rec,x,fs,f:(np.full(len(f),.1+0j),np.zeros(10)))
    runner._levels()
    assert runner.state['levels']=={'L_a':-36,'R_a':-36}
    assert len(checks)==4
    for reading in runner.state['level_report'].values():
        assert reading['checks']==2 and reading['signal_margin_db']>=24
    assert checks[1]/checks[0] == pytest.approx(10**(3/20))


def test_full_measurement_rechecks_margin_and_only_raises_weak_drivers(tmp_path,monkeypatch):
    cfg,_,_ = setup_profile()
    cfg['selected_pairs']=['a']
    for d in cfg['drivers']:d['max_db']=-15
    runner=Runner(tmp_path,demo=True)
    runner.state=dict(id='d'*32,cfg=cfg,levels={'L_a':-39,'R_a':-36},status='running')
    runner.directory.mkdir()
    monkeypatch.setattr(runner,'_capture',lambda signal:np.full(len(signal),1e-4))
    f=np.geomspace(180,10000,50)
    responses={key:dict(h=np.full(len(f),.1+0j),flatten=np.full(len(f),.1),valid=np.ones(len(f),dtype=bool)) for key in runner.state['levels']}
    monkeypatch.setattr(dsp,'extract',lambda *args:(f,responses,{key:np.zeros(100) for key in responses},0))
    monkeypatch.setattr(dsp,'flatten',lambda *args:{key:[] for key in responses})
    assert runner._measure()=='measure'
    assert runner.state['levels']=={'L_a':-36,'R_a':-36}
    assert runner._measure()=='baseline'
    runner.state['levels']['L_a']=-15
    responses['L_a']['h'][:]=1e-5
    assert runner._measure() == 'baseline'
    assert runner.state['cfg']['excluded_drivers'] == ['L_a']
    assert 'measurement ceiling' in runner.state['failed_drivers']['L_a']['reason']
    assert [d['id'] for d in config.active_drivers(cfg)] == ['R_a']
