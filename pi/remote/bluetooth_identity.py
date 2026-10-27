"""Read-only local-admin policy/BlueZ prerequisite. Does not authorize playback.

No server endpoint, pairing, trust mutation, connect, bridge spawn or service IO.
The separate runtime activation gates are intentionally still absent.
"""
import json
import os
from pathlib import Path
import stat
import subprocess

from bluetooth_contract import bridge_arguments

POLICY = Path('/etc/octet/bluetooth.json')


def local_policy(path=POLICY):
    if os.geteuid() != 0:
        raise RuntimeError('Bluetooth requires an explicit local administrator')
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise RuntimeError('Bluetooth policy must be a canonical absolute path')
    # Every ancestor is controlled by root; no symlink or writable parent escape.
    for parent in path.parents:
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Bluetooth policy directory is not protected')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError('Bluetooth policy requires root-owned mode 0600')
        raw = stream.read(4097)
        after = path.lstat()
        if (len(raw) > 4096 or (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
                != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
            raise RuntimeError('Bluetooth policy changed or oversized')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError('Duplicate Bluetooth policy key')
            result[key] = value
        return result
    policy = json.loads(raw, object_pairs_hook=unique)
    if (not isinstance(policy, dict) or set(policy) != {'version', 'enabled', 'addresses'}
            or type(policy['version']) is not int or policy['version'] != 1
            or policy['enabled'] is not True):
        raise RuntimeError('Bluetooth local policy is disabled or invalid')
    addresses = policy['addresses']
    if not isinstance(addresses, list) or not addresses or len(addresses) > 16:
        raise RuntimeError('Bluetooth requires an explicit bounded allowlist')
    for address in addresses:
        if bridge_arguments(address, addresses)[-1] != address:
            raise RuntimeError('Bluetooth allowlist must contain canonical MAC addresses')
    if len(set(addresses)) != len(addresses):
        raise RuntimeError('Duplicate Bluetooth address')
    return tuple(addresses)


def verify_identity(address, allowlist):
    """Read exact Device1 identity twice; observations alone never enable start.

    Connected is necessary but never substitutes for Paired AND Trusted. Repeated
    reads detect ordinary disconnect/replacement races, not an atomic bus lease.
    A future supervisor must keep watching identity and the BlueZ bus owner.
    """
    if os.geteuid() != 0:
        raise RuntimeError('Bluetooth identity check requires local administrator')
    address = bridge_arguments(address, allowlist)[-1]
    path = '/org/bluez/hci0/dev_' + address.replace(':', '_')
    expected = {'Address': f's "{address}"\n'.encode(),
                'Adapter': b'o "/org/bluez/hci0"\n',
                'Paired': b'b true\n', 'Trusted': b'b true\n',
                'Connected': b'b true\n'}
    for _ in range(2):
        for name, value in expected.items():
            result = subprocess.run(
                ['/usr/bin/busctl', '--system', '--no-pager', '--timeout=2s',
                 'get-property', 'org.bluez', path, 'org.bluez.Device1', name],
                capture_output=True, timeout=3, shell=False)
            if result.returncode != 0 or result.stderr != b'' or result.stdout != value:
                raise RuntimeError('Bluetooth identity unavailable or rejected: ' + name)
    return address
