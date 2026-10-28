#!/usr/bin/env python3
"""Optional Pi-only AirPlay upgrade; requires already installed OS dependencies.

Never installs packages or rewrites the speaker preset. Run --rollback to restore
managed files and service enable/active states from the first installation.
"""
import argparse
import grp
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import time

import yaml
from audio_sources import AIRPLAY, loopback_available, source_config

BACKUP = Path('/var/backups/octet-airplay')
PROFILE = Path('/var/lib/octet-dsp/channel-dsp.yml')
FILES = {
    '/etc/shairport-sync.conf': 'receiver.conf',
    '/etc/systemd/system/shairport-sync.service.d/octet.conf': 'receiver.service',
    '/etc/systemd/system/octet-remote.service.d/airplay.conf': 'remote.service',
}
SERVICES = ('shairport-sync', 'avahi-daemon', 'octet-remote')
REMOTE_OVERRIDE = '''[Service]
Group=octet-airplay
SupplementaryGroups=audio upmpdcli
RuntimeDirectory=octet-airplay
RuntimeDirectoryMode=0750
ExecStopPost=/usr/bin/python3 /opt/octet-remote/airplay_control.py mute-on-exit
'''
RECEIVER_OVERRIDE = '''[Unit]
After=octet-remote.service avahi-daemon.service
Requires=octet-remote.service avahi-daemon.service
BindsTo=octet-remote.service
PartOf=octet-remote.service
[Service]
User=shairport-sync
Group=shairport-sync
SupplementaryGroups=audio octet-airplay
ExecStart=
ExecStart=/usr/bin/shairport-sync -c /etc/shairport-sync.conf
NoNewPrivileges=true
'''


def run(*args, check=True):
    return subprocess.run(list(args), check=check, capture_output=True, text=True, timeout=30)


def check_loopback(proc=Path('/proc/asound')):
    if not loopback_available(proc):
        raise RuntimeError('Dedicated Loopback playback 0,1 and capture 1,1 are required; no fallback')


def check_capabilities(version):
    if 'airplay2' in version.lower() or 'airplay 2' in version.lower():
        raise RuntimeError('This installer requires a classic-only Shairport build; AirPlay 2 builds need separate capability verification')
    if not all(cap.lower() in version.lower() for cap in ('ALSA', 'Avahi')):
        raise RuntimeError('Shairport Sync requires ALSA and Avahi support')


def atomic_write(target, data, mode=0o644, owner=None):
    """Publish complete bytes and metadata together, then sync the directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '-', dir=target.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def copy_atomic(source, target):
    stat = source.stat()
    atomic_write(target, source.read_bytes(), stat.st_mode & 0o7777, (stat.st_uid, stat.st_gid))


def verify_file(target, backup):
    if backup.exists():
        expected, actual = backup.stat(), target.stat()
        if (target.read_bytes() != backup.read_bytes() or
                (actual.st_mode & 0o7777, actual.st_uid, actual.st_gid) !=
                (expected.st_mode & 0o7777, expected.st_uid, expected.st_gid)):
            raise RuntimeError(f'File restoration verification failed: {target}')
    elif backup.with_suffix(backup.suffix + '.absent').exists():
        if target.exists() or target.is_symlink():
            raise RuntimeError(f'File removal verification failed: {target}')
    else:
        raise RuntimeError(f'Missing backup for {target}')


def save_file(target, backup):
    if backup.exists() or backup.with_suffix(backup.suffix + '.absent').exists():
        return
    if target.is_symlink():
        raise RuntimeError(f'Managed file must not be a symlink: {target}')
    if target.exists():
        copy_atomic(target, backup)
    else:
        atomic_write(backup.with_suffix(backup.suffix + '.absent'), b'', 0o600)


def restore_file(target, backup):
    if backup.exists():
        copy_atomic(backup, target)
    elif backup.with_suffix(backup.suffix + '.absent').exists():
        target.unlink(missing_ok=True)
    verify_file(target, backup)


def enabled_state(name):
    return run('systemctl', 'is-enabled', name, check=False).stdout.strip()


UNCHANGED_ENABLE_STATES = {'static', 'indirect', 'alias', 'linked', 'linked-runtime',
                           'enabled', 'enabled-runtime'}


def snapshot():
    BACKUP.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = BACKUP / 'services.json'
    if not state.exists():
        states = {name: {
            'enabled': enabled_state(name),
            'active': run('systemctl', 'is-active', name, check=False).stdout.strip() == 'active',
        } for name in SERVICES}
        if any(item['enabled'] not in UNCHANGED_ENABLE_STATES | {'disabled'} for item in states.values()):
            raise RuntimeError('Unsupported or masked service state; no services changed')
        atomic_write(state, json.dumps(states).encode(), 0o600)
    for name, saved in FILES.items():
        save_file(Path(name), BACKUP / saved)


def rollback():
    state = json.loads((BACKUP / 'services.json').read_text())
    errors = []
    def attempt(action, *args):
        try:
            action(*args)
        except Exception as error:
            errors.append(str(error))
    attempt(run, 'systemctl', 'stop', 'shairport-sync')
    for name, saved in FILES.items():
        attempt(restore_file, Path(name), BACKUP / saved)
    attempt(run, 'systemctl', 'daemon-reload')
    for name in reversed(SERVICES):
        def restore_service():
            expected = state[name]['enabled']
            if enabled_state(name) != expected:
                if expected in ('enabled', 'disabled'):
                    run('systemctl', 'enable' if expected == 'enabled' else 'disable', name)
                else:
                    raise RuntimeError(f'Cannot reconstruct changed enable state for {name}: {expected}')
            if enabled_state(name) != expected:
                raise RuntimeError(f'Enable state restoration failed: {name}')
        attempt(restore_service)
        # Do not restart the receiver with a partially restored configuration.
        if name != 'shairport-sync' or not errors:
            attempt(run, 'systemctl', 'restart' if state[name]['active'] else 'stop', name)
            def verify_active():
                active = run('systemctl', 'is-active', name, check=False).stdout.strip() == 'active'
                if active != state[name]['active']:
                    raise RuntimeError(f'Active state restoration failed: {name}')
            attempt(verify_active)
    if errors:
        raise RuntimeError('Rollback incomplete; backups retained for retry: ' + '; '.join(errors))


def start_service(name):
    state = enabled_state(name)
    if state == 'disabled':
        run('systemctl', 'enable', name)
    elif state not in UNCHANGED_ENABLE_STATES:
        raise RuntimeError(f'Unsupported service enable state: {name}: {state}')
    run('systemctl', 'start', name)


def preflight():
    for executable in ('shairport-sync', 'systemctl'):
        if not shutil.which(executable):
            raise RuntimeError(f'Install required OS dependency first: {executable}')
    pwd.getpwnam('shairport-sync')
    version = run('shairport-sync', '-V')
    check_capabilities(version.stdout + version.stderr)
    check_loopback()
    for name in ('server.py', 'audio_sources.py', 'airplay_control.py', 'airplay_baseline.py', 'manual_source_route.py'):
        if not (Path('/opt/octet-remote') / name).exists():
            raise RuntimeError('Upgrade Octet remote files before enabling AirPlay')
    # Validate against the real binary and existing protected preset before enabling.
    candidate = source_config(yaml.safe_load(PROFILE.read_text()), AIRPLAY)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yml') as stream:
        yaml.safe_dump(candidate, stream)
        stream.flush()
        run('/opt/camilladsp/camilladsp', '--check', stream.name)
    return candidate


def verify_services():
    time.sleep(1)
    for name in SERVICES:
        run('systemctl', 'is-active', '--quiet', name)
    if not Path('/run/octet-airplay/control.sock').is_socket():
        raise RuntimeError('AirPlay control socket did not start')


def install():
    preflight()
    snapshot()
    try:
        run('systemctl', 'stop', 'shairport-sync')
        # Revalidate both directions after stopping the receiver. Never touch the DAC.
        check_loopback()
        try:
            grp.getgrnam('octet-airplay')
        except KeyError:
            run('groupadd', '--system', 'octet-airplay')
        content = [
            (Path(__file__).parents[1] / 'etc/shairport-sync.conf').read_text(),
            RECEIVER_OVERRIDE, REMOTE_OVERRIDE,
        ]
        for (name, _), text in zip(FILES.items(), content):
            target = Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(target, text.encode(), 0o644)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'restart', 'octet-remote')
        for name in ('avahi-daemon', 'shairport-sync'):
            start_service(name)
        verify_services()
    except Exception as error:
        try:
            rollback()
        except Exception as rollback_error:
            raise RuntimeError(f'Installation failed: {error}; {rollback_error}') from error
        raise
    print('AirPlay enabled; physical cast verification required. Backup:', BACKUP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run on the Pi as root.')
    os.umask(0o077)
    rollback() if args.rollback else install()


if __name__ == '__main__':
    main()
