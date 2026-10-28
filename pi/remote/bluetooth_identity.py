"""Read-only local-admin policy/BlueZ identity. Never sufficient to start playback.

Authority comes from a root-owned file in root-protected directories. There is
exactly ONE file contract: root:octet-bluetooth mode 0640 in root-owned,
non-writable, world-searchable ancestors. The admin CLI (as root) and the
unprivileged octet-remote service (through the octet-bluetooth group) apply the
same check, so a policy the admin tool accepts for trust is one the service can
read at start. There is deliberately no root-only 0600 variant: it would let an
operator pair and trust a phone that octet-remote can then never start.

pairing_policy() accepts the same file with enabled false and/or an empty
allowlist; it only gates the bounded pairing window and grants no playback.
No pairing, trust mutation or connect happens here.

admission_policy() is the typed read of the same file. version 1 (enabled,
addresses) is exactly and permanently 'restricted'. version 2 names the mode
('restricted' or 'open') explicitly; open is never implied, never a wildcard MAC,
and is not honored by the manual local/service/pairing readers, which refuse it
until a reviewed automatic observer exists. That observer must still verify a
paired+trusted+connected Device1 identity.
"""
import grp
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import NamedTuple

from bluetooth_contract import bridge_arguments

POLICY = Path('/etc/octet/bluetooth.json')
GROUP = 'octet-bluetooth'
MODES = ('restricted', 'open')


class AdmissionPolicy(NamedTuple):
    version: int
    mode: str
    enabled: bool
    addresses: tuple


def _service_mode(info):
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError as error:
        raise RuntimeError('Bluetooth service group is not installed') from error
    if info.st_gid != gid:
        raise RuntimeError(f'Bluetooth policy group must be {GROUP}; fix with '
                           f'chown root:{GROUP} {POLICY} && chmod 0640 {POLICY}')
    return stat.S_IMODE(info.st_mode) == 0o640


def _require_admin():
    if os.geteuid() != 0:
        raise RuntimeError('Bluetooth requires an explicit local administrator')


def admission_policy(path=POLICY):
    """Typed, fail-closed read of the protected policy file. Grants nothing by itself."""
    return _load(path)


def local_policy(path=POLICY):
    """Root read of the enabled allowlist under the service's exact file contract."""
    _require_admin()
    return _authorized(*_manual(path))


def service_policy(path=POLICY):
    """Unprivileged service read. File ownership, not reader euid, is the authority."""
    return _authorized(*_manual(path))


def pairing_policy(path=POLICY):
    """Root-only gate for the bounded pairing window; returns (enabled, addresses).

    A disabled policy and an empty allowlist are accepted, because a new phone's
    MAC is unknown until it has paired. Nothing here authorizes playback: trust
    and start still require local_policy()/service_policy().
    """
    _require_admin()
    return _manual(path)


def _manual(path):
    """(enabled, addresses) for the manual flow; open is not authorized here."""
    policy = _load(path)
    if policy.mode != 'restricted':
        raise RuntimeError('Bluetooth open admission is not authorized by the manual policy flow')
    return policy.enabled, policy.addresses


def _authorized(enabled, addresses):
    if enabled is not True:
        raise RuntimeError('Bluetooth local policy is disabled or invalid')
    if not addresses:
        raise RuntimeError('Bluetooth requires an explicit bounded allowlist')
    return addresses


def _load(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise RuntimeError('Bluetooth policy must be a canonical absolute path')
    # Every ancestor is controlled by root; no symlink or writable parent escape.
    # World-search is required so the octet-remote service user can reach the file.
    for parent in path.parents:
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not info.st_mode & stat.S_IXOTH):
            raise RuntimeError('Bluetooth policy directory is not protected and searchable')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or not _service_mode(info):
            raise RuntimeError(f'Bluetooth policy requires root-owned mode 0640 with group '
                               f'{GROUP}; fix with chown root:{GROUP} {POLICY} && '
                               f'chmod 0640 {POLICY}')
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
    if not isinstance(policy, dict) or type(policy.get('version')) is not int:
        raise RuntimeError('Bluetooth local policy is disabled or invalid')
    version = policy['version']
    if version == 1:
        keys, mode = {'version', 'enabled', 'addresses'}, 'restricted'
    elif version == 2:
        keys, mode = {'version', 'mode', 'enabled', 'addresses'}, policy.get('mode')
        if type(mode) is not str or mode not in MODES:
            raise RuntimeError('Bluetooth policy mode must be restricted or open')
    else:
        raise RuntimeError('Bluetooth local policy is disabled or invalid')
    if set(policy) != keys or type(policy['enabled']) is not bool:
        raise RuntimeError('Bluetooth local policy is disabled or invalid')
    addresses = policy['addresses']
    if not isinstance(addresses, list) or len(addresses) > 16:
        raise RuntimeError('Bluetooth requires an explicit bounded allowlist')
    for address in addresses:
        if bridge_arguments(address, addresses)[-1] != address:
            raise RuntimeError('Bluetooth allowlist must contain canonical MAC addresses')
    if len(set(addresses)) != len(addresses):
        raise RuntimeError('Duplicate Bluetooth address')
    return AdmissionPolicy(version, mode, policy['enabled'], tuple(addresses))


def verify_identity(address, allowlist):
    """Read exact Device1 identity twice; observations alone never enable start.

    Connected is necessary but never substitutes for Paired AND Trusted. Repeated
    reads detect ordinary disconnect/replacement races, not an atomic bus lease.
    A future supervisor must keep watching identity and the BlueZ bus owner.
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
            result = subprocess.run(
                ['/usr/bin/busctl', '--system', '--no-pager', '--timeout=2s',
                 'get-property', 'org.bluez', path, 'org.bluez.Device1', name],
                capture_output=True, timeout=3, shell=False)
            if result.returncode != 0 or result.stderr != b'' or result.stdout != value:
                raise RuntimeError('Bluetooth identity unavailable or rejected: ' + name)
    return address
