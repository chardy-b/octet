#!/usr/bin/env python3
"""Assign explicitly identified exciter measurements to their matching stereo pairs."""
import argparse
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


def build(base, ex30_text=None, ex32_text=None, ex30_source=None, ex32_source=None):
    if ex30_text is None and ex32_text is None:
        raise ValueError('Supply at least one verified exciter measurement')
    config = dict(base)
    config['title'] = 'Measured exciter EQ'
    config['description'] = 'Verified EX30 EQ uses outputs 1/2; verified EX32 EQ uses 3/4 when assigned. Unmeasured outputs retain audition filters; 5-8 silent.'
    filters = dict(base['filters'])
    stages = []
    for model, text, source, channels in (
            ('ex30', ex30_text, ex30_source, [0, 1]),
            ('ex32', ex32_text, ex32_source, [2, 3])):
        if text is None:
            continue
        prefix = 'exciter_' + model
        assigned, names = parse_export(text, prefix)
        if source:
            assigned[prefix + '_headroom']['description'] = str(source)
        filters.update(assigned)
        stages.append(dict(type='Filter', channels=channels, names=names))
    config['filters'] = filters
    config['pipeline'] = [*base['pipeline'], *stages]
    return config


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', type=Path)
    parser.add_argument('--ex30', type=Path, help='Export confirmed to measure the EX30HESF2-4')
    parser.add_argument('--ex32', type=Path, help='Export confirmed to measure the EX32VBDS-4')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.ex30 is None and args.ex32 is None:
        parser.error('Supply --ex30 and/or --ex32 with verified exports')
    config = build(yaml.safe_load(args.base.read_text()),
                   args.ex30.read_text() if args.ex30 else None, args.ex32.read_text() if args.ex32 else None,
                   str(args.ex30) if args.ex30 else None, str(args.ex32) if args.ex32 else None)
    args.output.write_text('# Saved exciter EQ, explicitly assigned by model.\n' + yaml.safe_dump(config, sort_keys=False))
    print(json.dumps({'ex30': str(args.ex30), 'ex32': str(args.ex32), 'output': str(args.output)}))
