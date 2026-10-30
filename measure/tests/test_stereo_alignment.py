"""Stereo timing uses one acoustic clock and retains the driver alignment."""
import threading

import numpy as np
import pytest

from octetmeasure.autotune import config, dsp, stereo
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.web import check_safety
from octetmeasure.autotune.runner import Runner


@pytest.mark.parametrize('lag,polarity,gain',[(2.,-1,4.),(-1.25,1,-3.),(0.,1,0.)])
def test_known_delay_level_and_polarity_are_corrected(lag,polarity,gain):
    cfg = config.example()
    fs = cfg['fs']
    left = np.zeros(fs//4);left[int(.04*fs)] = 1
    right = dsp.apply_stereo(left,dict(delay_ms=abs(lag),gain_db=gain,polarity=polarity),fs)
    if lag<0:
        left,right = right,left
    f = np.geomspace(30,15000,480)
    def responses(a,b):
        freq = np.fft.rfftfreq(len(a),1/fs)
        def h(x):
            y=np.fft.rfft(x)
            return np.interp(f,freq,y.real)+1j*np.interp(f,freq,y.imag)
        return dict(L=h(a),R=h(b),mono=h(a+b))
    report = stereo.metrics(cfg,f,responses(left,right),dict(L=left,R=right))
    assert report['right_minus_left_ms']==pytest.approx(lag,abs=.03)
    assert report['relative_polarity']==polarity
    settings = stereo.correction(cfg,report)
    corrected = {s:dsp.apply_stereo(ir,settings[s],fs) for s,ir in [('L',left),('R',right)]}
    result = stereo.metrics(cfg,f,responses(corrected['L'],corrected['R']),corrected)
    assert stereo.verify(cfg,result)['passed']
    assert all(s['gain_db']<=0 and s['delay_ms']>=0 for s in settings.values())


def test_ambiguous_polarity_and_residual_error_cannot_pass():
    cfg = config.example()
    with pytest.raises(ValueError,match='ambiguous'):
        stereo.correction(cfg,dict(polarity_confident=False))
    good = dict(polarity_confident=True,correlation=.99,right_minus_left_ms=0,
                right_minus_left_db=0,mono_prediction_error_db=0,mono_summing_loss_db=0)
    for change in [dict(correlation=-.99),dict(right_minus_left_ms=1),
                   dict(right_minus_left_db=3),dict(mono_summing_loss_db=6)]:
        with pytest.raises(ValueError,match='verification failed'):
            stereo.verify(cfg,dict(good,**change))


@pytest.mark.parametrize('ppm',[-80,65])
def test_shared_clock_preserves_relative_stereo_delay(ppm):
    cfg=config.example();cfg.update(duration=1,selected_pairs=['a'])
    fs=cfg['fs']
    def render(side,x):
        out=np.zeros((len(x)+int(.1*fs),8))
        out[:len(x),0 if side=='L' else 1]=x
        return out
    playback,slots=stereo.sequence(cfg,render,dict(L_a=-36,R_a=-36))
    y=np.pad(playback[:,0],(12000,fs))+np.pad(-.7*playback[:,1],(12096,fs-96))
    ratio=1+ppm*1e-6
    rec=np.interp(np.arange(int(len(y)*ratio))/ratio,np.arange(len(y)),y)
    f,h,ir,estimate=stereo.extract(rec,playback,slots,cfg)
    result=stereo.metrics(cfg,f,h,ir)
    assert estimate==pytest.approx(ppm,abs=3)
    assert result['right_minus_left_ms']==pytest.approx(2,abs=.03)
    assert result['relative_polarity']==-1 and result['polarity_confident']
    assert result['right_minus_left_db']==pytest.approx(20*np.log10(.7),abs=.1)
    assert result['mono_prediction_error_db']<.2


def test_common_stereo_export_preserves_limits_and_internal_delays():
    cfg=config.example();cfg['selected_pairs']=['a','sub','tweeter']
    roles=cfg['selected_pairs']
    candidate=dict(strategy='single',order=4,low=300,high=4000,split=1000,
                   gains=dict.fromkeys(roles,0),delays=dict(a=.2,sub=0,tweeter=1),
                   polarity=dict.fromkeys(roles,1))
    chosen=dict(L=candidate,R=candidate)
    eq={d['id']:[] for d in config.active_drivers(cfg)}
    plain,scales=export(cfg,chosen,eq,{})
    setting=dict(L=dict(delay_ms=2.127,gain_db=-3,polarity=-1))
    doc,new_scales=export(cfg,chosen,eq,{},setting)
    check_safety(doc,limits=True)
    assert scales==new_scales
    for name,value in plain['filters'].items():
        assert doc['filters'][name]==value
    assert doc['pipeline'][0]['names'][0]=='stereo_L_gain'
    assert doc['pipeline'][1]['type']=='Mixer'
    assert doc['filters']['stereo_L_gain']['parameters']['inverted']
    impulse=np.zeros(1000);impulse[10]=1
    expected=dsp.apply_stereo(impulse,setting['L'],cfg['fs'])
    params=doc['filters']['stereo_L_delay']['parameters']
    coeff=doc['filters']['stereo_L_fractional']['parameters']['values']
    actual=np.pad(np.convolve(impulse,coeff),(params['delay'],0))[:len(impulse)]*(-10**(-3/20))
    assert np.allclose(expected,actual,atol=1e-12)


def test_active_polarity_fault_rolls_back_even_when_side_magnitudes_match(tmp_path,monkeypatch):
    cfg=config.example()
    old_state='config_path: previous.yml\n'
    statefile=tmp_path/'state.yml';statefile.write_text(old_state)
    cfg['camilla']['statefile']=str(statefile)
    runner=Runner(tmp_path/'runs',demo=True)
    runner.state=dict(id='c'*32,cfg=cfg,chosen=dict(L={},R={}),eq={},room=dict(L=[],R=[]))
    runner.directory.mkdir(parents=True)
    (runner.directory/'camilladsp.yml').write_text('filters: {}')
    f=np.geomspace(30,15000,100);mag=np.zeros(100)
    for side in ('L','R'):
        np.savez(runner.directory/f'final-{side}.npz',after=mag)
    class Backend:
        demo=False
        actions=[]
        def systemctl(self,action):self.actions.append(action)
    runner.backend=Backend()
    monkeypatch.setattr('octetmeasure.autotune.camilla.subprocess.run',lambda *a,**k:None)
    monkeypatch.setattr('octetmeasure.autotune.camilla.time.sleep',lambda *a:None)
    monkeypatch.setattr(runner,'_sum',lambda *a,**k:(f,mag,np.zeros(100),np.zeros(100)))
    bad=dict(polarity_confident=True,relative_polarity=-1,correlation=-.99,
        right_minus_left_ms=0,right_minus_left_db=0,mono_prediction_error_db=0,mono_summing_loss_db=20)
    monkeypatch.setattr(runner,'_stereo_capture',lambda *a,**k:dict(bad,repeat_reports=[bad]*3))
    with pytest.raises(ValueError,match='Stereo alignment/polarity verification failed'):
        runner._apply()
    assert statefile.read_text()==old_state
    assert runner.backend.actions[-1]=='stop'
    assert not (runner.directory/'last-good.yml').exists()
    assert runner.state.get('status')!='applied'
