#!/usr/bin/env python3
"""One-step Bluetooth setup for an Octet Pi. Dry run unless --apply.

Run as root on the Pi from the staged repository copy (pi/remote), after
install.py and install_airplay.py. It makes Bluetooth work from boot with no
attention, in this order:

1. installs the BlueALSA packages;
2. masks Debian's own bluealsa and bluealsa-aplay services (octet-bluealsa
   replaces them; two daemons would compete for the phone's audio);
3. installs the WirePlumber fragment that keeps PipeWire away from Bluetooth;
4. runs install_bluetooth.py (receiver unit and octet-remote drop-in);
5. starts octet-bluealsa at boot;
6. turns on Auto mode (a phone that starts streaming takes over);
7. installs and enables octet-bluetooth-justworks (pairing with no code);
8. removes the taskbar's Bluetooth applet, whose pairing agent breaks Just Works.

Every step checks first and is skipped when already done, so re-running on a Pi
that is set up changes nothing. Replaced files are backed up in BACKUP. It never
restarts octet-remote, WirePlumber or audio: reboot afterwards to finish.
"""
import argparse
import os
from pathlib import Path
import pwd
import subprocess
import time

import install_bluetooth
from install_airplay import atomic_write, copy_atomic, run

REPO = Path(__file__).resolve().parent.parent
PACKAGES = ('bluez-alsa-utils', 'libasound2-plugin-bluez')
DISTRO_UNITS = ('bluealsa.service', 'bluealsa-aplay.service')
SYSTEMD = Path('/etc/systemd/system')
WIREPLUMBER = Path('/etc/wireplumber/wireplumber.conf.d/90-octet-bluealsa.conf')
AUTO = SYSTEMD / 'octet-remote.service.d/bluetooth-auto.conf'
JUSTWORKS = SYSTEMD / 'octet-bluetooth-justworks.service'
BACKUP = Path('/var/backups/octet-bluetooth-setup')
USER = 'octet'


def panel(user):
    return Path(pwd.getpwnam(user).pw_dir) / '.config/wf-panel-pi/wf-panel-pi.ini'


def files(user):
    """Installed path -> (repository source, owner or None for root)."""
    account = pwd.getpwnam(user)
    return {WIREPLUMBER: (REPO / 'install/wireplumber-bluealsa.conf', None),
            AUTO: (REPO / 'systemd/octet-remote-bluetooth-auto.conf', None),
            JUSTWORKS: (REPO / 'systemd/octet-bluetooth-justworks.service', None),
            panel(user): (REPO / 'home/wf-panel-pi.ini', (account.pw_uid, account.pw_gid))}


def installed(path, source):
    return path.is_file() and not path.is_symlink() and path.read_bytes() == source.read_bytes()


def install_file(path, source, owner, backup):
    if path.is_symlink():
        raise RuntimeError(f'Refusing to replace a symlink: {path}')
    if path.exists():
        backup.mkdir(mode=0o700, parents=True, exist_ok=True)
        copy_atomic(path, backup / path.name)
    if owner is not None:
        # Directories inside the user's home are created owned by the user, not root.
        home = Path(pwd.getpwuid(owner[0]).pw_dir)
        for directory in reversed(path.parents):
            if home in directory.parents and not directory.exists():
                directory.mkdir()
                os.chown(directory, *owner)
    atomic_write(path, source.read_bytes(), 0o644, owner)


def apt(*args):
    # Downloads can take minutes; run() allows 30 s. Output streams to the console.
    subprocess.run(['apt-get', '-q', *args], check=True, timeout=1800)


def packages_missing():
    return [name for name in PACKAGES
            if run('dpkg-query', '-W', '-f=${Status}', name, check=False).stdout.strip()
            != 'install ok installed']


def masked(unit):
    return run('systemctl', 'is-enabled', unit, check=False).stdout.strip() == 'masked'


def wanted_at_boot():
    return (SYSTEMD / 'multi-user.target.wants/octet-bluealsa.service').exists()


def receiver_installed():
    try:
        daemon = install_bluetooth.daemon_path()
    except RuntimeError:
        return False
    return (install_bluetooth.DROPIN.is_file() and install_bluetooth.UNIT.is_file()
            and install_bluetooth.DROPIN.read_text() == install_bluetooth.dropin()
            and install_bluetooth.UNIT.read_text() == install_bluetooth.unit(daemon))


def steps(user, backup):
    """(description, done?, apply) in order; done is re-read just before each apply."""
    installs = files(user)

    def put(path):
        source, owner = installs[path]
        return (lambda: installed(path, source), lambda: install_file(path, source, owner, backup))

    return [
        ('install packages ' + ' '.join(PACKAGES), lambda: not packages_missing(),
         lambda: (apt('update'), apt('install', '-y', *packages_missing()))),
        ('mask Debian ' + ' and '.join(DISTRO_UNITS), lambda: all(map(masked, DISTRO_UNITS)),
         lambda: run('systemctl', 'mask', '--now', *DISTRO_UNITS)),
        (f'install {WIREPLUMBER} (PipeWire leaves Bluetooth alone)', *put(WIREPLUMBER)),
        ('install the receiver unit and drop-in (install_bluetooth.py)', receiver_installed,
         install_bluetooth.install),
        ('start octet-bluealsa at boot', wanted_at_boot,
         lambda: run('systemctl', 'add-wants', 'multi-user.target', 'octet-bluealsa.service')),
        (f'turn on Auto mode ({AUTO})', *put(AUTO)),
        (f'install {JUSTWORKS} (pairing with no code)', *put(JUSTWORKS)),
        ('enable octet-bluetooth-justworks',
         lambda: run('systemctl', 'is-enabled', JUSTWORKS.name, check=False).stdout.strip() == 'enabled',
         lambda: run('systemctl', 'enable', JUSTWORKS.name)),
        (f'remove the taskbar Bluetooth applet for {user} ({panel(user)})', *put(panel(user))),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--user', default=USER, help='desktop user whose taskbar is changed')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise RuntimeError('Run on the Pi as root')
    backup = BACKUP / time.strftime('%Y%m%d-%H%M%S')
    todo = []
    for description, done, apply in steps(args.user, backup):
        if done():
            print(f'  done     {description}')
            continue
        if not args.apply:
            print(f'  to do    {description}')
            todo.append(description)
            continue
        print(f'  applying {description}', flush=True)
        apply()
        run('systemctl', 'daemon-reload')
        if not done():
            raise RuntimeError(f'Step did not take effect: {description}')
        todo.append(description)
    if not args.apply:
        print('Dry run; pass --apply to make these changes.' if todo else 'Bluetooth is set up.')
    elif todo:
        print('Bluetooth set up. Reboot to finish: octet-remote, WirePlumber and the taskbar '
              f'pick up their new settings at boot. Replaced files: {backup}')
    else:
        print('Bluetooth is already set up; nothing changed.')
    return todo


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        raise SystemExit(f'Octet Bluetooth setup: {error}')
