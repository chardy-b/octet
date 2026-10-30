#!/usr/bin/env python3
"""Install the song publisher without restarting any audio/control services."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess


def patch_page(html):
    link = '<a href="http://octet.local:8768/">Now playing · Wi-Fi</a>'
    if link not in html:
        if '</footer>' not in html:
            raise RuntimeError('Music page layout changed')
        html = html.replace('</footer>', link + '</footer>', 1)
    return html


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run sudo python3 install.py on Octet.')
    source = Path(__file__).resolve().parent
    target = Path('/opt/octet-now-playing')
    index = Path('/opt/octet-remote/index.html')
    html = patch_page(index.read_text())
    backup = Path('/var/backups/octet-now-playing') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True, mode=0o700)
    for path in (index,):
        shutil.copy2(path, backup / path.name)
    target.mkdir(exist_ok=True)
    for name in ('now_playing.py', 'now-playing.html', 'now-playing.js'):
        shutil.copy2(source / name, target / name)
        (target / name).chmod(0o644)
    for name in ('octet-now-playing.socket', 'octet-now-playing.service'):
        destination = Path('/etc/systemd/system') / name
        if destination.exists():
            shutil.copy2(destination, backup / name)
        shutil.copy2(source / name, destination)
        destination.chmod(0o644)
    index.write_text(html)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'octet-now-playing.socket'], check=True)
    # Restart just this publisher on an update; socket stays bound to wlan0.
    subprocess.run(['systemctl', 'try-restart', 'octet-now-playing.service'], check=True)
    print(f'Installed Wi-Fi-only now playing at http://octet.local:8768/; backup: {backup}')


if __name__ == '__main__':
    main()
