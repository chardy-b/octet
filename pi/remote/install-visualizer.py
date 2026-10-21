#!/usr/bin/env python3
"""Update the web app and HDMI kiosk without changing playback or DSP settings."""
from datetime import datetime, timezone
import os
from pathlib import Path
import pwd
import shutil
import subprocess


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run sudo python3 install-visualizer.py on Octet.')
    source = Path(__file__).resolve().parent
    target = Path('/opt/octet-remote')
    user = pwd.getpwnam('octet')
    home = Path(user.pw_dir)
    backup = Path('/var/backups/octet-visualizer') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, mode=0o700)
    files = ['server.py', 'audio_sources.py', 'input_levels.py', 'input-levels.js', 'visualizer_state.py', 'index.html', 'app.js', 'dsp.html', 'dsp.js', 'style.css',
             'visualizer.html', 'visualizer.js', 'visualizer-sync.js', 'visualizer-themes.js', 'visualizer-solar.js',
             'visualizer-audio.js', 'visualizer.css', 'README.md', 'launch-visualizer.sh']
    # Back up all files before replacing any of the running application.
    for name in files:
        if (target / name).exists():
            shutil.copy2(target / name, backup / name)
    for relative in ['.config/systemd/user/octet-visualizer.service', '.config/autostart/octet-visualizer.desktop']:
        path = home / relative
        if path.exists():
            shutil.copy2(path, backup / path.name)
    target.mkdir(mode=0o755, exist_ok=True)
    for name in files:
        shutil.copy2(source / name, target / name)
        (target / name).chmod(0o755 if name.endswith('.sh') else 0o644)
    shutil.copytree(source / 'visualizer-media', target / 'visualizer-media', dirs_exist_ok=True)
    for path in (target / 'visualizer-media').iterdir():
        path.chmod(0o644)
    for relative, name in [('.config/systemd/user', 'octet-visualizer.service'), ('.config/autostart', 'octet-visualizer.desktop')]:
        directory = home / relative
        directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chown(directory, user.pw_uid, user.pw_gid)
        shutil.copy2(source / name, directory / name)
        os.chown(directory / name, user.pw_uid, user.pw_gid)
        (directory / name).chmod(0o644)
    subprocess.run(['systemctl', 'restart', 'octet-remote'], check=True)
    env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{user.pw_uid}', DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{user.pw_uid}/bus')
    for command in ['daemon-reload', 'restart octet-visualizer.service']:
        subprocess.run(['runuser', '-u', 'octet', '--', 'systemctl', '--user', *command.split()], env=env, check=True)
    print('Visualizer and HDMI autostart installed. Backup:', backup)


if __name__ == '__main__':
    main()
