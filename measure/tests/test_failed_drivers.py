"""Unavailable physical outputs never prevent measuring the working outputs."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.backend import Simulated
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.runner import Runner
from octetmeasure.autotune.web import Controller, check_safety


def profile():
    cfg = config.example()
    cfg.update(duration=1, peq_bands=1, automatic=True, room_positions=1)
    for driver in cfg['drivers']:
        driver.update(start_db=-30, max_db=-30)
    return cfg


def level_runner(tmp_path, monkeypatch, missing):
    cfg = profile()
    runner = Runner(tmp_path, demo=True)
    runner.state = dict(id='levels', cfg=cfg, stage='levels', levels={}, status='running')
    runner.directory.mkdir()
    checked = []
    def capture(signal):
        channel = int(np.argmax(np.max(abs(signal), axis=0)))
        checked.append(channel+1)
        return np.full(len(signal)+4800, 1e-5)
    monkeypatch.setattr(runner, '_capture', capture)
    monkeypatch.setattr(dsp, '_arrival', lambda *a: 0)
    monkeypatch.setattr(dsp, 'transfer', lambda rec,x,fs,f: (
        np.full(len(f), (1e-9 if checked[-1] in missing else .1)+0j), np.zeros(100)))
    return runner, checked


def test_missing_channel_three_continues_through_channel_eight(tmp_path, monkeypatch):
    runner, checked = level_runner(tmp_path, monkeypatch, {3})
    runner._levels()
    assert sorted(checked) == list(range(1,9))
    assert runner.state['cfg']['excluded_drivers'] == ['L_b']
    assert set(runner.state['failed_drivers']) == {'L_b'}
    assert 'L_tweeter' in runner.state['levels'] and 'R_b' in runner.state['levels']
    assert runner.state['failed_drivers']['L_b']['output'] == 3
    assert 'minimum' in runner.state['failed_drivers']['L_b']['reason']


def test_no_working_drivers_is_reported_only_after_every_output_checked(tmp_path, monkeypatch):
    runner, checked = level_runner(tmp_path, monkeypatch, set(range(1,9)))
    with pytest.raises(ValueError, match='No usable drivers'):
        runner._levels()
    assert sorted(checked) == list(range(1,9))
    assert len(runner.state['failed_drivers']) == 8


def test_valid_signal_below_preferred_target_is_retained_at_cap(tmp_path, monkeypatch):
    runner, _ = level_runner(tmp_path, monkeypatch, set())
    monkeypatch.setattr(dsp, 'transfer', lambda rec,x,fs,f: (
        np.full(len(f), 10**(-52/20)+0j), np.zeros(100)))
    runner._levels()
    assert not runner.state.get('failed_drivers')
    assert all(15 < row['signal_margin_db'] < 24 for row in runner.state['level_report'].values())
    assert not any(row['target_reached'] for row in runner.state['level_report'].values())


@pytest.mark.parametrize('failure_kind',['signal','eq'])
def test_driver_lost_during_repeated_measurement_is_excluded_individually(tmp_path, monkeypatch, failure_kind):
    cfg = profile()
    cfg['measurement_repeats'] = 1
    runner = Runner(tmp_path, demo=True)
    runner.state = dict(id='measurement',cfg=cfg,stage='measure',status='running',
                        levels={d['id']:-30 for d in cfg['drivers']})
    runner.directory.mkdir()
    f = np.geomspace(20,21000,200)
    responses = {d['id']:dict(h=np.full(len(f),1e-9 if d['id']=='L_b' and failure_kind=='signal' else .1,dtype=complex),
        flatten=np.ones(len(f)),valid=np.ones(len(f),bool)) for d in cfg['drivers']}
    monkeypatch.setattr(runner,'_capture',lambda signal:np.full(len(signal)+4800,1e-5))
    monkeypatch.setattr(dsp,'extract',lambda *a:(f,responses,{key:np.zeros(100) for key in responses},0))
    def flatten(cfg,f,responses):
        if failure_kind == 'eq' and 'L_b' not in cfg.get('excluded_drivers',[]):
            raise dsp.DriverError('L_b','Driver EQ could not be fitted.')
        return {key:[] for key in responses}
    monkeypatch.setattr(dsp,'flatten',flatten)
    assert runner._measure() == 'baseline'
    assert runner.state['cfg']['excluded_drivers'] == ['L_b']
    assert 'L_b' not in runner.state['eq'] and 'R_b' in runner.state['eq']
    with np.load(runner.directory/'drivers.npz') as data:
        assert 'L_b' not in data and 'R_b' in data


def test_shared_microphone_fault_still_stops_and_preserves_error(tmp_path):
    class BrokenMic(Simulated):
        def capture(self,*args,**kwargs):
            raise RuntimeError('Capture discontinuity: input overflow')
    runner = Runner(tmp_path,demo=True,backend_factory=BrokenMic)
    runner.start(profile=profile())
    runner.thread.join(10)
    state = runner.status()
    assert state['status'] == 'failed'
    assert not state.get('failed_drivers')
    assert 'Capture discontinuity' in state['last_error']['message']
    runner.state.update(status='idle',message='Previous profile restored')
    runner.save()
    reloaded = Runner(tmp_path,demo=True).status()
    assert reloaded['failure_history'][0]['stage'] == 'levels'
    assert reloaded['last_error']['message'] == state['last_error']['message']


@pytest.mark.parametrize('working', [None, ['L_sub'], ['R_a']])
def test_complete_auto_run_applies_only_outputs_it_can_hear(tmp_path, monkeypatch, working):
    cfg = profile()
    cfg['test_attenuation_db'] = 9
    if working is None:
        working = [d['id'] for d in cfg['drivers'] if d['id'] != 'L_b']
    missing = {d['output']-1 for d in cfg['drivers'] if d['id'] not in working}
    monkeypatch.setattr(dsp,'minimize',lambda cost,x,**kw:SimpleNamespace(x=x))
    playback = []
    class MissingOutputs(Simulated):
        def capture(self, signal, cancel, meter=None, loopback=False):
            if not loopback:
                excluded = {d['output']-1 for d in self.cfg['drivers'] if d['id'] in self.cfg.get('excluded_drivers',[])}
                assert not excluded or not np.any(signal[:,list(excluded)])
                playback.append(np.max(abs(signal),axis=0))
                signal = signal.copy()
                signal[:,list(missing)] = 0
            return super().capture(signal,cancel,meter,loopback=loopback)
    controller = Controller(tmp_path,demo=True)
    controller.runner.set_profile(yaml.safe_dump(cfg))
    controller.runner.backend_factory = MissingOutputs
    controller.start(dict(microphone_ready=True,name='Usable outputs'))
    controller.runner.thread.join(180)
    state = controller.runner.status()
    assert not controller.runner.running
    assert state['status'] == 'applied', state['message']
    assert {d['id'] for d in config.active_drivers(state['cfg'])} == set(working)
    assert set(state['failed_drivers']) == {d['id'] for d in cfg['drivers'] if d['id'] not in working}
    saved = controller.status()['active']
    assert set(saved['active_driver_ids']) == set(working)
    assert saved['failed_drivers'] == state['failed_drivers']
    assert state.get('stereo_skipped')
    assert not state['stereo_verification']['passed']
    document, _ = export(state['cfg'],state['chosen'],state['eq'],state['room'])
    check_safety(document,limits=True)
    for d in cfg['drivers']:
        route = next(r for r in document['mixers']['drivers']['mapping'] if r['dest']==d['output']-1)
        assert bool(route['sources']) == (d['id'] in working)
        assert document['filters'][d['id']+'_gain']['parameters']['mute'] == (d['id'] not in working)
    measurements = controller.measurements(saved['id'])
    assert {d['id'] for d in measurements['drivers']} == set(working)
    assert measurements['failed_drivers'] == saved['failed_drivers']
    assert 'skipped' in measurements['notes']
    assert all(np.max(row) <= 10**(-39/20)*1.00001 for row in playback)


@pytest.mark.parametrize('excluded', ['L_b', ['unknown'], ['L_b','L_b'], [None]])
def test_invalid_exclusions_are_rejected(excluded):
    with pytest.raises(ValueError,match='excluded_drivers'):
        config.validate(dict(profile(),excluded_drivers=excluded))
