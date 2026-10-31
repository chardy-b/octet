"""Plain BlueZ pairing for Octet: Just Works with no agent, controlled from the web page.

With no agent registered, bluetoothd gives the kernel the NoInputNoOutput IO capability,
so the kernel accepts a phone's "Just Works" pairing by itself; there is no passkey.
BlueZ still asks an agent before it accepts audio from a paired but untrusted phone, so
trust_paired() marks every paired device trusted. Paired phones can always reconnect;
discoverable only controls whether new phones can find and pair with Octet.
"""
from bluetooth_identity import ADAPTER, busctl, devices

ADAPTER_IFACE = 'org.bluez.Adapter1'


def _set(path, interface, name, kind, value):
    result = busctl('set-property', 'org.bluez', path, interface, name, kind, value)
    if result.returncode != 0:
        raise RuntimeError(f'BlueZ refused {name}: {result.stderr.decode(errors="replace").strip()}')


def _get(name):
    result = busctl('get-property', 'org.bluez', ADAPTER, ADAPTER_IFACE, name)
    if result.returncode != 0:
        raise RuntimeError('BlueZ is unavailable')
    return result.stdout.strip()


def trust_paired():
    """Trust every paired device so its audio connection is accepted without an agent."""
    trusted = []
    for item in devices():
        if item['paired'] and not item['trusted']:
            path = ADAPTER + '/dev_' + item['address'].replace(':', '_')
            _set(path, 'org.bluez.Device1', 'Trusted', 'b', 'true')
            trusted.append(item['address'])
    return trusted


def discoverable():
    return _get('Discoverable') == b'b true' and _get('Pairable') == b'b true'


def set_discoverable(enabled):
    """Open (with no timeout) or close pairing. Connections from paired phones are unaffected."""
    value = 'true' if enabled else 'false'
    if enabled:
        for name in ('PairableTimeout', 'DiscoverableTimeout'):
            _set(ADAPTER, ADAPTER_IFACE, name, 'u', '0')
    for name in ('Pairable', 'Discoverable'):
        _set(ADAPTER, ADAPTER_IFACE, name, 'b', value)
    if discoverable() != enabled:
        raise RuntimeError('BlueZ did not apply the discoverable setting')
    return enabled
