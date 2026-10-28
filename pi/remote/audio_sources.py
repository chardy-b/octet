"""Discover USB capture sources and feed an unchanged stereo speaker pipeline."""
import copy
import ctypes
import subprocess
import sys
import json
import time
from pathlib import Path
import re

SOURCE_MIXER = 'octet_source'
SOURCE_DESCRIPTION = 'Octet source: '
CLOCK_KEYS = ('resampler', 'enable_rate_adjust', 'capture_samplerate', 'target_level', 'adjust_period')
TIDAL = dict(id='tidal', label='Tidal', group='Music', available=True,
             description='Tidal and the saved music queue.')

AIRPLAY = dict(id='airplay', label='AirPlay', group='Music', available=True,
               device='hw:Loopback,1,1', channels=2, mapping=[0, 1], rates=[44100],
               description='Cast to Octet on your LAN. Phone volume attenuates the input; automatic master starts at −18 dB.')


def pcm_fields(path):
    return dict(line.split(':', 1) for line in path.read_text().splitlines() if ':' in line)


def pcm_params_match(path):
    try:
        fields = {key.strip(): value.strip() for key, value in pcm_fields(path).items()}
        return (fields.get('format') == 'S32_LE' and fields.get('channels') == '2'
                and fields.get('rate', '').split()[0] == '44100')
    except (OSError, IndexError):
        return False


def loopback_card(proc):
    """Resolve the ALSA ID and verify the kernel driver and both exact endpoints."""
    card = (proc / 'Loopback').resolve(strict=True)
    if card.parent != proc.resolve() or not re.fullmatch(r'card[0-9]+', card.name):
        raise ValueError('Invalid Loopback card link')
    index = int(card.name[4:])
    if (card / 'id').read_text().strip() != 'Loopback':
        raise ValueError('Incorrect ALSA card ID')
    modules = dict(line.split() for line in (proc / 'modules').read_text().splitlines())
    if modules.get(str(index)) != 'snd_aloop':
        raise ValueError('Loopback must use snd_aloop')
    for endpoint, device, direction in [('pcm0p', '0', 'PLAYBACK'), ('pcm1c', '1', 'CAPTURE')]:
        sub = card / endpoint / 'sub1'
        info = {k.strip(): v.strip() for k, v in pcm_fields(sub / 'info').items()}
        if any(info.get(k) != v for k, v in dict(card=str(index), device=device,
                                                subdevice='1', stream=direction).items()):
            raise ValueError('Incorrect Loopback endpoint')
        (sub / 'status').read_text()
    return card, index


def probe_pcm(index, device, capture):
    """Negotiate on a closed hardware endpoint, in a bounded helper process."""
    try:
        result = subprocess.run([sys.executable, __file__, '--probe-loopback', str(index),
                                 str(device), str(int(capture))], capture_output=True, timeout=2)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def negotiate_pcm(index, device, capture):
    # No conversion, playback writes, or capture reads. Close releases the parameters.
    alsa = ctypes.CDLL('libasound.so.2')
    pcm, params = ctypes.c_void_p(), ctypes.c_void_p()
    def checked(name, *args):
        if getattr(alsa, name)(*args) < 0:
            raise RuntimeError('ALSA negotiation failed')
    checked('snd_pcm_open', ctypes.byref(pcm), f'hw:{index},{device},1'.encode(), capture, 1)
    try:
        checked('snd_pcm_hw_params_malloc', ctypes.byref(params))
        checked('snd_pcm_hw_params_any', pcm, params)
        checked('snd_pcm_hw_params_set_access', pcm, params, 3)  # RW_INTERLEAVED
        fmt = alsa.snd_pcm_format_value(b'S32_LE')
        checked('snd_pcm_hw_params_set_format', pcm, params, fmt)
        checked('snd_pcm_hw_params_set_channels', pcm, params, 2)
        checked('snd_pcm_hw_params_set_rate', pcm, params, 44100, 0)
        checked('snd_pcm_hw_params', pcm, params)
        endpoint = f'pcm{device}{"c" if capture else "p"}'
        if not pcm_params_match(Path('/proc/asound') / f'card{index}' / endpoint / 'sub1/hw_params'):
            raise RuntimeError('ALSA negotiated unexpected parameters')
    finally:
        if params:
            alsa.snd_pcm_hw_params_free(params)
        alsa.snd_pcm_close(pcm)


def loopback_available(proc=Path('/proc/asound')):
    try:
        card, index = loopback_card(proc)
        for endpoint, device, capture in [('pcm0p', 0, False), ('pcm1c', 1, True)]:
            sub = card / endpoint / 'sub1'
            if (sub / 'status').read_text().strip() == 'closed':
                if not probe_pcm(index, device, capture):
                    return False
            elif not pcm_params_match(sub / 'hw_params'):
                return False
        return True
    except (OSError, ValueError):
        return False


def discover_sources(proc=Path('/proc/asound')):
    """Advertise music routes and real USB capture endpoints, never HDMI outputs."""
    sources = [dict(TIDAL)]
    if loopback_available(proc):
        sources.append(dict(AIRPLAY))
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


SOURCE_ID = re.compile(r'(airplay|usb:([A-Za-z0-9_]+):(?:pair|mono)-[0-9]{1,2})')
MAX_METADATA = 4096


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError('Duplicate key in the source configuration')
        result[key] = value
    return result


def _is_source_stage(stage):
    return isinstance(stage, dict) and stage.get('type') == 'Mixer' and stage.get('name') == SOURCE_MIXER


def _number(value):
    return type(value) in (int, float)


CLOCK_TYPES = dict(resampler=lambda v: v is None or isinstance(v, dict),
                   enable_rate_adjust=lambda v: v is None or isinstance(v, bool),
                   capture_samplerate=lambda v: v is None or _number(v),
                   target_level=lambda v: v is None or _number(v),
                   adjust_period=lambda v: v is None or _number(v))


def _parse_metadata(description):
    """Strict, bounded parse of Octet's own restore record; nothing else is trusted."""
    if (not isinstance(description, str) or not description.startswith(SOURCE_DESCRIPTION)
            or len(description) > MAX_METADATA):
        raise RuntimeError('The active source configuration is not recognised')
    try:
        value = json.loads(description[len(SOURCE_DESCRIPTION):], object_pairs_hook=_unique)
    except ValueError as error:
        raise RuntimeError('The active source configuration is not recognised') from error
    if (not isinstance(value, dict)
            or set(value) != {'id', 'label', 'tidal_capture', 'tidal_clock'}
            or not isinstance(value['id'], str) or not SOURCE_ID.fullmatch(value['id'])
            or not isinstance(value['label'], str) or not 0 < len(value['label']) <= 200):
        raise RuntimeError('The active source configuration is not recognised')
    capture, clock = value['tidal_capture'], value['tidal_clock']
    if (not isinstance(capture, dict) or capture.get('type') != 'Alsa'
            or capture.get('device') != 'hw:Loopback,1,0' or capture.get('channels') != 2
            or type(capture.get('channels')) is not int):
        raise RuntimeError('Source restore metadata is missing; restore the managed DSP preset')
    if not isinstance(clock, dict) or set(clock) != set(CLOCK_KEYS):
        raise RuntimeError('Source restore metadata is missing; restore the managed DSP preset')
    for key, saved in clock.items():
        if (not isinstance(saved, dict) or set(saved) != {'present', 'value'}
                or type(saved['present']) is not bool or not CLOCK_TYPES[key](saved['value'])
                or (not saved['present'] and saved['value'] is not None)):
            raise RuntimeError('Source restore metadata is invalid')
    return value


def _check_source_mixer(config, metadata):
    """The one input stage must be exactly what source_config() builds, loaded defaults aside."""
    mixer = config['mixers'][SOURCE_MIXER]
    capture = config.get('devices', {}).get('capture', {})
    channels = mixer.get('channels') if isinstance(mixer, dict) else None
    count = channels.get('in') if isinstance(channels, dict) else None
    mapping = mixer.get('mapping') if isinstance(mixer, dict) else None
    if (not isinstance(channels, dict) or set(channels) != {'in', 'out'} or channels['out'] != 2
            or type(count) is not int or not 1 <= count <= 64
            or not isinstance(mapping, list) or len(mapping) != 2):
        raise RuntimeError('The active source mixer is not recognised')
    for dest, row in enumerate(mapping):
        if not isinstance(row, dict):
            raise RuntimeError('The active source mixer is not recognised')
        sources = row.get('sources')
        if (set(row) - {'dest', 'sources', 'mute'} or row.get('mute') not in (None, False)
                or row.get('dest') != dest or type(row.get('dest')) is not int
                or not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict)):
            raise RuntimeError('The active source mixer is not recognised')
        entry = sources[0]
        if (set(entry) - {'channel', 'gain', 'inverted', 'mute', 'scale'}
                or type(entry.get('channel')) is not int or not 0 <= entry['channel'] < count
                or not _number(entry.get('gain')) or entry['gain'] != 0
                or entry.get('inverted') not in (None, False) or entry.get('mute') not in (None, False)
                or entry.get('scale') not in (None, 'dB')):
            raise RuntimeError('The active source mixer is not recognised')
    identifier = metadata['id']
    if identifier == 'airplay':
        consistent = (capture.get('device') == 'hw:Loopback,1,1' and capture.get('channels') == 2
                      and config['devices'].get('capture_samplerate') == 44100 and count == 2)
    else:
        consistent = (capture.get('device') == f'hw:CARD={SOURCE_ID.fullmatch(identifier)[2]},DEV=0'
                      and capture.get('channels') == count)
    if capture.get('type') != 'Alsa' or not consistent:
        raise RuntimeError('The active capture device does not match the selected source')


def _pcm_text(path):
    try:
        raw = path.read_text()
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError('The USB capture substream could not be read') from error
    if len(raw) > 8192:
        raise RuntimeError('Oversized PCM observation')
    return raw


def _pcm_fields(raw):
    result = {}
    for line in raw.splitlines():
        if not line.strip() or line == '-----':
            continue
        key, separator, value = line.partition(':')
        key, value = key.strip(), value.strip()
        if not separator or not key or not value or key in result:
            raise RuntimeError('Invalid or duplicate PCM field')
        result[key] = value
    return result


def _capture_sample(sub, processes):
    raw = _pcm_text(sub / 'status')
    if raw.strip() == 'closed':
        raise RuntimeError('The USB capture substream is not open')
    fields = _pcm_fields(raw)
    if fields.get('state') != 'RUNNING':
        raise RuntimeError('The USB capture substream is not RUNNING')
    try:
        owner, hw_ptr, appl_ptr = (int(fields[key]) for key in ('owner_pid', 'hw_ptr', 'appl_ptr'))
    except (KeyError, ValueError) as error:
        raise RuntimeError('Invalid PCM status') from error
    if owner <= 0 or hw_ptr < 0 or appl_ptr < 0:
        raise RuntimeError('Invalid PCM status')
    # The ALSA owner is a task (thread); its thread group must be the CamillaDSP process.
    try:
        group = [line.partition(':')[2].strip() for line in (processes / str(owner) / 'status').read_text().splitlines()
                 if line.startswith('Tgid:')]
        name = (processes / str(int(group[0])) / 'comm').read_text().strip() if len(group) == 1 else None
    except (OSError, ValueError, IndexError):
        name = None
    if name != 'camilladsp':
        raise RuntimeError('The USB capture substream is not owned by CamillaDSP')
    return owner, hw_ptr, appl_ptr


def usb_capture_ready(source, rate, *, proc=Path('/proc/asound'), processes=Path('/proc'),
                      interval=.05, sleep=time.sleep):
    """Read-only proof that the selected USB capture is running under CamillaDSP and advancing.

    Never opens the (occupied) PCM and does not require signal. The card is found by the
    discovered ALSA ID and must resolve inside proc to a cardN whose id matches; the
    exact hw: device (no plughw), native S32_LE, channel count and rate must match the
    selection; the substream must be RUNNING, owned by a CamillaDSP thread, with both
    pointers advancing across a bounded interval. Raises ValueError for a source that is
    not a USB capture and RuntimeError, with a specific reason, for anything unready.
    """
    validate_source(source)
    match = re.fullmatch(r'usb:([A-Za-z0-9_]+):.+', source['id'])
    if match is None:
        raise ValueError('Capture readiness applies only to USB inputs')
    card_id = match[1]
    try:
        card = (proc / card_id).resolve(strict=True)
        identity = (card / 'id').read_text().strip()
    except OSError as error:
        raise RuntimeError('The USB interface is not connected') from error
    if card.parent != proc.resolve() or not re.fullmatch(r'card[0-9]+', card.name) or identity != card_id:
        raise RuntimeError('USB card identity does not match the selected input')
    sub = card / 'pcm0c' / 'sub0'
    first = _capture_sample(sub, processes)
    params = _pcm_fields(_pcm_text(sub / 'hw_params'))
    actual_rate = re.fullmatch(r'([0-9]+)(?: \(([0-9]+)/1\))?', params.get('rate', ''))
    if (params.get('format') != 'S32_LE' or params.get('channels') != str(source['channels'])
            or actual_rate is None or int(actual_rate[1]) != rate
            or (actual_rate[2] is not None and int(actual_rate[2]) != rate)):
        raise RuntimeError('USB capture format, channels or rate differ from the selected input')
    sleep(interval)
    second = _capture_sample(sub, processes)
    if second[0] != first[0]:
        raise RuntimeError('The USB capture substream changed owner')
    if second[1] <= first[1] or second[2] <= first[2]:
        raise RuntimeError('The USB capture substream stalled')


def source_metadata(config):
    pipeline = config.get('pipeline', [])
    mixers = config.get('mixers', {})
    stages = [index for index, stage in enumerate(pipeline) if _is_source_stage(stage)]
    if not stages:
        if SOURCE_MIXER in mixers:
            raise RuntimeError('The active source configuration is ambiguous')
        device = config.get('devices', {}).get('capture', {}).get('device', '')
        if device == 'hw:Loopback,1,1':
            return dict(id='airplay', label='AirPlay')
        return dict(id='tidal', label='Tidal') if device == 'hw:Loopback,1,0' else dict(id='unknown', label='Other source')
    if len(stages) != 1 or stages[0] != 0 or not isinstance(mixers.get(SOURCE_MIXER), dict):
        raise RuntimeError('The active source configuration is ambiguous')
    metadata = _parse_metadata(mixers[SOURCE_MIXER].get('description'))
    _check_source_mixer(config, metadata)
    return metadata


def validate_source(source):
    """Reject a malformed source before it can shape a DSP configuration."""
    if not isinstance(source, dict) or not isinstance(source.get('id'), str):
        raise ValueError('Choose an audio source')
    if source['id'] == 'tidal':
        return
    match = SOURCE_ID.fullmatch(source['id'])
    device, count, mapping, rates = (source.get(key) for key in ('device', 'channels', 'mapping', 'rates'))
    if (match is None or not isinstance(source.get('label'), str) or type(count) is not int
            or not 1 <= count <= 64 or not isinstance(mapping, list) or len(mapping) != 2
            or any(type(channel) is not int or not 0 <= channel < count for channel in mapping)
            or not isinstance(rates, list) or not rates
            or any(type(rate) is not int or rate <= 0 for rate in rates)):
        raise ValueError('Invalid audio source description')
    expected = 'hw:Loopback,1,1' if source['id'] == 'airplay' else f'hw:CARD={match[2]},DEV=0'
    if device != expected or (source['id'] == 'airplay' and (count != 2 or rates != [44100])):
        raise ValueError('Invalid audio source description')


def require_tidal_baseline(config):
    """Only the approved stereo Tidal loopback feeding an explicit eight-output DAC."""
    devices = config.get('devices') if isinstance(config, dict) else None
    if not isinstance(devices, dict) or source_metadata(config)['id'] != 'tidal':
        raise RuntimeError('Source routing requires the stereo Tidal baseline to be loaded')
    capture, playback = devices.get('capture'), devices.get('playback')
    if (not isinstance(capture, dict) or capture.get('type') != 'Alsa'
            or capture.get('device') != 'hw:Loopback,1,0' or capture.get('channels') != 2
            or type(devices.get('samplerate')) is not int or devices['samplerate'] != 48000
            or not isinstance(playback, dict) or playback.get('type') != 'Alsa'
            or playback.get('channels') != 8
            or not re.fullmatch(r'hw:[A-Za-z0-9_=,]+', str(playback.get('device', '')))
            or 'Loopback' in playback['device']):
        raise RuntimeError('Source routing requires a stereo Tidal baseline with an explicit eight-output DAC')


def meter_channels(config):
    """The visualizer follows the selected input, including duplicated mono."""
    if source_metadata(config)['id'] in ('tidal', 'airplay', 'unknown'):
        return (0, 1)
    mapping = config['mixers'][SOURCE_MIXER]['mapping']
    return tuple(row['sources'][0]['channel'] for row in sorted(mapping, key=lambda row: row['dest']))


def _output_subtree(config):
    """Everything downstream of the input: pipeline, filters, mixers, outputs and metadata."""
    result = copy.deepcopy(config)
    result['pipeline'] = [stage for stage in result.get('pipeline', []) if not _is_source_stage(stage)]
    if isinstance(result.get('mixers'), dict):
        result['mixers'].pop(SOURCE_MIXER, None)
    devices = result.get('devices')
    if isinstance(devices, dict):
        for key in ('capture',) + CLOCK_KEYS:
            devices.pop(key, None)
    return result


def _contains(actual, expected):
    """CamillaDSP adds parsed defaults; every requested value must still be present."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _contains(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_contains(a, b) for a, b in zip(actual, expected)))
    return actual == expected


def verify_output_equivalence(original, candidate, source=None):
    """Prove a source route differs from its approved original only on the input side.

    The speaker-side subtree must be exactly equal. Input-side fields may carry
    CamillaDSP's parsed defaults, but must contain every expected value. With a
    source the whole candidate is checked against an independently built route;
    without one, the route's own restore record must be truthful. Raises RuntimeError.
    """
    if source_metadata(original)['id'] != 'tidal':
        raise RuntimeError('The approved original must use the Tidal capture')
    if _output_subtree(candidate) != _output_subtree(original):
        raise RuntimeError('The source route changed a speaker output setting')
    metadata = source_metadata(candidate)
    devices, base = candidate['devices'], original['devices']
    if source is None:
        if metadata['id'] == 'tidal':
            expected = original
        else:
            if (metadata['tidal_capture'] != base['capture'] or metadata['tidal_clock']
                    != {key: dict(present=key in base, value=base.get(key)) for key in CLOCK_KEYS}):
                raise RuntimeError('The source route does not record how to restore the original')
            return
    else:
        if metadata['id'] != source['id']:
            raise RuntimeError('The loaded source is not the requested source')
        expected = source_config(original, source)
    if source['id'] == 'tidal' if source else metadata['id'] == 'tidal':
        if (not _contains(devices['capture'], base['capture'])
                or any(devices.get(key) != base.get(key) for key in CLOCK_KEYS)):
            raise RuntimeError('The Tidal route does not match the approved original')
        return
    if (not _contains(devices['capture'], expected['devices']['capture'])
            or any(not _contains(devices.get(key), expected['devices'][key]) for key in CLOCK_KEYS
                   if key in expected['devices'])
            or not _contains(candidate['mixers'][SOURCE_MIXER], expected['mixers'][SOURCE_MIXER])
            or candidate['pipeline'][0] != expected['pipeline'][0]):
        raise RuntimeError('The source route does not match the requested input')


def source_config(config, source):
    """Prepend a capture-to-stereo mixer; leave every speaker setting intact."""
    validate_source(source)
    result = _source_config(config, source)
    if _output_subtree(result) != _output_subtree(config):
        raise RuntimeError('The source route changed a speaker output setting')
    return result


def _source_config(config, source):
    result = copy.deepcopy(config)
    metadata = source_metadata(result)
    devices = result['devices']
    if metadata['id'] == 'unknown':
        raise RuntimeError('The current capture device is not managed by the music page')
    if metadata['id'] != 'tidal':
        if 'tidal_capture' not in metadata or 'tidal_clock' not in metadata:
            raise RuntimeError('Source restore metadata is missing; restore the managed DSP preset')
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
    if source['id'] == 'airplay' and devices['samplerate'] != 48000:
        raise ValueError('AirPlay requires the approved 48000 Hz speaker pipeline')
    rate = 44100 if source['id'] == 'airplay' else devices['samplerate']
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


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--probe-loopback':
        sys.exit(1)
    try:
        negotiate_pcm(*(int(value) for value in sys.argv[2:]))
    except Exception:
        sys.exit(1)
