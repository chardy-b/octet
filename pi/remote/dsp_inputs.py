"""Profile + input: the one place a DSP configuration gets its input.

A saved profile holds the speaker settings (crossovers, EQ, levels, routing). The
input it happens to contain is ignored: with_input() strips it and applies the
input in use, so any profile works with any input. Tidal, AirPlay, Bluetooth and
USB inputs all go through here; nothing else edits devices.capture.
"""
import copy
import json
import re

SOURCE_MIXER = 'octet_source'
SOURCE_DESCRIPTION = 'Octet source: '
CLOCK_KEYS = ('resampler', 'enable_rate_adjust', 'capture_samplerate', 'target_level', 'adjust_period')
TIDAL_DEVICE = 'hw:Loopback,1,0'
AIRPLAY_DEVICE = 'hw:Loopback,1,1'
BLUETOOTH_DEVICE = 'hw:Loopback,1,2'


def speaker_part(config):
    """The profile with Tidal input: any other input, resampling or source mixer removed."""
    result = copy.deepcopy(config)
    result['pipeline'] = [stage for stage in result.get('pipeline', [])
                          if not (stage.get('type') == 'Mixer' and stage.get('name') == SOURCE_MIXER)]
    result.get('mixers', {}).pop(SOURCE_MIXER, None)
    devices = result['devices']
    # Keep any extra keys CamillaDSP reports (stop_on_inactive, labels, ...).
    capture = {key: value for key, value in devices.get('capture', {}).items()
               if key not in ('type', 'channels', 'device', 'format')}
    devices['capture'] = dict(type='Alsa', channels=2, device=TIDAL_DEVICE, format='S32_LE', **capture)
    for key in CLOCK_KEYS:
        devices.pop(key, None)
    return result


def _resampled(devices, rate):
    devices.update(resampler=dict(type='AsyncSinc', profile='Balanced'), enable_rate_adjust=True,
                   capture_samplerate=rate, target_level=devices['chunksize'], adjust_period=10)


def with_input(config, source, *, rate=None, sample_format='S32_LE'):
    """The profile's speaker settings fed from source.

    source is an id ('tidal', 'airplay', 'bluetooth') or a USB source dict from
    audio_sources.discover_sources. Bluetooth needs the stream's rate and format.
    """
    result = speaker_part(config)
    devices = result['devices']
    identifier = source if isinstance(source, str) else source['id']
    if identifier == 'tidal':
        return result
    playback = devices.get('playback', {})
    # A real output, with no ALSA conversion and never back into the loopback.
    if (playback.get('type') != 'Alsa'
            or not re.fullmatch(r'hw:[A-Za-z0-9_=,]+', str(playback.get('device', '')))
            or 'Loopback' in playback['device']):
        raise ValueError('The profile must play to a hardware output (hw:...), not the loopback')
    if identifier == 'airplay':
        devices['capture'].update(device=AIRPLAY_DEVICE, format='S32_LE')
        _resampled(devices, 44100)
        return result
    if identifier == 'bluetooth':
        if (type(rate) is not int or rate not in (44100, 48000)
                or sample_format not in ('S16_LE', 'S32_LE')):
            raise ValueError('Unsupported Bluetooth PCM parameters')
        devices['capture'].update(device=BLUETOOTH_DEVICE, format=sample_format)
        _resampled(devices, rate)
        return result
    if isinstance(source, str):
        raise ValueError('Unknown input')
    rate = devices['samplerate']
    if rate not in source['rates']:
        raise ValueError('This input does not support the speaker sample rate')
    devices['capture'] = dict(type='Alsa', channels=source['channels'], device=source['device'], format='S32_LE')
    _resampled(devices, rate)
    saved = dict(id=source['id'], label=source['label'])
    result.setdefault('mixers', {})[SOURCE_MIXER] = dict(
        description=SOURCE_DESCRIPTION + json.dumps(saved, separators=(',', ':')),
        channels={'in': source['channels'], 'out': 2},
        mapping=[dict(dest=dest, sources=[dict(channel=channel, gain=0)])
                 for dest, channel in enumerate(source['mapping'])])
    result['pipeline'].insert(0, dict(type='Mixer', name=SOURCE_MIXER))
    return result


def input_of(config):
    """Which input a running configuration uses: dict(id=..., label=...)."""
    stage = next((stage for stage in config.get('pipeline', [])
                  if stage.get('type') == 'Mixer' and stage.get('name') == SOURCE_MIXER), None)
    if stage is None:
        device = config.get('devices', {}).get('capture', {}).get('device', '')
        return {TIDAL_DEVICE: dict(id='tidal', label='Tidal'),
                AIRPLAY_DEVICE: dict(id='airplay', label='AirPlay'),
                BLUETOOTH_DEVICE: dict(id='bluetooth', label='Bluetooth')}.get(
                    device, dict(id='unknown', label='Other source'))
    description = config['mixers'][SOURCE_MIXER].get('description') or ''
    if not description.startswith(SOURCE_DESCRIPTION):
        raise RuntimeError('The active source configuration is not recognised')
    saved = json.loads(description[len(SOURCE_DESCRIPTION):])
    return dict(id=saved['id'], label=saved['label'])
