#!/usr/bin/env python3
"""Translate the saved DML2 panel EQ exports without changing their coefficients."""
import json
from pathlib import Path
import re
import sys
import yaml


def parse_export(text, prefix):
    preamp = re.search(r'^Preamp:\s*([-+\d.]+) dB$', text, re.M)
    if not preamp or float(preamp.group(1)) > 0:
        raise ValueError('Expected a nonpositive exported preamp')
    names = [prefix + '_headroom']
    filters = {names[0]: {'type': 'Gain', 'parameters': {'gain': float(preamp.group(1)), 'inverted': False, 'mute': False}}}
    count = 0
    for line in text.splitlines():
        if not line.startswith('Filter '):
            continue
        match = re.fullmatch(r'Filter (\d+): ON (PK|HPQ|LPQ) Fc ([\d.]+) Hz(?: Gain ([-+\d.]+) dB)? Q ([\d.]+)', line)
        if not match:
            raise ValueError('Unsupported exported filter: ' + line)
        number, kind, frequency, gain, q = match.groups()
        parameters = dict(type={'PK': 'Peaking', 'HPQ': 'Highpass', 'LPQ': 'Lowpass'}[kind], freq=float(frequency), q=float(q))
        if not 0 < parameters['freq'] < 24000 or parameters['q'] <= 0:
            raise ValueError('Filter is outside the supported range')
        if kind == 'PK':
            if gain is None or not -30 <= float(gain) <= 3:
                raise ValueError('Exported EQ gain is outside the expected range')
            parameters['gain'] = float(gain)
        name = prefix + '_' + number
        filters[name] = dict(type='Biquad', parameters=parameters)
        names.append(name)
        count += 1
    if not count:
        raise ValueError('No exported filters found')
    return filters, names


def build(base, left_text, right_text):
    config = dict(base)
    config['title'] = 'Measured DML2 panel EQ'
    config['description'] = 'Saved left/right panel EQ. 1/2 EX30, 3/4 EX32; 5-8 silent. Temporary 500 Hz–5 kHz limits remain.'
    filters = dict(base['filters'])
    left, left_names = parse_export(left_text, 'panel_left')
    right, right_names = parse_export(right_text, 'panel_right')
    filters.update(left)
    filters.update(right)
    config['filters'] = filters
    config['pipeline'] = [dict(type='Filter', channels=[0], names=left_names),
                          dict(type='Filter', channels=[1], names=right_names), *base['pipeline']]
    return config


if __name__ == '__main__':
    base_path, left_path, right_path, output_path = map(Path, sys.argv[1:])
    config = build(yaml.safe_load(base_path.read_text()), left_path.read_text(), right_path.read_text())
    output_path.write_text('# Saved 2026-10-05 DML2 panel EQ; panel labels confirmed by the user.\n' + yaml.safe_dump(config, sort_keys=False))
    print(json.dumps({'left_filters': len([k for k in config['filters'] if k.startswith('panel_left_')]),
                      'right_filters': len([k for k in config['filters'] if k.startswith('panel_right_')]),
                      'output': str(output_path)}))
