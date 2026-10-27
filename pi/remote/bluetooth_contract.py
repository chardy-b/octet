"""Offline WIL-249 contracts. Nothing here starts audio or authorizes activation.

Observed PCM parameters and approval must eventually come from a trusted runtime
controller. These pure helpers deliberately cannot establish either fact.
"""
import copy
import re

from bluetooth_preflight import CAPTURE, PLAYBACK


def check_cli(version, help_text, *, player):
    """Accept only the reviewed 4.3.1 interface; help is not runtime readiness."""
    if version.strip() != '4.3.1':
        raise ValueError('Only BlueALSA 4.3.1 is reviewed')
    required = ('--profile-a2dp', '--pcm=NAME', '--volume=TYPE', '--single-audio') if player else ('--profile=NAME', 'a2dp-sink')
    for token in required:
        if not re.search(r'(?<![\w-])' + re.escape(token) + r'(?![\w=-])', help_text):
            raise ValueError('Missing reviewed BlueALSA CLI capability: ' + token)
    if player and not re.search(r'(?<![\w-])software(?![\w-])', help_text):
        raise ValueError('Software volume capability missing')


def bridge_arguments(address, allowlist):
    """Preview argv only. Paired/trusted status is a separate, unimplemented gate."""
    def canonical(value):
        if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', value):
            raise ValueError('Explicit Bluetooth address required')
        value = value.upper()
        if value in ('00:00:00:00:00:00', 'FF:FF:FF:FF:FF:FF'):
            raise ValueError('Wildcard Bluetooth addresses are forbidden')
        return value
    if not isinstance(allowlist, (list, tuple)) or not allowlist:
        raise ValueError('Explicit nonempty address allowlist required')
    allowed = [canonical(value) for value in allowlist]
    address = canonical(address)
    if address not in allowed:
        raise ValueError('Phone is not allowlisted')
    return ['bluealsa-aplay', '--profile-a2dp', '--pcm=' + PLAYBACK,
            '--volume=software', '--single-audio', address]


def routed(config, *, rate, sample_format, channels):
    """Build an offline candidate from actual post-bridge PCM parameters.

    No ALSA plug conversion. Unsupported native streams must be rejected. The
    future controller must verify approval/path/digest and run Camilla --check.
    """
    if (type(rate) is not int or rate not in (44100, 48000)
            or sample_format not in ('S16_LE', 'S32_LE')
            or type(channels) is not int or channels != 2):
        raise ValueError('Unsupported Bluetooth PCM parameters')
    devices = config['devices']
    capture, playback = devices['capture'], devices['playback']
    if (capture.get('type') != 'Alsa' or capture.get('device') != 'hw:Loopback,1,0'
            or capture.get('channels') != 2 or devices.get('samplerate') != 48000
            or playback.get('type') != 'Alsa' or playback.get('channels') != 8
            or not re.fullmatch(r'hw:[A-Za-z0-9_=,]+', playback.get('device', ''))
            or 'Loopback' in playback['device']):
        raise ValueError('Requires stereo Tidal baseline with explicit eight-output DAC')
    result = copy.deepcopy(config)
    result['devices']['capture'].update(device=CAPTURE, format=sample_format)
    result['devices'].update(capture_samplerate=rate,
                             resampler=dict(type='AsyncSinc', profile='Balanced'),
                             enable_rate_adjust=True, target_level=devices['chunksize'],
                             adjust_period=10)
    return result
