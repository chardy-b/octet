#!/usr/bin/env python3
"""Guarded, disabled-by-default Bluetooth receiver installer. Dry run unless --apply.

Installs only a DISABLED root-owned policy (never overwriting an existing one), an
octet-remote drop-in providing the root-only control socket directory, and a
dedicated octet-bluealsa.service A2DP-sink daemon unit that has no [Install]
section, so it cannot be enabled and never starts at boot. It never installs
packages, edits packaged BlueALSA/BlueZ units, restarts octet-remote, enables,
starts, stops or masks any unit, makes the adapter discoverable or changes audio.

Unit enable/active state is recorded before any change and re-read afterwards;
any difference is reported as an error and never "fixed". Rollback restores the
managed files from backup and likewise only reports, never changes, unit state.
The drop-in takes effect at the next attended octet-remote maintenance restart.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from bluetooth_preflight import preflight
from install_airplay import atomic_write, restore_file, run, save_file

POLICY = Path('/etc/octet/bluetooth.json')
DROPIN = Path('/etc/systemd/system/octet-remote.service.d/bluetooth.conf')
UNIT = Path('/etc/systemd/system/octet-bluealsa.service')
BACKUP = Path('/var/backups/octet-bluetooth')
REMOTE = Path('/opt/octet-remote')
GROUP = 'octet-bluetooth'
ADMIN_UID = 0
DEFAULT = json.dumps(dict(version=1, enabled=False, addresses=[])).encode() + b'\n'
REQUIRED = ('server.py', 'bluetooth_control.py', 'bluetooth_route.py', 'bluetooth_bridge.py',
            'bluetooth_identity.py', 'bluetooth_contract.py', 'bluetooth_preflight.py',
            'bluetooth_admin.py', 'airplay_control.py')
# Observed, never changed. bluealsa.service is the packaged daemon, which must
# not own org.bluealsa alongside ours; the installer only reports its state.
UNITS = ('octet-remote.service', 'octet-bluealsa.service', 'bluealsa.service', 'bluetooth.service')


def managed():
    """Managed file -> backup name, resolved at call time (tests patch the paths)."""
    return {POLICY: 'policy.json', DROPIN: 'octet-remote-bluetooth.conf',
            UNIT: 'octet-bluealsa.service'}


def dropin():
    groups = [GROUP]
    try:
        grp.getgrnam('bluetooth')
        groups.append('bluetooth')  # Debian's BlueZ D-Bus read access group
    except KeyError:
        pass
    return ('[Service]\n'
            f'SupplementaryGroups={" ".join(groups)}\n'
            'RuntimeDirectory=octet-bluetooth\n'
            'RuntimeDirectoryMode=0700\n'
            'ExecStopPost=/usr/bin/python3 /opt/octet-remote/airplay_control.py mute-on-exit\n')


def daemon_path():
    # Same resolution order as the preflight that verified the 4.3.1 CLI.
    path = shutil.which('bluealsad') or shutil.which('bluealsa')
    if path is None or Path(path).resolve() != Path(path) or Path(path).parent != Path('/usr/bin'):
        raise RuntimeError('BlueALSA daemon must be a canonical /usr/bin executable')
    return path


def unit(daemon):
    # Requisite (not Requires): starting this never starts bluetooth.service.
    # No [Install]: `systemctl enable` is impossible, so nothing starts at boot.
    return ('# Installed by Octet install_bluetooth.py (WIL-249). Manual, attended use only.\n'
            '# Start deliberately: sudo systemctl start octet-bluealsa\n'
            '[Unit]\n'
            'Description=Octet BlueALSA A2DP sink daemon (manual, WIL-249)\n'
            'Requisite=bluetooth.service\n'
            'After=bluetooth.service dbus.service\n'
            '\n'
            '[Service]\n'
            'Type=dbus\n'
            'BusName=org.bluealsa\n'
            f'ExecStart={daemon} --profile=a2dp-sink\n'
            'Restart=no\n'
            'NoNewPrivileges=true\n'
            'PrivateTmp=true\n'
            'ProtectHome=true\n'
            'ProtectSystem=strict\n'
            'ReadWritePaths=-/var/lib/bluealsa\n'
            'RestrictAddressFamilies=AF_UNIX AF_BLUETOOTH\n')


def unit_state(name):
    enabled = run('systemctl', 'is-enabled', name, check=False).stdout.strip()
    active = run('systemctl', 'is-active', name, check=False).stdout.strip()
    return dict(enabled=enabled or 'not-found', active=active or 'unknown')


def states():
    return {name: unit_state(name) for name in UNITS}


def unchanged(before, after, *, expected_ours):
    """Report, never repair: any unit whose enable/active state moved."""
    problems = []
    for name in UNITS:
        if name == 'octet-bluealsa.service':
            if after[name] != expected_ours:
                problems.append(f'{name}: expected {expected_ours}, found {after[name]}')
        elif after[name] != before[name]:
            problems.append(f'{name}: {before[name]} -> {after[name]}')
    if problems:
        raise RuntimeError('Unit state changed unexpectedly; NOT altered by the installer, '
                           'inspect manually: ' + '; '.join(problems))


def check():
    if os.geteuid() != 0:
        raise RuntimeError('Run on the Pi as root')
    result = preflight()
    if not result['prerequisites_ok']:
        raise RuntimeError('Bluetooth prerequisites missing: ' + '; '.join(result['blockers']))
    missing = [name for name in REQUIRED if not (REMOTE / name).is_file()]
    if missing:
        raise RuntimeError('Upgrade Octet remote files first: ' + ', '.join(missing))
    directory = POLICY.parent
    if directory.exists() or directory.is_symlink():
        info = directory.lstat()
        # Same rule as bluetooth_identity: octet-remote must be able to reach the file.
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != ADMIN_UID
                or info.st_mode & 0o022 or not info.st_mode & stat.S_IXOTH):
            raise RuntimeError(f'Policy directory is not protected and searchable: {directory}')
    for path in (DROPIN, UNIT):
        if path.is_symlink():
            raise RuntimeError(f'Managed file must not be a symlink: {path}')
    return daemon_path()


def plan():
    return ['verify BlueALSA 4.3.1 binaries and idle Loopback sub2 endpoints',
            f'record enable/active state of {", ".join(UNITS)} (read only)',
            f'back up {POLICY}, {DROPIN} and {UNIT} (or their absence) in {BACKUP}',
            f'create system group {GROUP} if missing',
            f'create {POLICY} as disabled (root:{GROUP} 0640) only if absent',
            f'write {DROPIN}',
            f'write {UNIT} (A2DP sink only; no [Install], cannot be enabled)',
            'systemctl daemon-reload, then re-read unit state and report any change',
            'NOT enabled/started/stopped/masked: octet-remote, octet-bluealsa, bluealsa, '
            'bluetooth; adapter unchanged']


def install():
    daemon = check()
    before = states()
    if before['octet-bluealsa.service']['active'] not in ('inactive', 'unknown'):
        raise RuntimeError('octet-bluealsa is running; stop it deliberately before reinstalling')
    BACKUP.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = BACKUP / 'units.json'
    if not record.exists():
        atomic_write(record, json.dumps(before).encode(), 0o600)
    # Back up every managed file before the first system change (including groupadd).
    for path, saved in managed().items():
        save_file(path, BACKUP / saved)
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError:
        run('groupadd', '--system', GROUP)
        gid = grp.getgrnam(GROUP).gr_gid
    if not POLICY.parent.exists():
        POLICY.parent.mkdir(mode=0o755, parents=True)
    if not POLICY.exists() and not POLICY.is_symlink():
        atomic_write(POLICY, DEFAULT, 0o640, (ADMIN_UID, gid))
    else:
        info = POLICY.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != ADMIN_UID or info.st_gid != gid
                or stat.S_IMODE(info.st_mode) != 0o640):
            # Never overwritten; pairing, trust and start all refuse until fixed.
            print(f'Existing {POLICY} is not root:{GROUP} 0640 and was left unchanged; '
                  f'pairing, trust and start refuse until: chown root:{GROUP} {POLICY} '
                  f'&& chmod 0640 {POLICY}', file=sys.stderr)
    atomic_write(DROPIN, dropin().encode(), 0o644)
    atomic_write(UNIT, unit(daemon).encode(), 0o644)
    run('systemctl', 'daemon-reload')
    unchanged(before, states(), expected_ours=dict(enabled='static', active='inactive'))
    print('Bluetooth installed DISABLED; octet-bluealsa installed but not started. '
          'An attended octet-remote restart is required. Pair first (the disabled policy '
          'opens the pairing window), then edit the policy in place, keeping '
          f'root:{GROUP} 0640. Backup:', BACKUP)


def rollback():
    before = states()
    if before['octet-bluealsa.service']['active'] not in ('inactive', 'failed', 'unknown'):
        # Never stop it silently: removing a running unit's file strands it.
        raise RuntimeError('octet-bluealsa is running; stop it deliberately, then retry rollback')
    errors = []
    marker = BACKUP / 'policy.json.absent'
    try:
        if marker.exists() and POLICY.exists() and POLICY.read_bytes() == DEFAULT:
            POLICY.unlink()
        elif POLICY.exists():
            print(f'Retaining administrator-edited or pre-existing {POLICY}', file=sys.stderr)
    except OSError as error:
        errors.append(str(error))
    for path, saved in managed().items():
        if path == POLICY:
            continue
        try:
            restore_file(path, BACKUP / saved)
        except (OSError, RuntimeError) as error:
            errors.append(str(error))
    try:
        run('systemctl', 'daemon-reload')
        after = states()
        ours = before['octet-bluealsa.service']
        if not UNIT.exists():
            ours = dict(enabled='not-found', active=after['octet-bluealsa.service']['active'])
        unchanged(before, after, expected_ours=ours)
    except Exception as error:
        errors.append(str(error))
    if errors:
        raise RuntimeError('Rollback incomplete; backups retained for retry: ' + '; '.join(errors))
    if before['octet-remote.service']['active'] == 'active':
        print('octet-remote keeps its previous drop-in settings until its next attended '
              'restart; it was not restarted.', file=sys.stderr)
    recorded = BACKUP / 'units.json'
    if recorded.exists():
        installed = json.loads(recorded.read_text())
        drift = [name for name in UNITS if name != 'octet-bluealsa.service'
                 and installed.get(name) != before[name]]
        if drift:
            print('Unit state differs from install time (left unchanged): ' + ', '.join(drift),
                  file=sys.stderr)
    # Verified: consume the backup so a later install snapshots fresh state and a
    # second rollback can never revert edits made after this one.
    for saved in [*managed().values(), 'units.json']:
        for name in (saved, saved + '.absent'):
            (BACKUP / name).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback', action='store_true')
    args = parser.parse_args(argv)
    if args.rollback:
        if os.geteuid() != 0:
            raise RuntimeError('Run on the Pi as root')
        return rollback()
    if args.apply:
        os.umask(0o022)
        return install()
    steps = plan()
    print('Dry run; pass --apply to install:\n  ' + '\n  '.join(steps))
    return steps


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f'Octet Bluetooth installer: {error}')
