"""Selected topology, quiet checks, and saved-profile silence through a complete run."""
import itertools
import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.backend import Simulated
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.web import Controller, check_safety


SELECTIONS = [list(pairs) for n in range(1, 5) for pairs in itertools.combinations(config.PAIRS, n)
              if {'a', 'b'}.intersection(pairs)]


def initial_search(monkeypatch):
    def minimize(cost, x, **kwargs):
        assert len(x) == len(kwargs['bounds'])
        assert np.isfinite(cost(x))
        return SimpleNamespace(x=x)
    monkeypatch.setattr(dsp, 'minimize', minimize)


@pytest.mark.parametrize('pairs', [[], ['sub', 'tweeter'], ['a', 'a'], ['unknown'], 'a', [None]])
def test_bad_selection_is_rejected(pairs):
    cfg = config.example()
    cfg['selected_pairs'] = pairs
    with pytest.raises(ValueError, match='pairs|exciter'):
        config.validate(cfg)


@pytest.mark.parametrize('pairs', SELECTIONS)
def test_all_supported_topologies_keep_excluded_outputs_silent(pairs, monkeypatch):
    initial_search(monkeypatch)
    cfg = config.example()
    cfg.update(selected_pairs=pairs, duration=1)
    cfg = config.validate(cfg)
    drivers = config.active_drivers(cfg)
    levels = {d['id']: -36 for d in drivers}
    signal, slots = dsp.sequence(cfg, levels)
    assert {slot['id'] for slot in slots[1:-1]} == set(levels)
    excluded = [d['output']-1 for d in cfg['drivers'] if d not in drivers]
    assert not np.any(signal[:, excluded])
    f = np.geomspace(20, 20000, 720)
    responses = {d['id']: dict(h=np.ones(len(f), dtype=complex)) for d in drivers}
    eq = {d['id']: [] for d in drivers}
    chosen = {side: dsp.search(cfg, f, responses, eq, side)[0] for side in ('L', 'R')}
    for side, candidate in chosen.items():
        assert set(candidate['gains']) == set(pairs)
        assert max(candidate['gains'].values()) == 0
        assert min(candidate['delays'].values()) == 0
        expected = ('both', 'split') if {'a', 'b'}.issubset(pairs) else ('single',)
        assert candidate['strategy'] in expected
        out, _ = dsp.render_chain(cfg, side, candidate, eq, np.ones(4800)*.001)
        assert not np.any(out[:, excluded])
    doc, _ = export(cfg, chosen, eq, {})
    check_safety(doc, limits=True)
    assert doc['devices']['playback']['channels'] == 8
    for d in cfg['drivers']:
        route = next(m for m in doc['mixers']['drivers']['mapping'] if m['dest'] == d['output']-1)
        muted = doc['filters'][d['id']+'_gain']['parameters']['mute']
        assert muted == (d not in drivers)
        assert bool(route['sources']) == (d in drivers)


def test_b_only_pair_supplies_reliable_clock_markers():
    cfg = config.example()
    cfg.update(selected_pairs=['b'], duration=1)
    levels = {d['id']: -36 for d in config.active_drivers(cfg)}
    signal, slots = dsp.sequence(cfg, levels)
    rec = Simulated(cfg, ppm=65).capture(signal, threading.Event())
    _, responses, _, ppm = dsp.extract(rec, signal, slots, cfg)
    assert set(responses) == {'L_b', 'R_b'}
    assert abs(ppm-65) < 4


def test_automatic_subset_reuses_levels_and_verifies_without_b(tmp_path, monkeypatch):
    initial_search(monkeypatch)
    cfg = config.example()
    cfg.update(duration=1, peq_bands=1)
    for d in cfg['drivers']:
        d.update(start_db=-54, max_db=-30)
    old = tmp_path/('f'*32)
    old.mkdir()
    report = {d['id']: dict(output_db=-30, margin_db=30 if d['role']=='a' else 27)
              for d in cfg['drivers'] if d['role'] != 'b'}
    (old/'run.json').write_text(json.dumps(dict(id=old.name, cfg=cfg, demo=True, signal_report=report)))
    buffers = []
    class DisconnectedB(Simulated):
        def capture(self, signal, cancel, meter=None, **kwargs):
            if not kwargs.get('loopback'):
                assert not np.any(signal[:, 2:4])
                buffers.append(np.max(abs(signal), axis=0))
            else:
                rendered=[]
                for i,side in enumerate(('L','R')):
                    out,scale=dsp.render_chain(self.cfg,side,self.applied['chosen'][side],self.applied['eq'],
                        signal[:,i],self.applied['room'][side],self.applied.get('stereo'))
                    rendered.append(out*self.applied['preamp'][side]/scale)
                signal=sum(rendered)
            # A complete physical right speaker is 2 ms later, 4 dB louder,
            # and wired with the opposite overall acoustic polarity.
            signal=signal.copy()
            for index in (1,3,5,7):
                signal[:,index]=dsp.apply_stereo(signal[:,index],dict(delay_ms=2,gain_db=4,polarity=-1),self.cfg['fs'])
            return super().capture(signal,cancel,meter)
    controller = Controller(tmp_path, demo=True)
    controller.runner.set_profile(yaml.safe_dump(cfg))
    controller.runner.backend_factory = DisconnectedB
    controller.start(dict(microphone_ready=True, selected_pairs=['a', 'sub', 'tweeter'], name='No B'))
    controller.runner.thread.join(180)
    state = controller.runner.status()
    assert not controller.runner.running
    assert state['status'] == 'applied', state['message']
    assert state['cfg']['measurement_repeats']==3
    assert len(state['driver_repeat_reports'])==3
    assert state['baseline_measured']
    assert state['stereo_before']['relative_polarity']==-1
    assert state['stereo']['L']['delay_ms']==pytest.approx(2,abs=.15)
    assert state['stereo']['R']['gain_db']==pytest.approx(-4,abs=.4)
    assert state['stereo']['R']['polarity']==-1
    assert state['active_stereo_verification']['passed']
    assert all(row['repeats']==3 for row in state['measurement_manifest'])
    for row in state['measurement_manifest']:
        assert len(list(controller.runner.directory.glob(f"measurement-{row['batch']}-*.npz")))==3
    assert state['level_reference'] == old.name
    assert set(state['levels']) == set(report)
    for d in config.active_drivers(state['cfg']):
        reading = state['level_report'][d['id']]
        assert reading['checks'] == 1 and reading['reused_recording']
        expected = -30
        assert reading['output_db'] == expected
        assert max(b[d['output']-1] for b in buffers) <= 10**(expected/20)*1.00001
    saved = controller.status()['profiles'][0]
    assert saved['selected_pairs'] == ['a', 'sub', 'tweeter']
    assert controller.status()['selected_pairs'] == ['a', 'sub', 'tweeter']
    assert len(controller.measurements(saved['id'])['drivers']) == 6
    curves=controller.measurements(saved['id'])
    assert curves['measurement_repeats']==3
    assert curves['systems'][0]['before_label']=='Before tuning (driver protection active)'
    assert curves['active_stereo_verification']['passed']
    controller.activate(saved['id'])
    assert controller.status()['active']['selected_pairs'] == ['a', 'sub', 'tweeter']


def test_invalid_selection_cannot_snapshot_or_start(tmp_path, monkeypatch):
    controller = Controller(tmp_path, demo=True)
    monkeypatch.setattr(controller, 'snapshot', lambda *args: pytest.fail('Invalid selection took a snapshot'))
    with pytest.raises(ValueError, match='exciter'):
        controller.start(dict(microphone_ready=True, selected_pairs=['sub']))
    assert not (tmp_path/'current.json').exists()
