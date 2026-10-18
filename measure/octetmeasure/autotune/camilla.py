"""Exact SOS export and transactional CamillaDSP activation."""
import json
import os
from pathlib import Path
import subprocess
import time

import numpy as np
from scipy.signal import sosfreqz
import yaml

from .. import peq
from .dsp import branch_sos


def atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def export(cfg, chosen, eq, room):
    fs = cfg['fs']
    filters, pipeline, mapping, scales = {}, [], [], {}
    grid = np.geomspace(10, fs*.499, 4096)
    for side in ('L','R'):
        peak = 0.
        for d in cfg['drivers']:
            if d['side'] != side:
                continue
            sections = branch_sos(cfg,d,chosen[side],eq[d['id']])
            if room.get(side):
                sections = np.vstack([peq.sos_for([peq.Peq(**p) for p in room[side]],fs),sections])
            _, h = sosfreqz(sections, worN=grid, fs=fs)
            peak = max(peak, float(20*np.log10(max(abs(h))))+chosen[side]['gains'][d['role']]-d['max_db'])
        # Extra transient headroom; per-output limiter enforces the final sample ceiling.
        scales[side] = 10**(-(max(0.,peak)+3)/20)
    for d in cfg['drivers']:
        key, side, channel = d['id'], d['side'], d['output']-1
        c = chosen[side]
        mapping.append(dict(dest=channel,sources=[dict(channel=0 if side=='L' else 1,gain=0)]))
        sections = branch_sos(cfg,d,c,eq[key])
        if room.get(side):
            sections = np.vstack([peq.sos_for([peq.Peq(**p) for p in room[side]],fs),sections])
        names = []
        for index, row in enumerate(sections):
            name = f'{key}_sos{index}'
            b0,b1,b2,a0,a1,a2 = map(float,row)
            filters[name] = dict(type='Biquad',parameters=dict(type='Free',b0=b0/a0,b1=b1/a0,b2=b2/a0,a1=a1/a0,a2=a2/a0))
            names.append(name)
        filters[f'{key}_gain'] = dict(type='Gain',parameters=dict(gain=float(20*np.log10(scales[side]))+c['gains'][d['role']],inverted=c['polarity'][d['role']]<0,mute=False))
        samples = c['delays'][d['role']]*fs/1000
        whole = int(np.floor(samples))
        fraction = samples-whole
        filters[f'{key}_delay'] = dict(type='Delay',parameters=dict(delay=whole,unit='samples',subsample=False))
        # Match the measurement renderer exactly instead of Camilla's different
        # allpass approximation for subsample delays.
        filters[f'{key}_fractional'] = dict(type='Conv',parameters=dict(type='Values',values=[1-fraction,fraction]))
        filters[f'{key}_limit'] = dict(type='Limiter',parameters=dict(clip_limit=d['max_db'],soft_clip=False))
        names += [f'{key}_gain',f'{key}_delay',f'{key}_fractional',f'{key}_limit']
        pipeline.append(dict(type='Filter',channels=[channel],names=names))
    document = dict(title='Octet Auto Tune',devices=dict(samplerate=fs,chunksize=1024,
                    capture=dict(type='Alsa',channels=2,device=cfg['camilla']['capture'],format='S32_LE'),
                    playback=dict(type='Alsa',channels=8,device=cfg['camilla']['playback'],format='S32_LE')),
                    mixers=dict(drivers=dict(channels={'in':2,'out':8},mapping=mapping)),filters=filters,
                    pipeline=[dict(type='Mixer',name='drivers'),*pipeline])
    return document, scales


class Deployment:
    def __init__(self, cfg, backend, directory):
        self.cfg,self.backend,self.directory = cfg,backend,Path(directory)

    def apply(self, config_path, verify):
        """Only publish last-good AFTER active-path acoustic verification succeeds."""
        if self.backend.demo:
            verify()
            return
        c = self.cfg['camilla']
        subprocess.run([c['binary'],'--check',str(config_path)],check=True,capture_output=True,text=True,timeout=20)
        state = Path(c['statefile'])
        original = state.read_text(encoding='utf-8') if state.exists() else None
        prior = yaml.safe_load(original) if original else {}
        prior = prior or {}
        self.backend.systemctl('stop')
        # The journal survives a process crash during application.
        atomic(self.directory/'deployment.json',json.dumps(dict(statefile=str(state),previous=original,pending=True)))
        updated = {**prior,'config_path':str(Path(config_path).resolve()),'mute':[False]*5,'volume':[0.]*5}
        try:
            atomic(state,yaml.safe_dump(updated))
            self.backend.systemctl('start')
            time.sleep(1)
            self.backend.systemctl('is-active')
            verify()
        except BaseException:
            self.backend.systemctl('stop')
            if original is not None:
                # Restore the previous config selection but leave playback stopped.
                atomic(state,original)
            raise
        atomic(self.directory/'last-good.yml',Path(config_path).read_text(encoding='utf-8'))
        atomic(self.directory/'deployment.json',json.dumps(dict(statefile=str(state),previous=original,pending=False)))

    def rollback(self):
        if self.backend.demo:
            return
        journal = json.loads((self.directory/'deployment.json').read_text(encoding='utf-8'))
        self.backend.systemctl('stop')
        if journal['previous'] is None:
            raise ValueError('No previous CamillaDSP state was saved; playback remains stopped.')
        atomic(journal['statefile'],journal['previous'])
        journal['pending'] = False
        atomic(self.directory/'deployment.json',json.dumps(journal))
