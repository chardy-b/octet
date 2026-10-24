"""Validated speaker profiles. Physical output numbers are always one-based."""
import copy
import math
import re
from pathlib import Path

import yaml

TWEETER_MIN_HZ = 3500
PAIRS = ('a', 'b', 'sub', 'tweeter')


def selected_pairs(cfg):
    return cfg.get('selected_pairs', list(PAIRS))


def active_drivers(cfg):
    return [d for d in cfg['drivers'] if d['role'] in selected_pairs(cfg)]


def example():
    drivers = []
    outputs = {'a': (1, 2), 'b': (3, 4), 'sub': (5, 6), 'tweeter': (7, 8)}
    models = {'a': 'Dayton EX30HESF2-4', 'b': 'Dayton EX32VBDS-4',
              'sub': 'Dayton 12-inch Reference kit, sealed', 'tweeter': 'Dayton PRO Neo-8 / H6512'}
    for side in ('L', 'R'):
        for role, low, high in [('sub', 30, 700), ('a', 150, 12000), ('b', 150, 12000), ('tweeter', TWEETER_MIN_HZ, 20000)]:
            drivers.append(dict(id=f'{side}_{role}', side=side, role=role, output=outputs[role][0 if side == 'L' else 1],
                                model=models[role],
                                min_hz=low, max_hz=high, order=4, start_db=-48, max_db=-24,
                                step_db=3, step_seconds=1, amp_group=role))
    return dict(version=1, hardware_confirmed=False, fs=48000, duration=2, gap_seconds=1,
                input_device='', output_device='', input_channels=2, mic_channel=0,
                calibration='', max_boost_db=3, max_cut_db=9, peq_bands=4, quiesce_services=['upmpdcli','mpd'],
                clip_db=-3, jump_db=9, min_snr_db=15, room_positions=5,
                gate_ms=8, target_bass_db=4, target_tilt=-0.4,
                verification_db=4, max_clock_ppm=300, drivers=drivers,
                automatic=False, auto_apply=False, sub_crossover_max=500, selected_pairs=list(PAIRS),
                camilla=dict(binary='/opt/camilladsp/camilladsp', service='camilladsp',
                             statefile='/opt/camilladsp/statefile.yml',
                             capture='hw:Loopback,1,0', playback='hw:sndrpihifiberry,0',
                             loopback_output='Loopback'))


def number(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f'{name} must be a finite number between {lo} and {hi}.')


def validate(raw, hardware=False):
    if not isinstance(raw, dict):
        raise ValueError('Speaker profile must be a YAML mapping.')
    cfg = copy.deepcopy(raw)
    defaults = example()
    for key, value in defaults.items():
        cfg.setdefault(key, value)
    for key, lo, hi in [('fs', 44100, 96000), ('duration', 1, 10), ('gap_seconds', 0.5, 5),
                        ('max_boost_db', 0, 3), ('max_cut_db', 0, 18), ('peq_bands', 0, 8),
                        ('clip_db', -18, -0.5), ('jump_db', 3, 18), ('min_snr_db', 6, 60),
                        ('room_positions', 1, 9), ('gate_ms', 2, 50), ('verification_db', 1, 8),
                        ('max_clock_ppm', 10, 1000), ('target_bass_db', 0, 8), ('target_tilt', -2, 0),
                        ('input_channels', 1, 8), ('mic_channel', 0, 7), ('sub_crossover_max', 200, 500)]:
        number(cfg[key], key, lo, hi)
    for key in ('fs', 'peq_bands', 'room_positions', 'input_channels', 'mic_channel'):
        if int(cfg[key]) != cfg[key]:
            raise ValueError(f'{key} must be an integer.')
        cfg[key] = int(cfg[key])
    if cfg['mic_channel'] >= cfg['input_channels']:
        raise ValueError('Microphone channel is outside the capture channels (zero-based).')
    drivers = cfg['drivers']
    pairs = cfg['selected_pairs']
    if (not isinstance(pairs, list) or any(not isinstance(p, str) or p not in PAIRS for p in pairs)
            or len(set(pairs)) != len(pairs)):
        raise ValueError('selected_pairs must list unique channel pairs: a, b, sub, tweeter.')
    if not {'a', 'b'}.intersection(pairs):
        raise ValueError('Select at least one exciter pair: A (1/2) or B (3/4).')
    cfg['selected_pairs'] = [p for p in PAIRS if p in pairs]
    for key in ('automatic', 'auto_apply'):
        if type(cfg[key]) is not bool:
            raise ValueError(f'{key} must be a boolean.')
    if cfg['automatic'] and cfg['room_positions'] != 1:
        raise ValueError('Automatic mode uses one fixed microphone position.')
    if not isinstance(drivers, list) or len(drivers) != 8:
        raise ValueError('Exactly eight drivers are required.')
    if any(not isinstance(d,dict) or type(d.get('output')) is not int or not isinstance(d.get('id'),str) for d in drivers):
        raise ValueError('Every driver needs a string ID and integer output number.')
    if {d.get('output') for d in drivers} != set(range(1, 9)):
        raise ValueError('Each physical output 1 through 8 must occur exactly once.')
    if {d.get('id') for d in drivers} != {f'{s}_{r}' for s in ('L', 'R') for r in ('sub', 'a', 'b', 'tweeter')}:
        raise ValueError('Driver IDs must be L_sub, L_a, L_b, L_tweeter and their R equivalents.')
    for d in drivers:
        if type(d['output']) is not int:
            raise ValueError('Output numbers must be integers.')
        if d['id'] != f"{d.get('side')}_{d.get('role')}":
            raise ValueError('Driver ID, side and role must agree.')
        for key, lo, hi in [('min_hz', 10, 10000), ('max_hz', 30, cfg['fs'] * .49),
                            ('start_db', -80, -6), ('max_db', -60, -3), ('step_db', .5, 3),
                            ('step_seconds', .25, 10)]:
            number(d.get(key), f"{d['id']}.{key}", lo, hi)
        if d['min_hz'] >= d['max_hz'] or d['start_db'] > d['max_db']:
            raise ValueError(f"Invalid frequency or ramp limits for {d['id']}.")
        if d.get('order') not in (4, 8):
            raise ValueError('Protection order must be 4 or 8 (LR24 or LR48).')
        if d['role'] == 'tweeter' and d['min_hz'] < TWEETER_MIN_HZ:
            raise ValueError('Tweeter safety high-pass must remain at or above 3500 Hz.')
        expected = {'a': (1, 2), 'b': (3, 4), 'sub': (5, 6), 'tweeter': (7, 8)}
        if hardware and d['output'] != expected[d['role']][d['side'] == 'R']:
            raise ValueError('Hardware output mapping must preserve tweeters on outputs 7/8.')
    c = cfg['camilla']
    if not isinstance(cfg['quiesce_services'],list) or any(not isinstance(s,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*',s) for s in cfg['quiesce_services']):
        raise ValueError('quiesce_services must be a list of service names.')
    if not isinstance(c, dict) or any(not isinstance(c.get(k), str) or not c[k].strip()
                                    for k in defaults['camilla']):
        raise ValueError('Complete the CamillaDSP device, service and path settings.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*',c['service']):
        raise ValueError('Invalid CamillaDSP service name.')
    if hardware:
        if cfg['hardware_confirmed'] is not True:
            raise ValueError('Confirm the physical output map and driver limits in speaker.yaml before hardware playback.')
        if not cfg['input_device'] or not cfg['output_device']:
            raise ValueError('Set explicit input_device and output_device names for hardware.')
        if not cfg['calibration'] or not Path(cfg['calibration']).is_file():
            raise ValueError('Set calibration to the verified microphone calibration file on the Pi.')
    return cfg


def load(path, hardware=False):
    return validate(parse(Path(path).read_text(encoding='utf-8')), hardware)


def parse(text):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f'Invalid speaker YAML: {e}') from e


def ramp(driver):
    level = driver['start_db']
    while level < driver['max_db']:
        yield level
        level = min(driver['max_db'], level + driver['step_db'])
    yield driver['max_db']
