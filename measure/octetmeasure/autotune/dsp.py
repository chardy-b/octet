"""Pure signal generation, shared-clock extraction and crossover search."""
from dataclasses import asdict
import itertools

import numpy as np
from scipy.optimize import minimize
from scipy.signal import butter, correlate, correlation_lags, sosfilt, sosfreqz

from .. import peq
from ..sweep import make_sweep


def lr(freq, kind, order, fs):
    half = butter(order // 2, freq, btype=kind, fs=fs, output='sos')
    return np.vstack([half, half])


def protection(d, fs):
    return np.vstack([lr(d['min_hz'], 'highpass', d['order'], fs),
                      lr(d['max_hz'], 'lowpass', d['order'], fs)])


def protected_sweep(d, cfg, level, duration=None):
    if not d['start_db'] <= level <= d['max_db']:
        raise ValueError('Requested level exceeds driver ramp limits.')
    sw = make_sweep(cfg['fs'], duration or cfg['duration'], d['min_hz'], d['max_hz'], 0)
    x = sosfilt(protection(d, cfg['fs']), np.pad(sw.signal, (0, int(.15 * cfg['fs']))))
    x *= 10**(level/20) / max(1, np.max(np.abs(x)))
    return x


def sequence(cfg, levels):
    """One DAC buffer; identical protected acoustic markers bracket the eight sweeps."""
    fs, gap = cfg['fs'], int(cfg['gap_seconds'] * cfg['fs'])
    pilot_driver = next(d for d in cfg['drivers'] if d['id'] == 'L_a')
    pilot = protected_sweep(pilot_driver, cfg, levels['L_a'], duration=1)
    chunks, slots, cursor = [np.zeros((gap, 8))], [], gap
    items = [(pilot_driver, pilot, 'pilot_start')]
    items += [(d, protected_sweep(d, cfg, levels[d['id']]), d['id']) for d in cfg['drivers']]
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
    for d in cfg['drivers']:
        r = responses[d['id']]
        lo, hi = d['min_hz']*1.2, d['max_hz']/1.2
        if lo >= hi:
            raise ValueError(f"Usable band too narrow for {d['id']}.")
        filters, _ = peq.fit_peq(f, 20*np.log10(np.maximum(r['flatten'], 1e-12)), np.zeros(len(f)),
                                cfg['fs'], cfg['peq_bands'], lo, hi, cfg['max_boost_db'],
                                cfg['max_cut_db'], 2, ref_band=(lo, hi), smooth=3)
        out[d['id']] = [asdict(p) for p in filters]
    return out


def branch_sos(cfg, d, candidate, eq):
    fs = cfg['fs']
    sos = [protection(d, fs)]
    low, high, split = candidate['low'], candidate['high'], candidate['split']
    order, role = candidate['order'], d['role']
    if role == 'sub':
        sos.append(lr(low, 'lowpass', order, fs))
    elif role == 'tweeter':
        sos.append(lr(high, 'highpass', order, fs))
    else:
        sos.extend([lr(low, 'highpass', order, fs), lr(high, 'lowpass', order, fs)])
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
    drivers = [d for d in cfg['drivers'] if d['side'] == side]
    byrole = {d['role']: d for d in drivers}
    low_bounds = (max(150, byrole['a']['min_hz'], byrole['b']['min_hz']), min(300, byrole['sub']['max_hz']))
    high_bounds = (max(2500, byrole['tweeter']['min_hz']), min(4000, byrole['a']['max_hz'], byrole['b']['max_hz']))
    if low_bounds[0] >= low_bounds[1] or high_bounds[0] >= high_bounds[1]:
        raise ValueError('Driver limits leave no crossover search overlap.')
    mask = (f >= low_bounds[0]*.65) & (f <= high_bounds[1]*1.4)
    ff = f[mask][::3]
    hh = {d['id']: responses[d['id']]['h'][mask][::3] for d in drivers}
    fixed = {}
    for d in drivers:
        sections = protection(d,cfg['fs'])
        if eq[d['id']]:
            sections = np.vstack([sections,peq.sos_for([peq.Peq(**p) for p in eq[d['id']]],cfg['fs'])])
        fixed[d['role']] = hh[d['id']]*sosfreqz(sections,worN=ff,fs=cfg['fs'])[1]
    candidates = []
    for strategy, order in itertools.product(('both', 'split'), (4, 8)):
        for signs in itertools.product((-1, 1), repeat=3):
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Tuning stopped.')
            def unpack(x):
                delays = np.array([0, *x[3:6]])
                delays -= delays.min()
                return dict(strategy=strategy, order=order, low=float(x[0]), high=float(x[1]), split=float(x[2]),
                            delays=dict(zip(('sub','a','b','tweeter'), delays.tolist())),
                            gains=dict(zip(('sub','a','b','tweeter'), [0., *map(float, x[6:9])])),
                            polarity=dict(zip(('sub','a','b','tweeter'), [1, *signs])))
            def cost(x):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError('Tuning stopped.')
                c = unpack(x)
                low_lp = fast_lr(ff,x[0],'lowpass',order,cfg['fs'])
                low_hp = fast_lr(ff,x[0],'highpass',order,cfg['fs'])
                high_lp = fast_lr(ff,x[1],'lowpass',order,cfg['fs'])
                high_hp = fast_lr(ff,x[1],'highpass',order,cfg['fs'])
                cross = dict(sub=low_lp,a=low_hp*high_lp,b=low_hp*high_lp,tweeter=high_hp)
                if strategy == 'split':
                    cross['a'] = cross['a']*fast_lr(ff,x[2],'lowpass',order,cfg['fs'])
                    cross['b'] = cross['b']*fast_lr(ff,x[2],'highpass',order,cfg['fs'])
                branches = [fixed[r]*cross[r]*10**(c['gains'][r]/20)*c['polarity'][r]*delay_response(ff,c['delays'][r],cfg['fs'])
                            for r in ('sub','a','b','tweeter')]
                summed = np.sum(branches, axis=0)
                db = 20*np.log10(np.maximum(abs(summed), 1e-10))
                smoothness = np.mean((db-np.median(db))**2)
                cancellation = np.maximum(20*np.log10(np.maximum(np.sum(np.abs(branches), axis=0), 1e-10)) - db - 4, 0)
                return float(smoothness + .4*np.mean(cancellation**2) + .015*np.sum(x[6:9]**2) + .02*np.sum(x[3:6]**2))
            bounds = [low_bounds, high_bounds, (500,1500), *[(-4,4)]*3, *[(-12,0)]*3]
            x = np.array([np.mean(low_bounds), np.mean(high_bounds), 900, 0,0,0, -3,-3,-3.])
            result = minimize(cost, x, method='Powell', bounds=bounds, options=dict(maxiter=12, maxfev=700, xtol=.03))
            candidate = unpack(result.x)
            candidate['score'] = cost(result.x)
            candidates.append(candidate)
    return sorted(candidates, key=lambda c: c['score'])[:3]


def render_chain(cfg, side, candidate, eq, stimulus, room=()):
    fs = cfg['fs']
    x = sosfilt(peq.sos_for([peq.Peq(**p) for p in room], fs), stimulus) if room else stimulus
    out = np.zeros((len(x)+int(.1*fs), 8))
    for d in cfg['drivers']:
        if d['side'] != side:
            continue
        y = sosfilt(branch_sos(cfg, d, candidate, eq[d['id']]), np.pad(x, (0, int(.1*fs))))
        delay = candidate['delays'][d['role']]*fs/1000
        y = np.interp(np.arange(len(y))-delay, np.arange(len(y)), y, left=0, right=0)
        out[:,d['output']-1] = y*10**(candidate['gains'][d['role']]/20)*candidate['polarity'][d['role']]
    scale = min(1., *[10**(d['max_db']/20)/max(np.max(abs(out[:,d['output']-1])), 1e-12)
                      for d in cfg['drivers'] if d['side'] == side])
    return out*scale, scale


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
