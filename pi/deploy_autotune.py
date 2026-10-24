#!/usr/bin/env python3
"""Install Auto mode without changing the active DSP or starting test playback."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

import yaml

stage = Path(sys.argv[1]).resolve()
remote = Path('/opt/octet-remote')
engine = Path('/opt/octet-measure')
data = Path('/var/lib/octet-autotune')
names = ['server.py', 'dsp.js', 'dsp.html', 'style.css', 'auto.js', 'measurements.js', 'tuning_proxy.py']
expected = json.loads((stage/'expected.json').read_text())
for name, sha in expected.items():
    current = hashlib.sha256((remote/name).read_bytes()).hexdigest()
    release = hashlib.sha256((stage/'pi/remote'/name).read_bytes()).hexdigest()
    assert current in (sha, release), f'Live {name} changed; inspect before deploying.'

sys.path.insert(0, str(remote))
import server
with server.DSP() as dsp:
    before = {cmd: dsp.call(cmd) for cmd in ('GetConfigJson', 'GetConfigFilePath', 'GetVolume', 'GetMute')}
pids = subprocess.check_output(['systemctl', 'show', '--property=MainPID', 'camilladsp', 'mpd'])
backup = Path('/var/backups/octet-autotune')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup.mkdir(parents=True)
for name in names:
    if (remote/name).exists():
        shutil.copy2(remote/name, backup/name)
shutil.copy2('/var/lib/octet-dsp/channel-dsp.yml', backup/'channel-dsp.yml')
shutil.copy2('/opt/camilladsp/statefile.yml', backup/'statefile.yml')

engine.mkdir(exist_ok=True)
shutil.copytree(stage/'measure/octetmeasure', engine/'octetmeasure', dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
calibration = engine/'EMM-6_38327.txt'
shutil.copy2(stage/'measure/emm6_38327.txt', calibration)
subprocess.run(['install', '-d', '-m', '0750', '-o', 'octet', '-g', 'audio', str(data)], check=True)
sys.path.insert(0, str(engine))
from octetmeasure.autotune import config
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.web import check_safety

cfg = config.example()
cfg.update(hardware_confirmed=True, input_device='Audient iD14', output_device='snd_rpi_hifiberry_dac8x',
           calibration=str(calibration), room_positions=1, duration=2, mic_channel=0)
cfg['camilla']['loopback_output'] = 'Loopback: PCM (hw:10,0)'
for driver in cfg['drivers']:
    driver.update(start_db=-54, max_db=-30, step_seconds=.5)
config.validate(cfg, hardware=True)
profile = data/'speaker.yaml'
if not profile.exists():
    profile.write_text(yaml.safe_dump(cfg, sort_keys=False))
    shutil.chown(profile, user='octet', group='audio')
    profile.chmod(0o640)

# Compile a real representative eight-output result with the installed CamillaDSP.
roles = ('sub','a','b','tweeter')
candidate = dict(strategy='both', order=4, low=500, high=4200, split=1000,
                 gains=dict.fromkeys(roles,0.), delays=dict.fromkeys(roles,0.), polarity=dict.fromkeys(roles,1))
document, _ = export(cfg, {s:candidate for s in ('L','R')}, {d['id']:[] for d in cfg['drivers']}, {})
check_safety(document, limits=True)
validation = stage/'validation.yml'
validation.write_text(yaml.safe_dump(document, sort_keys=False))
subprocess.run(['/opt/camilladsp/camilladsp','--check',str(validation)],check=True)

unit = Path('/etc/systemd/system/octet-autotune.service')
try:
    shutil.copy2(stage/'pi/systemd/octet-autotune.service', unit)
    subprocess.run(['systemctl','daemon-reload'], check=True)
    subprocess.run(['systemctl','enable','--now','octet-autotune'], check=True)
    for attempt in range(50):
        try:
            with urlopen('http://127.0.0.1:8766/api/auto', timeout=2) as response:
                assert json.load(response)['available']
            break
        except OSError:
            time.sleep(.2)
    else:
        raise RuntimeError('Auto Tune did not become healthy')
    for name in names:
        shutil.copy2(stage/'pi/remote'/name, remote/name)
        (remote/name).chmod(0o644)
    subprocess.run(['systemctl','restart','octet-remote'], check=True)
    for attempt in range(50):
        try:
            with urlopen('http://100.100.177.81:8767/api/auto', timeout=2) as response:
                assert json.load(response)['available']
            break
        except OSError:
            time.sleep(.2)
    else:
        raise RuntimeError('Music web integration did not become healthy')
    with server.DSP() as dsp:
        after = {cmd: dsp.call(cmd) for cmd in before}
    assert before == after, 'Active audio settings changed during installation'
    assert subprocess.check_output(['systemctl','show','--property=MainPID','camilladsp','mpd']) == pids
except BaseException:
    for name in names:
        if (backup/name).exists():
            shutil.copy2(backup/name, remote/name)
    subprocess.run(['systemctl','stop','octet-autotune'])
    subprocess.run(['systemctl','restart','octet-remote'])
    raise
print(json.dumps(dict(deployed=True, backup=str(backup), audio_settings_preserved=True,
                      test_playback_started=False, microphone='Audient iD14 input 1', tweeter_safety_hz=3500)))
