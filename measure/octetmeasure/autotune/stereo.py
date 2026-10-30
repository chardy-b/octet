"""Shared-reference stereo measurements, repeat averaging, and polarity checks."""
import numpy as np
from scipy.signal import butter, correlate, correlation_lags, sosfilt

from .. import peq
from . import dsp, config


def mean_ir(rows):
    length = max(map(len, rows))
    return np.mean([np.pad(row, (0, length-len(row))) for row in rows], axis=0)


def average_drivers(rows):
    """Every repeat was aligned by its common acoustic markers, never per driver."""
    f = rows[0][0]
    responses, impulses = {}, {}
    for key, first in rows[0][1].items():
        responses[key] = dict(h=np.mean([r[1][key]['h'] for r in rows],axis=0),
            flatten=np.sqrt(np.mean([r[1][key]['flatten']**2 for r in rows],axis=0)),valid=first['valid'])
        impulses[key] = mean_ir([r[2][key] for r in rows])
    return f, responses, impulses, float(np.mean([r[3] for r in rows]))


def sequence(cfg, render, levels, loopback=False):
    """L marker, L, R, mono L+R, L marker in one continuous DAC/capture buffer."""
    fs, gap = cfg['fs'], int(cfg['gap_seconds']*cfg['fs'])
    drivers = config.active_drivers(cfg)
    lo, hi = min(d['min_hz'] for d in drivers), max(d['max_hz'] for d in drivers)
    def stimulus(seconds, low=lo, high=hi):
        from ..sweep import make_sweep
        return np.pad(make_sweep(fs,seconds,low,high,-3).signal,(0,int(.15*fs)))
    # A midrange pilot avoids low-frequency ringing and crossover cancellation
    # moving the correlation peak between otherwise identical timing markers.
    marker, sweep = stimulus(1,max(lo,500),min(hi,2000)), stimulus(cfg['duration'])
    pilot = render('L',marker)
    left, right = render('L',sweep), render('R',sweep)
    common = min(1., *[10**(levels[d['id']]/20)/max(float(np.max(abs(buffer[:,d['output']-1]))),1e-12)
                        for buffer in (pilot,left,right,left+right) for d in drivers])
    if loopback:
        def source(signal, side):
            out = np.zeros((len(signal)+int(.1*fs),2))
            out[:len(signal),side] = signal
            return out
        pilot, left, right = source(marker,0), source(sweep,0), source(sweep,1)
    channels = 2 if loopback else 8
    chunks, slots, cursor = [np.zeros((gap,channels))], [], gap
    for key, output, reference in [('pilot_start',pilot,marker),('L',left,sweep),('R',right,sweep),
                                    ('mono',left+right,sweep),('pilot_end',pilot,marker)]:
        block = np.zeros((len(output)+gap,channels))
        block[:len(output)] = output*common
        chunks.append(block)
        slots.append(dict(id=key,start=cursor,length=len(output),signal=reference*common))
        cursor += len(block)
    return np.concatenate(chunks), slots


def extract(rec, playback, slots, cfg, cal=None):
    aligned, ppm = dsp.align_capture(rec,playback,slots,cfg)
    drivers = config.active_drivers(cfg)
    f = np.geomspace(min(d['min_hz'] for d in drivers)*1.2,max(d['max_hz'] for d in drivers)/1.2,480)
    pre, responses, impulses = int(.04*cfg['fs']), {}, {}
    for slot in slots[1:-1]:
        start, n = slot['start'], slot['length']
        part = aligned[start-pre:start+n+int(cfg['gap_seconds']*cfg['fs'])]
        h, ir = dsp.transfer(part,slot['signal'],cfg['fs'],f,pre=pre)
        responses[slot['id']] = h*(10**(-cal.at(f)/20) if cal is not None else 1)
        impulses[slot['id']] = ir[:int(.5*cfg['fs'])]
    return f,responses,impulses,ppm


def polarity_timing(left, right, fs, lo=300, hi=3000, max_ms=20):
    """Gate direct energy without moving either IR's common time origin."""
    filt = butter(3,[lo,hi],btype='bandpass',fs=fs,output='sos')
    gated = []
    for ir in (left,right):
        band = sosfilt(filt,ir)
        peak = int(np.argmax(abs(band[:min(len(band),int(.09*fs))])))
        start, end = max(0,peak-int(.0015*fs)), min(len(band),peak+int(.006*fs))
        out = np.zeros_like(band)
        out[start:end] = band[start:end]
        width = min(int(.001*fs),(end-start)//2)
        if width:
            out[start:start+width] *= np.linspace(0,1,width)
            out[end-width:end] *= np.linspace(1,0,width)
        gated.append(out)
    correlation = correlate(gated[1],gated[0],mode='full',method='fft')
    lags = correlation_lags(len(gated[1]),len(gated[0]))
    valid = abs(lags)<=max_ms*fs/1000
    correlation, lags = correlation[valid], lags[valid]
    norm = max(float(np.linalg.norm(gated[0])*np.linalg.norm(gated[1])),1e-24)
    correlation /= norm
    index = int(np.argmax(abs(correlation)))
    fraction = 0.
    if 0<index<len(correlation)-1:
        y = abs(correlation)
        denominator = y[index-1]-2*y[index]+y[index+1]
        if denominator:
            fraction = .5*(y[index-1]-y[index+1])/denominator
    value = float(correlation[index])
    positive, negative = max(float(np.max(correlation)),1e-12), max(float(-np.min(correlation)),1e-12)
    return dict(right_minus_left_ms=float((lags[index]+fraction)/fs*1000),
        correlation=value,relative_polarity=1 if value>=0 else -1,
        polarity_peak_ratio=max(positive,negative)/min(positive,negative),
        polarity_confident=abs(value)>=.6 and max(positive,negative)/min(positive,negative)>=1.05)


def driver_polarities(cfg, impulses):
    report = {}
    for role in config.selected_pairs(cfg):
        drivers = [d for d in config.active_drivers(cfg) if d['role']==role]
        if len(drivers) != 2:
            report[role] = dict(interpretation='unavailable',reason='Both matching drivers were not measurable.')
            continue
        lo = max(40, min(d['min_hz'] for d in drivers)*1.5)
        hi = min(12000, max(d['max_hz'] for d in drivers)/1.3)
        row = polarity_timing(impulses[f'L_{role}'],impulses[f'R_{role}'],cfg['fs'],lo,hi)
        row['interpretation'] = ('opposite' if row['relative_polarity']<0 else 'same') if row['polarity_confident'] else 'uncertain'
        report[role] = row
    return report


def metrics(cfg,f,h,ir):
    band = (f>=300)&(f<=3000)
    if band.sum()<8:
        raise ValueError('Stereo alignment needs a usable 300–3000 Hz measurement band.')
    result = polarity_timing(ir['L'],ir['R'],cfg['fs'],max_ms=cfg.get('stereo_max_delay_ms',20))
    left, right, mono = h['L'],h['R'],h['mono']
    result['right_minus_left_db'] = float(np.median(20*np.log10(np.maximum(abs(right[band]),1e-12)/np.maximum(abs(left[band]),1e-12))))
    predicted = peq.smooth_log(f,20*np.log10(np.maximum(abs(left+right),1e-12)),6)
    measured = peq.smooth_log(f,20*np.log10(np.maximum(abs(mono),1e-12)),6)
    result['mono_prediction_error_db'] = float(np.sqrt(np.mean((measured[band]-predicted[band])**2)))
    result['mono_summing_loss_db'] = float(np.median(20*np.log10(np.maximum(abs(left[band])+abs(right[band]),1e-12)/np.maximum(abs(mono[band]),1e-12))))
    return result


def correction(cfg, report):
    if not report['polarity_confident']:
        raise ValueError('Stereo polarity is ambiguous; check microphone position, reflections and matching crossover phase before applying.')
    delay, gain = report['right_minus_left_ms'], report['right_minus_left_db']
    if abs(delay)>=cfg.get('stereo_max_delay_ms',20)*.95:
        raise ValueError('Stereo arrival-time difference exceeds the alignment range; check placement and routing.')
    if abs(gain)>12:
        raise ValueError('Stereo level difference exceeds 12 dB; check routing and amplifier settings.')
    return dict(L=dict(delay_ms=max(0.,delay),gain_db=min(0.,gain),polarity=1),
                R=dict(delay_ms=max(0.,-delay),gain_db=min(0.,-gain),polarity=report['relative_polarity']))


def verify(cfg,report):
    if (report['correlation']<.6 or not report['polarity_confident'] or
            abs(report['right_minus_left_ms'])>cfg.get('stereo_delay_tolerance_ms',.15) or
            abs(report['right_minus_left_db'])>cfg.get('stereo_level_tolerance_db',1) or
            report['mono_prediction_error_db']>cfg['verification_db'] or report['mono_summing_loss_db']>3):
        raise ValueError('Stereo alignment/polarity verification failed; review arrival times, level balance, crossover phase and mono summation.')
    return dict(report,passed=True,polarity_checked=True)
