#!/usr/bin/env python3
"""Enable LAN access on an existing Pi, preserving its application/audio changes."""
from datetime import datetime, timezone
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def patch_server(text):
    replacements = [
        ('from input_levels import InputLevels\n',
         'from input_levels import InputLevels\nfrom network_access import NetworkAccess\n'),
        ("BIND = os.environ.get('OCTET_BIND', '100.100.177.81')",
         "BIND = os.environ.get('OCTET_BIND', '0.0.0.0')"),
        ("PORT = int(os.environ.get('OCTET_PORT', '8767'))\n",
         "PORT = int(os.environ.get('OCTET_PORT', '8767'))\nNETWORK = NetworkAccess()\n"),
        ('    def valid_host(self):\n',
         '    def valid_host(self):\n        if not NETWORK.allowed_peer(self.client_address[0], BIND):\n            return False\n'),
        ("host in {f'{BIND}:{PORT}', f'octet:{PORT}', f'octet.tailf55458.ts.net:{PORT}'}",
         'host in NETWORK.hosts(PORT, BIND)'),
        ("self.client_address[0] in {BIND, '127.0.0.1', '::1'}",
         'NETWORK.local_peer(self.client_address[0], BIND)'),
        ("self.client_address[0] not in {BIND, '127.0.0.1', '::1'}",
         'not NETWORK.local_peer(self.client_address[0], BIND)'),
    ]
    for old, new in replacements:
        if new in text:
            continue
        if text.count(old) != 1:
            raise RuntimeError(f'Server layout changed; inspect before installing: {old!r}')
        text = text.replace(old, new, 1)
    # Older releases build stream URLs inline in both track and playlist controls.
    text = text.replace("f'http://{BIND}:{PORT}/stream/", "f'http://{NETWORK.stream_host(BIND)}:{PORT}/stream/")
    text = text.replace('Tailscale-only music controls for Octet.', 'LAN and Tailscale music controls for Octet.')
    compile(text, 'server.py', 'exec')
    return text


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run sudo python3 enable-lan.py on Octet.')
    source = Path(__file__).resolve().parent
    target = Path('/opt/octet-remote')
    server_path = target / 'server.py'
    module = source / 'network_access.py'
    updated = patch_server(server_path.read_text())
    compile(module.read_text(), 'network_access.py', 'exec')
    sys.path.insert(0, str(target))
    import server

    def audio_state():
        with server.DSP() as dsp:
            settings = {name: dsp.call(name) for name in ('GetConfigJson', 'GetConfigFilePath', 'GetVolume', 'GetMute')}
        pids = subprocess.check_output(['systemctl', 'show', '--property=MainPID', 'camilladsp', 'mpd'])
        return settings, pids

    before = audio_state()
    override = Path('/etc/systemd/system/octet-remote.service.d/lan.conf')
    paths = [server_path, target / 'network_access.py', override]
    backup = Path('/var/backups/octet-lan') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True, mode=0o700)
    originals = {}
    for path in paths:
        originals[path] = path.exists()
        if path.exists():
            shutil.copy2(path, backup / path.name)

    def request(host, method='GET', origin=None):
        conn = HTTPConnection('127.0.0.1', server.PORT, timeout=5)
        headers = {'Host': host}
        if origin:
            headers['Origin'] = origin
        try:
            conn.request(method, '/' if method == 'GET' else '/api/control/probe', headers=headers)
            response = conn.getresponse()
            response.read()
            return response.status
        finally:
            conn.close()

    try:
        shutil.copy2(module, target / 'network_access.py')
        (target / 'network_access.py').chmod(0o644)
        server_path.write_text(updated)
        override.parent.mkdir(parents=True, exist_ok=True)
        # iproute2 uses netlink to discover the physical interface/subnet.
        override.write_text('[Service]\nEnvironment=OCTET_BIND=0.0.0.0\n'
                            'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK\n')
        override.chmod(0o644)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', 'octet-remote'], check=True)
        for attempt in range(20):
            try:
                if request(f'octet.local:{server.PORT}') == 200:
                    break
            except OSError:
                pass
            time.sleep(.25)
        else:
            raise RuntimeError('LAN web service did not become healthy')
        assert request(f'evil.example:{server.PORT}') == 403, 'Host guard failed'
        assert request(f'octet.local:{server.PORT}', 'POST', 'https://evil.example') == 403, 'Origin guard failed'
        assert audio_state() == before, 'Audio state changed during deployment'
        from network_access import NetworkAccess
        addresses, _ = NetworkAccess().snapshot()
        if not addresses:
            raise RuntimeError('No active physical LAN interface found')
        for address in addresses:
            assert request(f'{address}:{server.PORT}') == 200, 'LAN IP host rejected'
    except BaseException:
        for path, existed in originals.items():
            if existed:
                shutil.copy2(backup / path.name, path)
            else:
                path.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', 'octet-remote'], check=True)
        raise
    print(json.dumps(dict(enabled=True, url=f'http://octet.local:{server.PORT}',
                          addresses=sorted(addresses), backup=str(backup), audio_settings_preserved=True)))


if __name__ == '__main__':
    main()
