"""Pure signal generation, shared-clock extraction and crossover search."""
from dataclasses import asdict
import itertools

import numpy as np
from scipy.optimize import minimize
from scipy.signal import butter, correlate, correlation_lags, sosfilt, sosfreqz

from .. import peq
from ..sweep import make_sweep
from .config import TWEETER_MIN_HZ, active_drivers, active_roles, measurement_driver, measurement_ceiling


class DriverError(ValueError):
    def __init__(self, driver_id, message):
        self.driver_id = driver_id
        super().__init__(message)


def lr(freq, kind, order, fs):
    half = butter(order // 2, freq, btype=kind, fs=fs, output='sos')
    return np.vstack([half, half])


def protection(d, fs):
    if d['role'] == 'tweeter' and (d['min_hz'] < TWEETER_MIN_HZ or d['order'] not in (4, 8)):
        raise ValueError('Tweeter protection requires a 3500 Hz or higher LR4/LR8 high-pass.')
    return np.vstack([lr(d['min_hz'], 'highpass', d['order'], fs),
                      lr(d['max_hz'], 'lowpass', d['order'], fs)])


def protected_sweep(d, cfg, level, duration=None):
    limits = measurement_driver(cfg, d)
    if not limits['start_db'] <= level <= limits['max_db']:
        raise ValueError('Requested level exceeds driver ramp limits.')
    sw = make_sweep(cfg['fs'], duration or cfg['duration'], d['min_hz'], d['max_hz'], 0)
    x = sosfilt(protection(d, cfg['fs']), np.pad(sw.signal, (0, int(.15 * cfg['fs']))))
    x *= 10**(level/20) / max(1, np.max(np.abs(x)))
    return x


def sequence(cfg, levels):
    """One DAC buffer; identical protected acoustic markers bracket the eight sweeps."""
    fs, gap = cfg['fs'], int(cfg['gap_seconds'] * cfg['fs'])
    drivers = active_drivers(cfg)
    if not drivers:
        raise ValueError('No usable drivers remain for measurement.')
    pilot_driver = next((d for d in drivers if d['role'] in ('a', 'b')), drivers[0])
    pilot = protected_sweep(pilot_driver, cfg, levels[pilot_driver['id']], duration=1)
    chunks, slots, cursor = [np.zeros((gap, 8))], [], gap
    items = [(pilot_driver, pilot, 'pilot_start')]
    items += [(d, protected_sweep(d, cfg, levels[d['id']]), d['id']) for d in drivers]
    items += [(pilot_driver, pilot, 'pilot_end')]
    for d, x, name in items:
        block = np.zeros((len(x)+gap, 8))
        block[:len(x), d['output']-1] = x
        chunks.append(block)
        slots.append(dict(id=name, start=cursor, length=len(x), signal=x))
        cursor += len(block)
    return np.concatenate(chunks), slots


def _arrival(rec, signal):
    corr = correlate(rec, signal, mode='full', method='fft')
    index = int(np.argmax(np.abs(corr)))
    y = np.abs(corr)
    frac = 0.
    if 0 < index < len(y)-1:
        den = y[index-1] - 2*y[index] + y[index+1]
        if den != 0:
            frac = .5*(y[index-1]-y[index+1])/den
    lag = correlation_lags(len(rec), len(signal), mode='full')[index] + frac
    return float(lag)


def align_capture(rec, playback, slots, cfg):
    """Estimate offset + clock ratio using two markers from the SAME fixed driver."""
    first, last = slots[0], slots[-1]
    # Restrict each correlation to its scheduled marker window. Driver sweeps can
    # otherwise match the same chirp (especially when their duration is one second).
    a = _arrival(rec[:slots[1]['start']], first['signal'])
    begin = last['start'] - int(.2*cfg['fs'])
    b = begin + _arrival(rec[begin:], last['signal'])
    ratio = (b-a)/(last['start']-first['start'])
    ppm = (ratio-1)*1e6
    if not np.isfinite(ppm) or abs(ppm) > cfg['max_clock_ppm']:
        raise ValueError(f'Unreliable timing markers: {ppm:.1f} ppm. Check capture and background noise.')
    offset = a-ratio*first['start']
    indices = offset + ratio*np.arange(len(playback))
    if indices[-1] >= len(rec):
        raise ValueError('Capture ended before the final timing marker tail.')
    return np.interp(indices, np.arange(len(rec)), rec, left=0, right=0), float(ppm)


def transfer(recording, stimulus, fs, freq, gate_ms=None, pre=0):
    """Regularized deconvolution; time origin stays at the scheduled DAC onset."""
    n = 1 << int(np.ceil(np.log2(len(recording)+len(stimulus))))
    x = np.fft.rfft(stimulus, n)
    y = np.fft.rfft(recording, n)
    power = abs(x)**2
    spec = y*np.conj(x)/(power + max(power.max()*1e-7, 1e-20))
    ir = np.fft.irfft(spec, n)
    if gate_ms is not None:
        # Window around arrival but do not shift it: relative delay remains in the FFT.
        peak = int(np.argmax(abs(ir[:min(n, int(.1*fs)+pre)])))
        end = min(n, peak + int(gate_ms*.001*fs))
        width = max(2, (end-peak)//2)
        ir[end-width:end] *= np.linspace(1, 0, width)
        ir[end:] = 0
        spec = np.fft.rfft(ir)
    f = np.fft.rfftfreq(n, 1/fs)
    h = np.interp(freq, f, spec.real) + 1j*np.interp(freq, f, spec.imag)
    return h*np.exp(2j*np.pi*freq*pre/fs), ir


def extract(rec, playback, slots, cfg, cal=None):
    aligned, ppm = align_capture(rec, playback, slots, cfg)
    f = np.geomspace(15, min(21000, cfg['fs']*.47), 720)
    responses, impulses = {}, {}
    pre = int(.04*cfg['fs'])
    for slot in slots[1:-1]:
        d = next(d for d in cfg['drivers'] if d['id'] == slot['id'])
        start, n = slot['start'], slot['length']
        part = aligned[start-pre:start+n+int(cfg['gap_seconds']*cfg['fs'])]
        h, ir = transfer(part, slot['signal'], cfg['fs'], f, pre=pre)
        gated, _ = transfer(part, slot['signal'], cfg['fs'], f, cfg['gate_ms'], pre=pre)
        # Keep ungated LF where the gate cannot resolve the response.
        blend = np.clip((f-1000/cfg['gate_ms'])/(1000/cfg['gate_ms']), 0, 1)
        flat_mag = abs(h)*(1-blend)+abs(gated)*blend
        corr = 10**(-cal.at(f)/20) if cal is not None else 1
        band = (f >= d['min_hz']*1.15) & (f <= d['max_hz']/1.15)
        responses[d['id']] = dict(h=h*corr, flatten=flat_mag*corr, valid=band)
        impulses[d['id']] = ir[:int(.5*cfg['fs'])]
    return f, responses, impulses, ppm


def flatten(cfg, f, responses):
    out = {}
    for d in active_drivers(cfg):
        r = responses[d['id']]
        lo, hi = d['min_hz']*1.2, d['max_hz']/1.2
        if lo >= hi:
            raise DriverError(d['id'],f"Usable band too narrow for {d['id']}.")
        try:
            filters, _ = peq.fit_peq(f, 20*np.log10(np.maximum(r['flatten'], 1e-12)), np.zeros(len(f)),
                                    cfg['fs'], cfg['peq_bands'], lo, hi, cfg['max_boost_db'],
                                    cfg['max_cut_db'], 2, ref_band=(lo, hi), smooth=3)
        except (ValueError,RuntimeError) as error:
            raise DriverError(d['id'],f'Driver EQ could not be fitted: {error}') from error
        out[d['id']] = [asdict(p) for p in filters]
    return out


def signal_report(rec, cfg, levels, responses):
    """The same conservative signal check used for acceptance and repeat levels."""
    noise = max(float(np.sqrt(np.mean(rec[:int(.15*cfg['fs'])]**2))), 1e-12)
    report = {}
    for d in active_drivers(cfg):
        response = responses[d['id']]
        signal = float(np.median(abs(response['h'][response['valid']]))) * 10**(levels[d['id']]/20)
        report[d['id']] = dict(output_db=levels[d['id']], noise_db=float(20*np.log10(noise)),
                               margin_db=float(20*np.log10(max(signal, 1e-12)/noise)))
    return report


def branch_sos(cfg, d, candidate, eq):
    fs = cfg['fs']
    sos = [protection(d, fs)]
    low, high, split = candidate['low'], candidate['high'], candidate['split']
    order, role = candidate['order'], d['role']
    if candidate['strategy'] == 'protected':
        pass  # No usable panel on this side: retain each driver's own protected band.
    elif role == 'sub':
        sos.append(lr(low, 'lowpass', order, fs))
    elif role == 'tweeter':
        sos.append(lr(high, 'highpass', order, fs))
    else:
        if 'sub' in active_roles(cfg,d['side']):
            sos.append(lr(low, 'highpass', order, fs))
        if 'tweeter' in active_roles(cfg,d['side']):
            sos.append(lr(high, 'lowpass', order, fs))
        if candidate['strategy'] == 'split':
            sos.append(lr(split, 'lowpass' if role == 'a' else 'highpass', order, fs))
    if eq:
        sos.append(peq.sos_for([peq.Peq(**p) for p in eq], fs))
    return np.vstack(sos)


def branch_response(cfg, d, c, eq, f):
    _, h = sosfreqz(branch_sos(cfg, d, c, eq), worN=f, fs=cfg['fs'])
    return h * 10**(c['gains'][d['role']]/20) * c['polarity'][d['role']] * delay_response(f,c['delays'][d['role']],cfg['fs'])


def delay_response(f, milliseconds, fs):
    samples = milliseconds*fs/1000
    whole = int(np.floor(samples))
    fraction = samples-whole
    z = np.exp(-2j*np.pi*f/fs)
    return z**whole*((1-fraction)+fraction*z)


def fast_lr(f, fc, kind, order, fs):
    """Bilinear Butterworth prototype, squared; identical to the exported LR SOS."""
    n = order//2
    poles = np.exp(1j*np.pi*(2*np.arange(n)+1+n)/(2*n))
    s = 1j*np.tan(np.pi*f/fs)/np.tan(np.pi*fc/fs)
    if kind == 'highpass':
        s = 1/s
    return (1/np.prod(s[:,None]-poles[None,:],axis=1))**2


def search(cfg, f, responses, eq, side, cancel=None):
    drivers = [d for d in active_drivers(cfg) if d['side'] == side]
    byrole = {d['role']: d for d in drivers}
    panels = [d for d in drivers if d['role'] in ('a', 'b')]
    if not drivers:
        raise ValueError(f'{side}: no usable drivers remain.')
    if not panels:
        roles = list(byrole)
        return [dict(strategy='protected',order=4,low=min(d['min_hz'] for d in drivers),
                     high=max(d['max_hz'] for d in drivers),split=1000.,active_pairs=roles,
                     gains=dict.fromkeys(roles,0.),delays=dict.fromkeys(roles,0.),
                     polarity=dict.fromkeys(roles,1),score=0.)]
    low_bounds = ((max(150, *[d['min_hz'] for d in panels]),
                   min(cfg.get('sub_crossover_max', 500), byrole['sub']['max_hz']/1.2))
                  if 'sub' in byrole else None)
    high_bounds = ((max(TWEETER_MIN_HZ, byrole['tweeter']['min_hz']),
                    min(5000, *[d['max_hz'] for d in panels]))
                   if 'tweeter' in byrole else None)
    if any(b is not None and b[0] >= b[1] for b in (low_bounds, high_bounds)):
        raise ValueError('Driver limits leave no crossover search overlap.')
    lo = low_bounds[0]*.65 if low_bounds else min(d['min_hz'] for d in panels)*1.2
    hi = high_bounds[1]*1.4 if high_bounds else max(d['max_hz'] for d in panels)/1.2
    mask = (f >= lo) & (f <= hi)
    ff = f[mask][::3]
    hh = {d['id']: responses[d['id']]['h'][mask][::3] for d in drivers}
    fixed = {}
    for d in drivers:
        sections = protection(d,cfg['fs'])
        if eq[d['id']]:
            sections = np.vstack([sections,peq.sos_for([peq.Peq(**p) for p in eq[d['id']]],cfg['fs'])])
        fixed[d['role']] = hh[d['id']]*sosfreqz(sections,worN=ff,fs=cfg['fs'])[1]
    candidates = []
    roles = list(byrole)
    count = len(roles)-1
    strategies = ('both', 'split') if 'a' in byrole and 'b' in byrole else ('single',)
    for strategy, order in itertools.product(strategies, (4, 8) if len(roles)>1 else (4,)):
        geometry = {key: bounds for key, bounds in (('low', low_bounds), ('high', high_bounds),
                    ('split', (500, 1500) if strategy == 'split' else None)) if bounds is not None}
        bounds = [*geometry.values(), *[(-4, 4)]*count, *[(-24, 24)]*count]
        initial = [*[np.mean(b) for b in geometry.values()], *[0.]*count, *[-3.]*count]
        offset = len(geometry)
        for signs in itertools.product((-1, 1), repeat=count):
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Tuning stopped.')
            def unpack(x):
                delays = np.array([0, *x[offset:offset+count]])
                delays -= delays.min()
                gains = np.array([0., *x[offset+count:]])
                gains -= gains.max()  # Relative matching uses attenuation only, including the sub.
                frequencies = dict(low=min(d['min_hz'] for d in panels), high=max(d['max_hz'] for d in panels), split=1000.)
                frequencies.update(zip(geometry, map(float, x[:offset])))
                return dict(strategy=strategy, order=order, **frequencies, active_pairs=roles,
                            delays=dict(zip(roles, delays.tolist())), gains=dict(zip(roles, gains.tolist())),
                            polarity=dict(zip(roles, [1, *signs])))
            def cost(x):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('Tuning stopped.')
                c = unpack(x)
                panel = np.ones(len(ff), dtype=complex)
                cross = {}
                if 'sub' in byrole:
                    cross['sub'] = fast_lr(ff,c['low'],'lowpass',order,cfg['fs'])
                    panel *= fast_lr(ff,c['low'],'highpass',order,cfg['fs'])
                if 'tweeter' in byrole:
                    cross['tweeter'] = fast_lr(ff,c['high'],'highpass',order,cfg['fs'])
                    panel *= fast_lr(ff,c['high'],'lowpass',order,cfg['fs'])
                cross.update({r:panel for r in roles if r in ('a','b')})
                if strategy == 'split':
                    cross['a'] = cross['a']*fast_lr(ff,c['split'],'lowpass',order,cfg['fs'])
                    cross['b'] = cross['b']*fast_lr(ff,c['split'],'highpass',order,cfg['fs'])
                branches = [fixed[r]*cross[r]*10**(c['gains'][r]/20)*c['polarity'][r]*delay_response(ff,c['delays'][r],cfg['fs'])
                            for r in roles]
                summed = np.sum(branches, axis=0)
                db = 20*np.log10(np.maximum(abs(summed), 1e-10))
                smoothness = np.mean((db-np.median(db))**2)
                cancellation = np.maximum(20*np.log10(np.maximum(np.sum(np.abs(branches), axis=0), 1e-10)) - db - 4, 0)
                return float(smoothness + .4*np.mean(cancellation**2) + .015*np.sum(x[offset+count:]**2) + .02*np.sum(x[offset:offset+count]**2))
            x = np.array(initial)
            if bounds:
                x = minimize(cost, x, method='Powell', bounds=bounds, options=dict(maxiter=12, maxfev=700, xtol=.03)).x
            candidate = unpack(x)
            candidate['score'] = cost(x)
            candidates.append(candidate)
    return sorted(candidates, key=lambda c: c['score'])[:3]


def apply_stereo(signal, setting, fs):
    if not setting:
        return signal
    delay = setting.get('delay_ms',0)*fs/1000
    indices = np.arange(len(signal))
    gain = 10**(setting.get('gain_db',0)/20)*setting.get('polarity',1)
    if signal.ndim==1:
        return np.interp(indices-delay,indices,signal,left=0,right=0)*gain
    return np.column_stack([np.interp(indices-delay,indices,channel,left=0,right=0)*gain for channel in signal.T])


def render_safety(cfg, side, stimulus):
    """Untuned baseline with each driver's mandatory band protection retained."""
    out = np.zeros((len(stimulus)+int(.1*cfg['fs']),8))
    for d in active_drivers(cfg):
        if d['side']==side:
            out[:,d['output']-1] = sosfilt(protection(d,cfg['fs']),np.pad(stimulus,(0,int(.1*cfg['fs']))))
    scale = min([1., *[10**(measurement_ceiling(cfg,d)/20)/max(np.max(abs(out[:,d['output']-1])),1e-12)
                      for d in active_drivers(cfg) if d['side']==side]])
    return out*scale,scale


def render_chain(cfg, side, candidate, eq, stimulus, room=(), stereo=None):
    fs = cfg['fs']
    x = sosfilt(peq.sos_for([peq.Peq(**p) for p in room], fs), stimulus) if room else stimulus
    out = np.zeros((len(x)+int(.1*fs), 8))
    for d in active_drivers(cfg):
        if d['side'] != side:
            continue
        y = sosfilt(branch_sos(cfg, d, candidate, eq[d['id']]), np.pad(x, (0, int(.1*fs))))
        delay = candidate['delays'][d['role']]*fs/1000
        y = np.interp(np.arange(len(y))-delay, np.arange(len(y)), y, left=0, right=0)
        out[:,d['output']-1] = y*10**(candidate['gains'][d['role']]/20)*candidate['polarity'][d['role']]
    scale = min([1., *[10**(measurement_ceiling(cfg,d)/20)/max(np.max(abs(out[:,d['output']-1])), 1e-12)
                      for d in active_drivers(cfg) if d['side'] == side]])
    return apply_stereo(out*scale,(stereo or {}).get(side),fs), scale


def decay_weights(impulses, fs, freq):
    """LF late-energy fraction: prioritize persistent modes, not moving nulls."""
    centres = np.geomspace(30,250,12)
    late = []
    for fc in centres:
        ratios = []
        sos = butter(2,[fc/2**(1/6),fc*2**(1/6)],btype='bandpass',fs=fs,output='sos')
        for ir in impulses:
            band = sosfilt(sos,ir)
            arrival = int(np.argmax(abs(band)))
            energy = band[arrival:]**2
            cutoff = int(.08*fs)
            ratios.append(float(np.sum(energy[cutoff:])/max(np.sum(energy),1e-24)))
        late.append(float(np.mean(ratios)))
    weights = 1+np.interp(freq,centres,late,left=0,right=0)
    return weights,dict(freq_hz=centres.tolist(),late_energy_fraction=late,late_after_ms=80)
