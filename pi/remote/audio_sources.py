"""Discover USB capture sources and feed an unchanged stereo speaker pipeline."""
import copy
import json
from pathlib import Path
import re

SOURCE_MIXER = 'octet_source'
SOURCE_DESCRIPTION = 'Octet source: '
CLOCK_KEYS = ('resampler', 'enable_rate_adjust', 'capture_samplerate', 'target_level', 'adjust_period')
TIDAL = dict(id='tidal', label='Tidal', group='Music', available=True,
             description='Tidal and the saved music queue.')


def discover_sources(proc=Path('/proc/asound')):
    """Only advertise real USB capture endpoints, never HDMI outputs or loopback."""
    sources = [dict(TIDAL)]
    for card in sorted(proc.glob('card[0-9]*')):
        try:
            card_id = (card / 'id').read_text().strip()
            stream = (card / 'stream0').read_text()
        except OSError:
            continue
        if not re.fullmatch(r'[A-Za-z0-9_]+', card_id) or 'Capture:' not in stream:
            continue
        capture = stream.split('Capture:', 1)[1]
        # Use one hardware-supported alternate setting, without ALSA conversion.
        mode = next((block for block in capture.split('Altset ')[1:]
                     if re.search(r'Format:\s+S32_LE\b', block)), None)
        channels = re.search(r'Channels:\s+(\d+)', mode or '')
        rates = re.search(r'Rates:\s+([\d, ]+)', mode or '')
        if not channels or not rates:
            continue
        count = int(channels[1])
        if not 1 <= count <= 64:
            continue
        supported_rates = [int(rate) for rate in re.findall(r'\d+', rates[1])]
        name = stream.splitlines()[0].split(' at usb-', 1)[0].removesuffix(' : USB Audio')
        if name.startswith('Audient Audient '):
            name = name.removeprefix('Audient ')
        # iD14 channels 3–10 are optical, not eight extra analogue sockets.
        # Do not offer these until external digital clocking has been configured.
        analogue = min(count, 2) if card_id == 'iD14' else count
        selections = [(f'pair-{ch + 1}', [ch, ch + 1], f'Inputs {ch + 1} / {ch + 2} · stereo')
                      for ch in range(0, analogue - 1, 2)]
        selections += [(f'mono-{ch + 1}', [ch, ch], f'Input {ch + 1} · mono') for ch in range(analogue)]
        for key, mapping, label in selections:
            sources.append(dict(id=f'usb:{card_id}:{key}', label=f'{name} · {label}', group=name,
                                available=True, device=f'hw:CARD={card_id},DEV=0',
                                channels=count, mapping=mapping, rates=supported_rates,
                                description=(f'{label}. Uses the existing speaker crossover, EQ and output levels.'
                                             + (' Optical inputs require a separate clock setup.' if card_id == 'iD14' else ''))))
    return sources


def source_metadata(config):
    stage = next((stage for stage in config.get('pipeline', [])
                  if stage.get('type') == 'Mixer' and stage.get('name') == SOURCE_MIXER), None)
    if stage is None:
        return dict(id='tidal', label='Tidal') if config.get('devices', {}).get('capture', {}).get('device', '').startswith('hw:Loopback') else dict(id='unknown', label='Other source')
    description = config['mixers'][SOURCE_MIXER].get('description', '')
    if not description.startswith(SOURCE_DESCRIPTION):
        raise RuntimeError('The active source configuration is not recognised')
    return json.loads(description[len(SOURCE_DESCRIPTION):])


def meter_channels(config):
    """The visualizer follows the selected input, including duplicated mono."""
    if source_metadata(config)['id'] in ('tidal', 'unknown'):
        return (0, 1)
    mapping = config['mixers'][SOURCE_MIXER]['mapping']
    return tuple(row['sources'][0]['channel'] for row in sorted(mapping, key=lambda row: row['dest']))


def source_config(config, source):
    """Prepend a capture-to-stereo mixer; leave every speaker setting intact."""
    result = copy.deepcopy(config)
    metadata = source_metadata(result)
    devices = result['devices']
    if metadata['id'] == 'unknown':
        raise RuntimeError('The current capture device is not managed by the music page')
    if metadata['id'] != 'tidal':
        result['pipeline'] = [stage for stage in result['pipeline']
                              if not (stage.get('type') == 'Mixer' and stage.get('name') == SOURCE_MIXER)]
        del result['mixers'][SOURCE_MIXER]
        devices['capture'] = metadata['tidal_capture']
        for key, saved in metadata['tidal_clock'].items():
            if saved['present']:
                devices[key] = saved['value']
            else:
                devices.pop(key, None)
    if source['id'] == 'tidal':
        return result
    rate = devices['samplerate']
    if rate not in source['rates']:
        raise ValueError('This input does not support the speaker sample rate')
    saved = dict(id=source['id'], label=source['label'],
                 tidal_capture=copy.deepcopy(devices['capture']),
                 tidal_clock={key: dict(present=key in devices, value=devices.get(key)) for key in CLOCK_KEYS})
    devices['capture'] = dict(type='Alsa', channels=source['channels'], device=source['device'], format='S32_LE')
    devices.update(resampler=dict(type='AsyncSinc', profile='Balanced'), enable_rate_adjust=True,
                   capture_samplerate=rate, target_level=devices['chunksize'], adjust_period=10)
    result.setdefault('mixers', {})[SOURCE_MIXER] = dict(
        description=SOURCE_DESCRIPTION + json.dumps(saved, separators=(',', ':')),
        channels={'in': source['channels'], 'out': 2},
        mapping=[dict(dest=dest, sources=[dict(channel=channel, gain=0)])
                 for dest, channel in enumerate(source['mapping'])])
    result['pipeline'].insert(0, dict(type='Mixer', name=SOURCE_MIXER))
    return result
