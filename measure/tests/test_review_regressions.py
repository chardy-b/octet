"""Reproduce the three material findings without audio playback or network discovery."""
from types import SimpleNamespace
import threading

import numpy as np
import pytest
import yaml

from octetmeasure import capture, peq_workflow, upnp
from octetmeasure.audio import RealIO
from octetmeasure.autotune import config
from octetmeasure.autotune.backend import Hardware, Simulated
from octetmeasure.autotune.runner import Runner
from octetmeasure.autotune.web import Controller
from octetmeasure.session import RecordConfig
from octetmeasure.ui.jobs import JobManager


def test_controller_snapshots_before_handoff_and_does_not_open_microphone(tmp_path, monkeypatch):
    controller = Controller(tmp_path)
    cfg = config.example()
    cfg.update(hardware_confirmed=True, input_device='Audient', output_device='DAC', calibration=__file__)
    controller.runner.set_profile(yaml.safe_dump(cfg))
    actions = []
    monkeypatch.setattr(controller, 'snapshot', lambda cfg: actions.append('snapshot'))
    monkeypatch.setattr(controller.runner, 'start', lambda **kw: actions.append('handoff'))
    import sounddevice as sd
    monkeypatch.setattr(sd, 'rec', lambda *a, **k: pytest.fail('Input must not open before the worker takes over'))
    controller.start({'microphone_ready': True})
    assert actions == ['snapshot', 'handoff']


@pytest.mark.parametrize('outcome', ['success', 'failure', 'recovery_failure', 'cancel'])
def test_worker_preflight_runs_after_takeover_and_recovers_before_tones(tmp_path, outcome):
    actions = []
    class Backend(Simulated):
        def take_dac(self): actions.append('takeover')
        def preflight(self):
            actions.append('preflight')
            assert actions == ['takeover', 'preflight']
            if outcome == 'cancel': raise InterruptedError('Stopped')
            if outcome != 'success': raise ValueError('Microphone unavailable')
        def restore_preflight(self):
            actions.append('restore')
            if outcome == 'recovery_failure': raise RuntimeError('service failed')
        def systemctl(self, action): actions.append(action)
        def stop(self): actions.append('cancel_streams')
    runner = Runner(tmp_path, demo=False, backend_factory=Backend)
    # Exercise hardware failure cleanup with an entirely simulated backend.
    cfg = config.example()
    cfg.update(hardware_confirmed=True, input_device='Audient', output_device='DAC', calibration=__file__)
    def levels():
        actions.append('tones')
        runner.state['status'] = 'await_levels'
    runner._levels = levels
    runner.start(profile=cfg)
    runner.thread.join(5)
    assert not runner.running
    if outcome == 'success':
        assert actions == ['takeover', 'preflight', 'tones']
    else:
        assert 'tones' not in actions
        assert runner.state['status'] == ('cancelled' if outcome == 'cancel' else 'failed')
        assert runner.state['audio_owned'] == (outcome != 'failure')
        assert ('restore' in actions) == (outcome != 'cancel')
        assert ('stop' in actions) == (outcome != 'failure')


def test_takeover_releases_capture_before_preflight_and_restores_only_active_services(monkeypatch):
    import octetmeasure.autotune.backend as backend
    import octetmeasure.autotune.camilla as camilla
    import sounddevice as sd
    cfg = config.example()
    hw = object.__new__(Hardware)
    hw.cfg, hw.cancel = cfg, threading.Event()
    actions = []
    active = {'camilladsp': True, 'mpd': True, 'upmpdcli': False}
    monkeypatch.setattr(backend.subprocess, 'run', lambda args, **kw: SimpleNamespace(returncode=0 if active[args[-1]] else 3))
    monkeypatch.setattr(hw, 'systemctl', lambda action, service=None: actions.append((action, service or 'camilladsp')))
    monkeypatch.setattr(camilla, 'control', lambda commands: [-28, True] if commands[0][0] == 'GetVolume' else ['Running'])
    monkeypatch.setattr(hw, 'master', lambda volume, muted: actions.append(('master', volume, muted)))
    monkeypatch.setattr(sd, '_terminate', lambda: actions.append('refresh-stop'))
    monkeypatch.setattr(sd, '_initialize', lambda: actions.append('refresh-start'))
    def ambient(*a, **k):
        assert ('stop', 'camilladsp') in actions
        assert actions[-1] == 'refresh-start'
        return np.full((24000, 2), .001)
    monkeypatch.setattr(sd, 'rec', ambient)
    hw.take_dac();hw.preflight();hw.restore_preflight()
    assert ('start', 'camilladsp') in actions and ('start', 'mpd') in actions
    assert ('start', 'upmpdcli') not in actions
    assert ('master', -28, True) in actions


def test_resume_reacquires_ownership_after_recovered_preflight(tmp_path):
    attempts = []
    class Backend(Simulated):
        def preflight(self):
            assert runner.state['audio_owned']
            attempts.append('preflight')
            if len(attempts) == 1:
                raise ValueError('Microphone unavailable')
        def restore_preflight(self): pass
    runner = Runner(tmp_path, demo=True, backend_factory=Backend)
    def levels(): runner.state.update(status='await_levels')
    runner._levels = levels
    runner.start(profile=config.example());runner.thread.join(5)
    assert runner.state['status'] == 'failed' and not runner.state['audio_owned']
    runner.start('resume');runner.thread.join(5)
    assert runner.state['status'] == 'await_levels' and runner.state['audio_owned']
    assert len(attempts) == 2


@pytest.mark.parametrize('grids', [
    (np.geomspace(100, 10000, 101), np.geomspace(200, 8000, 79)),
    (np.geomspace(100, 10000, 101), np.geomspace(200, 20000, 101)),
])
def test_eq_aligns_different_sweep_ranges_before_power_average(monkeypatch, grids):
    recordings = {'one': 0, 'two': 1}
    monkeypatch.setattr(peq_workflow.store, 'load_recording', lambda name: (recordings[name], SimpleNamespace(f1=20, f2=20000), {}))
    def measure(index, *a, **k):
        f = grids[index]
        # Same frequency-dependent curve, but the second measurement is 6 dB louder.
        return SimpleNamespace(response=SimpleNamespace(freq=f, mag_db_raw=8*np.log2(f/1000)+6*index))
    monkeypatch.setattr(peq_workflow.analysis, 'measure', measure)
    monkeypatch.setattr(peq_workflow.peqmod, 'fit_peq', lambda *a, **k: ([], {}))
    fitted = peq_workflow.fit_measurements(['one', 'two'], hp='off', lp='off')
    f = fitted['freq']
    expected = 8*np.log2(f/1000)+10*np.log10((1+10**.6)/2)
    assert f[0] >= max(grid[0] for grid in grids)
    assert f[-1] <= min(grid[-1] for grid in grids)
    assert np.allclose(fitted['mag'], expected, atol=1e-10)
    assert fitted['f_lo'] >= f[0] and fitted['f_hi'] <= f[-1]


def test_eq_rejects_empty_or_disjoint_measurement_ranges(monkeypatch):
    with pytest.raises(ValueError, match='at least one'):
        peq_workflow.fit_measurements([])
    monkeypatch.setattr(peq_workflow.store, 'load_recording', lambda name: (name, SimpleNamespace(f1=20, f2=20000), {}))
    def measure(name, *a, **k):
        f = np.geomspace(*(100, 200) if name == 'low' else (1000, 2000), 20)
        return SimpleNamespace(response=SimpleNamespace(freq=f, mag_db_raw=np.zeros(len(f))))
    monkeypatch.setattr(peq_workflow.analysis, 'measure', measure)
    with pytest.raises(ValueError, match='overlapping'):
        peq_workflow.fit_measurements(['low', 'high'])


@pytest.mark.parametrize('kind', ['record', 'noise', 'monitor', 'clock'])
def test_missing_microphone_reports_error_and_job_idle(kind, monkeypatch):
    monkeypatch.setattr(capture, 'preferred_api', lambda: 'ALSA')
    monkeypatch.setattr(capture, 'list_devices', lambda: [])
    events = []
    manager = JobManager(RealIO(), SimpleNamespace(publish=events.append))
    manager.start(kind, RecordConfig(in_device='unplugged microphone', player='manual', save=False, duration=1))
    manager.thread.join(5)
    assert not manager.thread.is_alive()
    assert 'unplugged microphone' in manager.last_error
    assert [e['type'] for e in events][-2:] == ['error', 'idle']


def test_missing_renderer_reports_error_and_idle(monkeypatch):
    monkeypatch.setattr(capture, 'find_device', lambda *a, **k: 0)
    monkeypatch.setattr(upnp, 'discover', lambda *a: [])
    events = []
    manager = JobManager(RealIO(), SimpleNamespace(publish=events.append))
    manager.start('record', RecordConfig(renderer='missing player', player='dlna', save=False, duration=1))
    manager.thread.join(5)
    assert manager.last_error and 'network player' in manager.last_error
    assert [e['type'] for e in events][-2:] == ['error', 'idle']


def test_missing_default_device_reports_normal_exception(monkeypatch):
    monkeypatch.setattr(capture, 'default_device', lambda kind: None)
    with pytest.raises(RuntimeError, match='No input device'):
        capture.find_device('', 'input')


def test_cli_missing_device_keeps_friendly_exit(monkeypatch, capsys):
    from octetmeasure import cli
    monkeypatch.setattr(capture, 'find_device', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Microphone unplugged')))
    with pytest.raises(SystemExit) as error:
        cli.main(['record', 'test', '--in-device', 'missing'])
    assert error.value.code == 1
    assert 'Microphone unplugged' in capsys.readouterr().err
