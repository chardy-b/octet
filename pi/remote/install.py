#!/usr/bin/env python3
"""Install on the Pi with sudo python3 install.py; preserves previous settings."""
from datetime import datetime, timezone
import argparse
import grp
import os
from pathlib import Path
import re
import shutil
import subprocess
import pwd
import yaml
from server import CHANNEL_PROFILE, channel_config

# FIRST-TIME installer. It stops MPD and CamillaDSP, resets the CamillaDSP state file, rewrites
# mpd.conf and octet-remote.service, and copies the visualizer media. Do NOT use it to update a
# running Octet: run deploy.py, which changes only the application files and never touches DSP state.
from remote_manifest import EXECUTABLE, MEDIA, REMOTE_FILES  # noqa: E402  (shared with deploy.py)


def main():
    parser = argparse.ArgumentParser()
    parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run this installer with sudo on Octet.')
    source = Path(__file__).resolve().parent
    preset = 'channel-dsp.yml'
    files, media = list(REMOTE_FILES), list(MEDIA)
    # Fail before stopping playback if the staging tree is incomplete.
    for path in [*(source / name for name in files),
                 *(source / 'visualizer-media' / name for name in media),
                 source / preset, source / 'octet-remote.service']:
        if not path.is_file():
            raise SystemExit(f'Missing installation file: {path}')
    subprocess.run(['/opt/camilladsp/camilladsp', '--check', str(source / preset)], check=True)
    backup = Path('/var/backups/octet-remote') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup.mkdir(mode=0o700, parents=True)
    subprocess.run(['systemctl', 'stop', 'mpd.socket', 'mpd', 'camilladsp'], check=True)
    for name in ['/opt/camilladsp/statefile.yml', '/etc/mpd.conf', '/etc/systemd/system/camilladsp.service.d/octet-startup.conf']:
        path = Path(name)
        if path.exists():
            shutil.copy2(path, backup / path.name)
    target = Path('/opt/octet-remote')
    target.mkdir(mode=0o755, exist_ok=True)
    for name in files:
        shutil.copy2(source / name, target / name)
        os.chmod(target / name, 0o755 if name in EXECUTABLE else 0o644)
    (target / 'visualizer-media').mkdir(mode=0o755, exist_ok=True)
    for name in media:
        shutil.copy2(source / 'visualizer-media' / name, target / 'visualizer-media' / name)
        os.chmod(target / 'visualizer-media' / name, 0o644)
    for name in ['exciter-audition.yml']:
        if (source / name).exists():
            shutil.copy2(source / name, Path('/opt/camilladsp/configs') / name)
    CHANNEL_PROFILE.parent.mkdir(exist_ok=True)
    os.chown(CHANNEL_PROFILE.parent, pwd.getpwnam('upmpdcli').pw_uid, grp.getgrnam('audio').gr_gid)
    os.chmod(CHANNEL_PROFILE.parent, 0o2750)
    if CHANNEL_PROFILE.exists():
        shutil.copy2(CHANNEL_PROFILE, backup / CHANNEL_PROFILE.name)
    else:
        CHANNEL_PROFILE.write_text(yaml.safe_dump(channel_config(yaml.safe_load((source / preset).read_text())), sort_keys=False))
    os.chown(CHANNEL_PROFILE, pwd.getpwnam('upmpdcli').pw_uid, grp.getgrnam('audio').gr_gid)
    os.chmod(CHANNEL_PROFILE, 0o640)
    subprocess.run(['/opt/camilladsp/camilladsp', '--check', str(CHANNEL_PROFILE)], check=True)
    profile_path = str(CHANNEL_PROFILE)
    state = Path('/opt/camilladsp/statefile.yml')
    owner = state.stat()
    state.write_text(yaml.safe_dump(dict(config_path=profile_path,
                                      mute=[True, False, False, False, False], volume=[-36.0, 0., 0., 0., 0.]), sort_keys=False))
    os.chown(state, owner.st_uid, owner.st_gid)
    config = Path('/etc/mpd.conf')
    text = config.read_text()
    if re.search(r'^\s*restore_paused\s+', text, re.M):
        text = re.sub(r'^\s*restore_paused\s+.*$', 'restore_paused "yes"', text, flags=re.M)
    else:
        text += '\n# Octet: saved music never resumes automatically at boot.\nrestore_paused "yes"\n'
    config.write_text(text)
    override = Path('/etc/systemd/system/camilladsp.service.d')
    override.mkdir(exist_ok=True)
    (override / 'octet-startup.conf').write_text('[Service]\nExecStart=\nExecStart=/opt/camilladsp/camilladsp -p 1234 -a 127.0.0.1 -s /opt/camilladsp/statefile.yml -o /opt/camilladsp/camilladsp.log -w --mute --gain=-36\n')
    shutil.copy2(source / 'octet-remote.service', '/etc/systemd/system/octet-remote.service')
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'start', 'camilladsp', 'mpd'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'octet-remote'], check=True)
    subprocess.run(['systemctl', 'restart', 'octet-remote'], check=True)
    print('Installed, muted. Previous settings:', backup)
    print('On the same Wi-Fi/LAN, open http://octet.local:8767 — no sign-in required.')
    print('Tailscale access: https://octet.tailf55458.ts.net/')


if __name__ == '__main__':
    main()
