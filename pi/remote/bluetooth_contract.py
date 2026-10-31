"""Offline WIL-249 contracts. Nothing here starts audio or authorizes activation.

Observed PCM parameters and approval must eventually come from a trusted runtime
controller. These pure helpers deliberately cannot establish either fact.
"""
import re

from bluetooth_preflight import PLAYBACK
from dsp_inputs import with_input


def check_cli(version, help_text, *, player):
    """Accept only the reviewed 4.3.1 interface; help is not runtime readiness."""
    if version != 'v4.3.1\n':
        raise ValueError('BlueALSA version stdout must be exactly v4.3.1 followed by one newline')
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
    """The running profile fed from the phone's actual post-bridge PCM parameters.

    Any profile (see dsp_inputs). No ALSA plug conversion: unsupported native
    streams are rejected. The controller runs Camilla --check on the result.
    """
    if type(channels) is not int or channels != 2:
        raise ValueError('Unsupported Bluetooth PCM parameters')
    return with_input(config, 'bluetooth', rate=rate, sample_format=sample_format)
