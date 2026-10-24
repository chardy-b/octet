"""Durable Auto Tune stages shared by CLI and web clients."""
from dataclasses import asdict
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import uuid

import numpy as np
import yaml

from .. import peq
from ..cal import load_cal
from ..sweep import make_sweep
from . import config, dsp
from .backend import Hardware, Simulated, exclusive
from .camilla import Deployment, atomic, export


class Runner:
    def __init__(self, root, demo=False, emit=None, backend_factory=None):
        self.root = Path(root)
        self.demo = demo
        self.emit = emit or (lambda event: None)
        self.backend_factory = backend_factory
        self.cancel = threading.Event()
        self.backend = None
        self.thread = None
        self.lock = threading.RLock()
        self.state = self._load()
        if self.state.get('running'):
            try:
                with exclusive(self.root/'audio.lock'):
                    self.state.update(running=False,status='interrupted',message='Service restarted. Resume the saved stage with the microphone in its prior position.')
                    if self.state.get('stage') == 'apply' and not self.demo:
                        try:
                            Hardware(self.state['cfg']).systemctl('stop')
                            self.state['message'] = 'Activation was interrupted. Playback stopped; use Rollback before applying again.'
                        except Exception as e:
                            self.state['message'] = f'Activation interrupted; could not stop playback: {e}. Stop CamillaDSP on the Pi.'
                    self.save()
            except RuntimeError:
                pass  # Another CLI/server process still owns this run.

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def _load(self):
        path = self.root/'current.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(status='idle',running=False)

    def save(self):
        atomic(self.root/'current.json',json.dumps(self.state,indent=2,allow_nan=False))
        if self.state.get('id'):
            atomic(self.directory/'run.json',json.dumps(self.state,indent=2,allow_nan=False))
        self.emit(dict(type='autotune',**self.state))

    @property
    def directory(self):
        return self.root/self.state['id']

    def status(self):
        with self.lock:
            if not self.running:
                self.state = self._load()
            return json.loads(json.dumps(self.state))

    def profile(self):
        path = self.root/'speaker.yaml'
        return path.read_text(encoding='utf-8') if path.exists() else yaml.safe_dump(config.example(),sort_keys=False)

    def set_profile(self, text):
        cfg = config.validate(config.parse(text))
        atomic(self.root/'speaker.yaml',yaml.safe_dump(cfg,sort_keys=False))
        return cfg

    def start(self, action='start', profile=None):
        with self.lock:
            if self.running:
                raise ValueError('Auto Tune is already running.')
            lease = ExitStack()
            try:
                lease.enter_context(exclusive(self.root/'audio.lock'))
                self.state = self._load()
                self._start(action, profile, lease)
            except BaseException:
                lease.close()
                raise

    def _start(self, action, profile, lease):
        self.lease = lease
        if action == 'start':
            cfg = config.validate(profile or config.parse(self.profile()),hardware=not self.demo)
            self.state = dict(id=uuid.uuid4().hex,cfg=cfg,demo=self.demo,status='starting',stage='levels',
                              running=True,audio_owned=True,levels={},room_count=0,history=[])
        elif not self.state.get('id'):
            raise ValueError('Start an Auto Tune run first.')
        elif bool(self.state['demo']) != self.demo:
            raise ValueError('This saved run belongs to a different demo/hardware mode.')
        elif action == 'continue':
            transition = {'await_levels':'measure','await_room':'room','await_reference':'finish'}
            if self.state['status'] not in transition:
                raise ValueError('This run is not waiting for a microphone or level adjustment.')
            self.state['stage'] = transition[self.state['status']]
        elif action == 'resume':
            if self.state['status'] not in ('failed','cancelled','interrupted'):
                raise ValueError('Only interrupted, failed or cancelled stages can be resumed.')
            if self.state['stage'] == 'apply':
                raise ValueError('An interrupted deployment requires rollback before retrying apply.')
        elif action == 'levels':
            if self.state['status'] != 'await_levels':
                raise ValueError('Level adjustment is only available before driver measurement.')
            self.state['stage'] = 'levels'
        elif action == 'apply':
            if self.state['status'] != 'ready':
                raise ValueError('Complete tuning and verification before applying.')
            self.state['stage'] = 'apply'
        elif action == 'rollback':
            self.state['stage'] = 'rollback'
        else:
            raise ValueError('Unknown Auto Tune action.')
        self.cancel = threading.Event()
        self.state.update(running=True,audio_owned=True,status='running',message='Starting '+self.state['stage'])
        self.save()
        self.thread = threading.Thread(target=self._work,daemon=True,name='octet-auto')
        self.thread.start()

    def mute(self):
        self.cancel.set()
        if self.backend:
            self.backend.stop()
        elif not self.demo and self.state.get('cfg'):
            self.backend = Hardware(self.state['cfg'])
        if not self.demo and self.backend:
            self.backend.systemctl('stop')
        if not self.running and self.state.get('status') == 'applied':
            self.state.update(status='ready',message='Playback stopped. Apply and verify to restart the tuned configuration.')
            self.save()
        return dict(stopping=self.running)

    def _message(self, message):
        self.state['message'] = message
        self.save()

    def _capture(self, playback, **kw):
        if self.cancel.is_set():
            raise InterruptedError('Stopped.')
        return self.backend.capture(playback,self.cancel,lambda peak:self.emit(dict(type='auto_meter',peak_db=peak)),**kw)

    def _work(self):
        try:
            cfg = self.state['cfg']
            self.backend = (self.backend_factory or (Simulated if self.demo else Hardware))(cfg)
            if self.state['stage'] != 'rollback':
                self.backend.take_dac()
            if self.state['stage'] == 'levels' and hasattr(self.backend, 'preflight'):
                self._message('Checking the microphone after releasing the music input')
                try:
                    self.backend.preflight()
                except InterruptedError:
                    raise
                except Exception as error:
                    if self.cancel.is_set():
                        raise InterruptedError('Playback stopped.') from error
                    try:
                        self.backend.restore_preflight()
                    except InterruptedError:
                        raise
                    except Exception as recovery:
                        raise RuntimeError(f'{error} Could not restore the previous audio services: {recovery}. Use Restore before tuning.') from error
                    self.state['audio_owned'] = False
                    raise RuntimeError(f'{error} No tones played; previous audio services and master settings restored.') from error
            while True:
                if self.cancel.is_set():
                    raise InterruptedError('Stopped.')
                stage = self.state['stage']
                self._message(stage.capitalize())
                next_stage = getattr(self,'_'+stage)()
                if next_stage is None:
                    if cfg.get('automatic'):
                        next_stage = {'await_levels': 'measure', 'await_room': 'room',
                                      'await_reference': 'finish'}.get(self.state['status'])
                        if self.state['status'] == 'ready' and cfg.get('auto_apply'):
                            next_stage = 'apply'
                        if next_stage:
                            self.state['status'] = 'running'
                    if next_stage is None:
                        break
                self.state['stage'] = next_stage
                self.save()
        except InterruptedError as e:
            self.state.update(status='cancelled',message=str(e))
        except Exception as e:
            self.state.update(status='failed',message=str(e))
        finally:
            if self.backend and self.state.get('status') in ('failed','cancelled'):
                self.backend.stop()
                if not self.demo and self.state.get('audio_owned', True):
                    try:
                        self.backend.systemctl('stop')
                    except Exception as e:
                        self.state['message'] += f' Could not stop CamillaDSP: {e}'
            self.state['running'] = False
            try:
                self.save()
            finally:
                self.lease.close()

    def _reference_levels(self):
        """Reuse recordings with the same input, routing and protected bands; play nothing."""
        cfg = self.state['cfg']
        for path in sorted(self.root.glob('*/run.json'), key=lambda p:p.stat().st_mtime, reverse=True):
            if path.parent == self.directory:
                continue
            try:
                previous = json.loads(path.read_text(encoding='utf-8'))
                old = previous['cfg']
                if previous.get('demo') != self.demo or any(old[k] != cfg[k] for k in (
                        'fs', 'input_device', 'output_device', 'input_channels', 'mic_channel', 'calibration')):
                    continue
                report = previous.get('signal_report')
                if report is None:
                    raw = path.parent/'drivers-raw.npz'
                    if not raw.exists():
                        continue
                    with np.load(raw, allow_pickle=False) as data:
                        rec = data['capture']
                    playback, slots = dsp.sequence(old, previous['levels'])
                    cal = load_cal(old['calibration']) if old['calibration'] else None
                    _, responses, _, _ = dsp.extract(rec, playback, slots, old, cal)
                    report = dsp.signal_report(rec, old, previous['levels'], responses)
                compatible = {d['id'] for d in config.active_drivers(cfg) if any(
                    all(other.get(k) == d.get(k) for k in ('id', 'output', 'min_hz', 'max_hz', 'order'))
                    for other in config.active_drivers(old))}
                report = {key:value for key,value in report.items() if key in compatible
                          and np.isfinite([value['margin_db'], value['output_db']]).all()
                          and value['margin_db'] >= cfg['min_snr_db']}
                if report:
                    self.state['level_reference'] = previous['id']
                    return report
            except (ValueError, KeyError, OSError):
                continue
        return {}

    def _levels(self):
        cfg, fs = self.state['cfg'], self.state['cfg']['fs']
        summaries = {}
        automatic = cfg.get('automatic', False)
        reference = self._reference_levels() if automatic else {}
        cal = load_cal(cfg['calibration']) if cfg['calibration'] else None
        for d in config.active_drivers(cfg):
            previous = None
            ramp = dict(d)
            prior = reference.get(d['id'])
            if prior:
                # Aim six dB above the acceptance floor, rather than at the ceiling.
                estimate = prior['output_db'] + cfg['min_snr_db'] + 6 - prior['margin_db']
                ramp['start_db'] = float(np.clip(round(estimate/d['step_db'])*d['step_db'],
                                                  d['start_db'], d['max_db']))
            checks = 0
            for level in config.ramp(ramp):
                self._message(f"{d['id']}: {level:g} dBFS; amp group {d.get('amp_group',d['id'])}")
                x = dsp.protected_sweep(d,cfg,level,duration=1)
                signal = np.zeros((len(x)+int(d['step_seconds']*fs),8))
                signal[:len(x),d['output']-1] = x
                rec = self._capture(signal)
                peak = float(20*np.log10(max(np.max(abs(rec)),1e-12)))
                if peak >= cfg['clip_db']:
                    raise ValueError(f"{d['id']}: microphone clipping; reduce input gain.")
                if previous is not None and peak-previous[0] > level-previous[1]+cfg['jump_db']:
                    raise ValueError(f"{d['id']}: unexpected level jump. Check amplifier and microphone settings.")
                previous = peak,level
                noise = max(float(np.sqrt(np.mean(rec[:int(.15*fs)]**2))), 1e-12)
                freq = np.geomspace(d['min_hz']*1.15,d['max_hz']/1.15,120)
                onset = max(0,int(dsp._arrival(rec,x)) - int(.04*fs))
                response,_ = dsp.transfer(rec[onset:],x,fs,freq)
                if cal is not None:
                    response *= 10**(-cal.at(freq)/20)
                sensitivity = float(np.median(20*np.log10(np.maximum(abs(response),1e-12))))
                margin = sensitivity+level-20*np.log10(noise)
                checks += 1
                if automatic and margin >= cfg['min_snr_db']+3:
                    break
            if automatic and margin < cfg['min_snr_db']:
                raise ValueError(f"Insufficient signal for {d['id']}; check its connection or deselect its channel pair before starting a new run.")
            summaries[d['id']] = dict(peak_db=peak,output_db=level,noise_db=float(20*np.log10(max(noise,1e-12))),
                                      sensitivity_db=sensitivity,amp_group=d.get('amp_group',d['id']),
                                      signal_margin_db=float(margin), checks=checks,
                                      reused_recording=bool(prior))
            self.state['levels'][d['id']] = level
        reference = min(v['sensitivity_db'] for k,v in summaries.items() if k.endswith(('_a','_b')))
        groups = {}
        for v in summaries.values():
            v['suggested_reduction_db'] = round(max(0.,v['sensitivity_db']-reference),1)
            groups.setdefault(v['amp_group'],[]).append(v['suggested_reduction_db'])
        self.state['amp_advice'] = {group:round(float(np.median(values)),1) for group,values in groups.items()}
        self.state.update(level_report=summaries,status='await_levels',
                          message='Review driver levels and microphone gain. Adjust shared amp knobs if needed and repeat Levels. Keep the microphone fixed for the driver and crossover measurements; Continue when ready.')
        return None

    def _measure(self):
        cfg = self.state['cfg']
        playback,slots = dsp.sequence(cfg,self.state['levels'])
        rec = self._capture(playback)
        np.savez_compressed(self.directory/'drivers-raw.npz',capture=rec)
        cal = load_cal(cfg['calibration']) if cfg['calibration'] else None
        f,responses,impulses,ppm = dsp.extract(rec,playback,slots,cfg,cal)
        # Estimate noise from the silent lead-in; reject absent or unusable driver responses.
        self.state['signal_report'] = dsp.signal_report(rec, cfg, self.state['levels'], responses)
        for key, reading in self.state['signal_report'].items():
            if reading['margin_db'] < cfg['min_snr_db']:
                raise ValueError(f"Insufficient signal for {key}; check its connection or deselect its channel pair before starting a new run.")
        data = dict(freq=f,**{key:r['h'] for key,r in responses.items()},
                    **{key+'_flatten':r['flatten'] for key,r in responses.items()},
                    **{key+'_ir':ir for key,ir in impulses.items()})
        np.savez_compressed(self.directory/'drivers.npz',**data)
        self.state['clock_ppm'] = ppm
        self.state['eq'] = dsp.flatten(cfg,f,responses)
        return 'tune'

    def _tune(self):
        with np.load(self.directory/'drivers.npz') as data:
            f = data['freq']
            responses = {d['id']:dict(h=data[d['id']]) for d in config.active_drivers(self.state['cfg'])}
            self.state['candidates'] = {}
            for side in ('L','R'):
                self._message(f'Searching {side} crossovers, exciter strategies, delay, polarity and attenuation')
                self.state['candidates'][side] = dsp.search(self.state['cfg'],f,responses,self.state['eq'],side,self.cancel)
        return 'verify'

    def _sum(self, side, candidate, room=(), loopback=False):
        cfg, fs = self.state['cfg'], self.state['cfg']['fs']
        lo = min(d['min_hz'] for d in config.active_drivers(cfg))
        hi = max(d['max_hz'] for d in config.active_drivers(cfg))
        # Exported attenuation already limits full-scale music. Avoid applying a
        # second -30 dB test attenuation, which would bury real verification in noise.
        sw = make_sweep(fs,cfg['duration'],lo,hi,-3 if 'preamp' in self.state else -30)
        stimulus = np.pad(sw.signal,(0,int(cfg['gap_seconds']*fs)))
        out,scale = dsp.render_chain(cfg,side,candidate,self.state['eq'],stimulus,room)
        # Use the exact exported gain for both direct and active-path checks.
        if 'preamp' in self.state:
            out *= self.state['preamp'][side]/scale
            scale = self.state['preamp'][side]
        quiet = 1.
        if cfg.get('automatic'):
            quiet = min(1., *[10**(self.state['levels'][d['id']]/20)/max(
                float(np.max(abs(out[:,d['output']-1]))), 1e-12)
                for d in config.active_drivers(cfg) if d['side'] == side])
        out *= quiet
        if loopback:
            signal = np.zeros((len(stimulus),2))
            signal[:,0 if side=='L' else 1] = stimulus*quiet
            rec = self._capture(signal,loopback=True)
        else:
            rec = self._capture(out)
        scale *= quiet
        peak = float(np.max(abs(rec)))
        if peak < 1e-6 or peak >= 10**(cfg['clip_db']/20):
            raise ValueError('Verification is silent or clipped.')
        # Correct known clock rate for magnitude analysis; absolute phase is not used here.
        ratio = 1+self.state.get('clock_ppm',0)*1e-6
        rec = np.interp(np.arange(int(len(rec)/ratio))*ratio,np.arange(len(rec)),rec)
        onset = int(round(dsp._arrival(rec,sw.signal)))
        pre = int(.04*fs)
        start = max(0,onset-pre)
        f = np.geomspace(lo*1.2,hi/1.2,480)
        h,ir = dsp.transfer(rec[start:],sw.signal*scale,fs,f,pre=onset-start)
        mag = 20*np.log10(np.maximum(abs(h),1e-12))
        if cfg['calibration']:
            mag -= load_cal(cfg['calibration']).at(f)
        mag = peq.smooth_log(f,mag,6)
        return f,mag,ir,rec

    def _verify(self):
        chosen,report = {},{}
        baseline = {}
        with np.load(self.directory/'drivers.npz') as data:
            driver_f = data['freq']
            driver_h = {d['id']:data[d['id']] for d in config.active_drivers(self.state['cfg'])}
        for side in ('L','R'):
            results = []
            for index,c in enumerate(self.state['candidates'][side]):
                self._message(f"Measuring {side} crossover candidate {index+1}/{len(self.state['candidates'][side])}")
                f,mag,ir,rec = self._sum(side,c)
                pairs = config.selected_pairs(self.state['cfg'])
                mask = (f > c['low']*.7 if 'sub' in pairs else np.ones(len(f), dtype=bool))
                mask &= (f < c['high']*1.3 if 'tweeter' in pairs else np.ones(len(f), dtype=bool))
                score = float(np.std(mag[mask]))
                predicted = np.zeros(len(f),dtype=complex)
                for d in config.active_drivers(self.state['cfg']):
                    if d['side'] == side:
                        h = driver_h[d['id']]
                        measured = np.interp(f,driver_f,h.real)+1j*np.interp(f,driver_f,h.imag)
                        predicted += measured*dsp.branch_response(self.state['cfg'],d,c,self.state['eq'][d['id']],f)
                discrepancy = mag[mask]-20*np.log10(np.maximum(abs(predicted[mask]),1e-12))
                discrepancy -= np.median(discrepancy)
                model_error = float(np.sqrt(np.mean(discrepancy**2)))
                if model_error <= self.state['cfg']['verification_db']:
                    results.append((score,c,f,mag,ir,model_error))
                np.savez_compressed(self.directory/f'candidate-{side}-{index}.npz',freq=f,mag=mag,capture=rec)
            if not results:
                raise ValueError(f'{side}: no crossover candidate agreed with the driver measurements. Check timing, microphone placement and levels.')
            score,c,f,mag,ir,model_error = min(results,key=lambda row:row[0])
            chosen[side] = c
            report[side] = dict(ripple_db=score,predicted_score=c['score'],prediction_error_db=model_error)
            baseline[side] = mag
        np.savez_compressed(self.directory/'baseline.npz',freq=f,**baseline)
        self.state.update(chosen=chosen,verification=report,room={},status='await_room',
                          message=f"Move the microphone to listening position 1/{self.state['cfg']['room_positions']}, then Continue.")
        return None

    def _room(self):
        index = min(self.state['room_count'],self.state['cfg']['room_positions']-1)
        if hasattr(self.backend,'position'):
            self.backend.position = index
        rows = {}
        for side in ('L','R'):
            f,mag,ir,rec = self._sum(side,self.state['chosen'][side])
            rows[side] = mag
            rows[side+'_ir'] = ir[:int(self.state['cfg']['fs']*.5)]
        np.savez_compressed(self.directory/f'room-{index}.npz',freq=f,**rows)
        self.state['room_count'] = index+1
        cfg = self.state['cfg']
        if index+1 < cfg['room_positions']:
            self.state.update(status='await_room',message=f"Move the microphone to listening position {index+2}/{cfg['room_positions']}, then Continue.")
            return None
        report = {}
        for side in ('L','R'):
            mags,impulses = [],[]
            for i in range(cfg['room_positions']):
                with np.load(self.directory/f'room-{i}.npz') as data:
                    mags.append(data[side])
                    impulses.append(data[side+'_ir'])
            mean = 10*np.log10(np.mean(10**(np.array(mags)/10),axis=0))
            target = peq.harman_target(f,cfg['target_bass_db'],105,cfg['target_tilt'])
            # Broad spatial correction; avoid narrow, position-specific high-frequency filters.
            weights,decay = dsp.decay_weights(impulses,cfg['fs'],f)
            filters,info = peq.fit_peq(f,mean,target,cfg['fs'],6,float(f[0]),float(f[-1]),
                                     cfg['max_boost_db'],cfg['max_cut_db'],2,ref_band=(300,3000),smooth=3,weights=weights)
            self.state['room'][side] = [asdict(p) for p in filters]
            report[side] = dict(before_db=info['rms_before'],predicted_after_db=info['rms_after'],decay=decay,
                                spatial_spread_db=float(np.mean(np.std(mags,axis=0))))
        self.state.update(room_report=report,status='await_reference',message='Return the microphone to the original driver/crossover reference position, then Continue for final verification.')
        return None

    def _finish(self):
        document,scales = export(self.state['cfg'],self.state['chosen'],self.state['eq'],self.state['room'])
        self.state['preamp'] = scales
        atomic(self.directory/'camilladsp.yml',yaml.safe_dump(document,sort_keys=False))
        verification = {}
        with np.load(self.directory/'baseline.npz') as baseline:
            for side in ('L','R'):
                f,mag,ir,rec = self._sum(side,self.state['chosen'][side],self.state['room'][side])
                before = baseline[side]
                room = peq.eq_db(f,self.state['cfg']['fs'],[peq.Peq(**p) for p in self.state['room'][side]])
                difference = mag-before-room
                difference -= np.median(difference)
                error = float(np.sqrt(np.mean(difference**2)))
                if error > self.state['cfg']['verification_db']:
                    raise ValueError(f'{side}: measured filters disagree with prediction ({error:.1f} dB). Check microphone placement and repeat.')
                np.savez_compressed(self.directory/f'final-{side}.npz',freq=f,before=before,after=mag,capture=rec)
                verification[side] = dict(prediction_error_db=error)
        self.state.update(final_verification=verification,status='ready',message='Tuning verified. Review the results and apply the configuration.',config_path=str(self.directory/'camilladsp.yml'))
        atomic(self.directory/'report.json',json.dumps(self.state,indent=2))
        return None

    def _apply(self):
        if self.backend.demo:
            self.backend.applied = self.state
        def verify():
            for side in ('L','R'):
                f,mag,ir,rec = self._sum(side,self.state['chosen'][side],self.state['room'][side],loopback=True)
                with np.load(self.directory/f'final-{side}.npz') as before:
                    error = float(np.sqrt(np.mean((mag-before['after'])**2)))
                if error > self.state['cfg']['verification_db']:
                    raise ValueError(f'{side}: active CamillaDSP path differs by {error:.1f} dB; rolling back.')
        Deployment(self.state['cfg'],self.backend,self.directory).apply(self.directory/'camilladsp.yml',verify)
        self.state.update(status='applied',audio_owned=False,message='Configuration active and acoustic verification passed. Listening level is −36 dB; playback is muted until you resume music.')
        return None

    def _rollback(self):
        Deployment(self.state['cfg'],self.backend,self.directory).rollback()
        self.state.update(status='ready',message='Previous configuration selection restored; playback remains stopped. The tuned candidate is available to apply again.')
        return None
