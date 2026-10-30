"""Read saved acoustic measurements for one immutable listening profile."""
import json
from pathlib import Path
import re

import numpy as np
from scipy.signal import sosfreqz

from .. import peq
from . import dsp, config


def values(array):
    return [round(float(x), 3) if np.isfinite(x) else None for x in array]


def measurements(root, record):
    result = dict(profile=dict(id=record['id'], name=record['name'], created=record['created']),
                  available=False, systems=[], drivers=[])
    if record.get('measurement_mode') == 'magnitude_only_recovery':
        if not re.fullmatch(r'[a-f0-9]{32}',record['id']):
            raise ValueError('This profile has no valid saved recovery record.')
        path = Path(root)/record['id']/'recovery-report.json'
        if not path.is_file():
            return dict(result,message='The recovered measurement report is unavailable.')
        recovery = json.loads(path.read_text(encoding='utf-8'))
        drivers = recovery.get('driver_graphs',[])
        return dict(result,available=bool(drivers),drivers=drivers,
                    failed_drivers=record.get('failed_drivers',{}),measurement_repeats=1,
                    timing_reliable=False,acoustically_verified=False,
                    message='Frequency-response data recovered from the saved recording. New timing and polarity were not measured.',
                    notes='Single-capture magnitude recovery. Corrections use reliable frequency bands only. All new filters are cuts. Existing crossovers, channel gains, delays and polarity were retained. DSP predictions have not been acoustically verified; no test tones were played.')
    if record.get('kind') != 'tuned':
        return dict(result, message='This is a manual settings backup. No acoustic measurements were recorded for it.')
    identifier = record.get('measurement_run_id', record['id'])
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('This profile has no valid saved measurement run.')
    directory = Path(root)/identifier
    report = directory/'report.json'
    if not report.is_file():
        return dict(result, message='The measurement report for this profile is unavailable.')
    state = json.loads(report.read_text(encoding='utf-8'))
    cfg, chosen = state['cfg'], state['chosen']
    for side in ('L', 'R'):
        path = directory/f'final-{side}.npz'
        if not path.is_file():
            continue
        with np.load(path, allow_pickle=False) as data:
            f, before, after = data['freq'], data['before'], data['after']
            before_room = before.copy()
            untuned = directory/f'untuned-{side}.npz'
            if untuned.exists():
                with np.load(untuned,allow_pickle=False) as initial:
                    before = np.interp(f,initial['freq'],initial['mag'])
            band = (f >= 300) & (f <= 3000) & np.isfinite(before)
            reference = float(np.median(before[band])) if band.any() else 0.
            target = peq.harman_target(f, cfg['target_bass_db'], 105, cfg['target_tilt'])
            target -= np.median(target[band]) if band.any() else 0.
            result['systems'].append(dict(side=side, freq=values(f), before=values(before-reference),
                                           after=values(after-reference), target=values(target),
                                           before_room=values(before_room-reference),
                                           before_label='Before tuning (driver protection active)' if untuned.exists() else 'Before room EQ',
                                           crossover=chosen[side],
                                           verification=state.get('final_verification', {}).get(side)))
    path = directory/'drivers.npz'
    if path.is_file():
        with np.load(path, allow_pickle=False) as data:
            grid = data['freq']
            for driver in sorted(config.active_drivers(cfg), key=lambda d: d['output']):
                key, side = driver['id'], driver['side']
                if key not in data:
                    continue
                valid = (grid >= driver['min_hz']*1.15) & (grid <= driver['max_hz']/1.15)
                f, h = grid[valid], data[key][valid]
                if len(f) < 2:
                    continue
                predicted = h*dsp.branch_response(cfg, driver, chosen[side], state['eq'].get(key, []), f)
                room = state.get('room', {}).get(side, [])
                if room:
                    predicted *= sosfreqz(peq.sos_for([peq.Peq(**p) for p in room], cfg['fs']), worN=f, fs=cfg['fs'])[1]
                stereo = state.get('stereo',{}).get(side,{})
                predicted *= (dsp.delay_response(f,stereo.get('delay_ms',0),cfg['fs'])*
                              10**(stereo.get('gain_db',0)/20)*stereo.get('polarity',1))
                raw = 20*np.log10(np.maximum(abs(h), 1e-12))
                reference = np.median(raw[np.isfinite(raw)])
                corrected = 20*np.log10(np.maximum(abs(predicted), 1e-12))
                result['drivers'].append(dict(id=key, output=driver['output'], model=driver['model'], side=side,
                    freq=values(f), measured=values(peq.smooth_log(f, raw, 6)-reference),
                    predicted=values(peq.smooth_log(f, corrected, 6)-reference),
                    phase=values(np.angle(h, deg=True)), predicted_phase=values(np.angle(predicted, deg=True)),
                    delay_ms=chosen[side]['delays'][driver['role']]+state.get('stereo',{}).get(side,{}).get('delay_ms',0),
                    polarity=chosen[side]['polarity'][driver['role']]*state.get('stereo',{}).get(side,{}).get('polarity',1),
                    attenuation_db=chosen[side]['gains'][driver['role']]+state.get('stereo',{}).get(side,{}).get('gain_db',0)))
    result.update(available=bool(result['systems'] or result['drivers']),
                  message='Saved measurements for this profile. Viewing graphs does not apply the profile.',
                  measurement_repeats=cfg.get('measurement_repeats',1),
                  stereo=state.get('stereo'),stereo_verification=state.get('stereo_verification'),
                  active_stereo_verification=state.get('active_stereo_verification'),
                  failed_drivers=state.get('failed_drivers',{}),
                  active_driver_ids=[d['id'] for d in config.active_drivers(cfg)],
                  stereo_skipped=state.get('stereo_skipped'),
                  driver_polarity_report=state.get('driver_polarity_report'),
                  notes='System curves share the before-curve midrange reference. Driver curves use each driver’s measured median. Levels are relative dB, not calibrated SPL. Driver DSP curves are predictions; final system curves are measured.')
    if identifier != record['id']:
        result['notes'] += ' Measurements are retained from the original tuning run; playback gain and ceiling were revised afterward.'
    if state.get('failed_drivers'):
        result['notes'] += ' Unmeasurable outputs were skipped and remain muted: '+', '.join(
            str(row['output']) for row in state['failed_drivers'].values())+'.'
    if state.get('stereo_skipped'):
        result['notes'] += ' Stereo alignment was skipped: '+state['stereo_skipped']
    return result
