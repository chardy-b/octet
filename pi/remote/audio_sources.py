"""Discover USB capture sources and feed an unchanged stereo speaker pipeline."""
import ctypes
import subprocess
import sys
from pathlib import Path
import re

from dsp_inputs import CLOCK_KEYS, SOURCE_DESCRIPTION, SOURCE_MIXER, input_of, with_input

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


def source_metadata(config):
    return input_of(config)


def meter_channels(config):
    """The visualizer follows the selected input, including duplicated mono."""
    if source_metadata(config)['id'] in ('tidal', 'airplay', 'bluetooth', 'unknown'):
        return (0, 1)
    mapping = config['mixers'][SOURCE_MIXER]['mapping']
    return tuple(row['sources'][0]['channel'] for row in sorted(mapping, key=lambda row: row['dest']))


def source_config(config, source):
    """The profile's speaker settings fed from source (see dsp_inputs.with_input)."""
    return with_input(config, source)


if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] != '--probe-loopback':
        sys.exit(1)
    try:
        negotiate_pcm(*(int(value) for value in sys.argv[2:]))
    except Exception:
        sys.exit(1)
