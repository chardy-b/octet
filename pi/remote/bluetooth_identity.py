"""BlueZ device identity. Every phone paired with hci0 may play; there is no allowlist.

paired_addresses() is the list of phones that may start Bluetooth playback: the
Device1 objects on hci0 that BlueZ reports as Paired. Pairing itself is plain BlueZ
"Just Works" with no agent (see bluetooth_pairing). observe_identity() re-reads one
device and requires Paired, Trusted and Connected before and during playback.
"""
import json
import os
import subprocess

from bluetooth_contract import bridge_arguments

ADAPTER = '/org/bluez/hci0'
DEVICE_IFACE = 'org.bluez.Device1'


def busctl(*args):
    return subprocess.run(['/usr/bin/busctl', '--system', '--no-pager', '--timeout=2s', *args],
                          capture_output=True, timeout=3, shell=False)


def devices():
    """Every BlueZ device on hci0 as dicts of address, name, paired, trusted, connected."""
    result = busctl('--json=short', 'call', 'org.bluez', '/',
                    'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects')
    if result.returncode != 0:
        raise RuntimeError('BlueZ is unavailable')
    objects = json.loads(result.stdout)['data'][0]
    found = []
    for interfaces in objects.values():
        device = interfaces.get(DEVICE_IFACE)
        if not device or device.get('Adapter', {}).get('data') != ADAPTER:
            continue
        value = lambda name, default=None: device.get(name, {}).get('data', default)
        found.append(dict(address=value('Address'), name=value('Alias') or value('Name') or '',
                          paired=value('Paired') is True, trusted=value('Trusted') is True,
                          connected=value('Connected') is True))
    return found


def paired_addresses():
    """The phones allowed to play: everything paired with hci0."""
    addresses = tuple(sorted(item['address'] for item in devices() if item['paired']))
    if not addresses:
        raise RuntimeError('No paired Bluetooth phone')
    return addresses


def verify_identity(address, allowlist):
    """Read exact Device1 identity twice; observations alone never enable start.

    Connected is necessary but never substitutes for Paired AND Trusted. Repeated
    reads detect ordinary disconnect/replacement races, not an atomic bus lease.
    """
    if os.geteuid() != 0:
        raise RuntimeError('Bluetooth identity check requires local administrator')
    return observe_identity(address, allowlist)


def observe_identity(address, allowlist):
    """Same exact reads for the unprivileged service; reading Device1 grants nothing."""
    address = bridge_arguments(address, allowlist)[-1]
    path = '/org/bluez/hci0/dev_' + address.replace(':', '_')
    expected = {'Address': f's "{address}"\n'.encode(),
                'Adapter': b'o "/org/bluez/hci0"\n',
                'Paired': b'b true\n', 'Trusted': b'b true\n',
                'Connected': b'b true\n'}
    for _ in range(2):
        for name, value in expected.items():
            result = busctl('get-property', 'org.bluez', path, DEVICE_IFACE, name)
            if result.returncode != 0 or result.stderr != b'' or result.stdout != value:
                raise RuntimeError('Bluetooth identity unavailable or rejected: ' + name)
    return address
