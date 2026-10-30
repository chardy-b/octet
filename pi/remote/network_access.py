"""Allow the directly connected LAN and the existing private Tailscale path."""
import ipaddress
import json
from pathlib import Path
import subprocess
import time


TAILSCALE_IP = '100.100.177.81'
TAILNET = ipaddress.ip_network('100.64.0.0/10')
LAN_RANGES = tuple(map(ipaddress.ip_network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))


class NetworkAccess:
    def __init__(self):
        self._expires = 0
        self._snapshot = (frozenset(), ())

    def snapshot(self):
        """Refresh after DHCP changes; discovery failures close LAN access."""
        now = time.monotonic()
        if now < self._expires:
            return self._snapshot
        addresses, networks = set(), []
        try:
            result = subprocess.run(['ip', '-j', '-4', 'address', 'show', 'up'],
                                    capture_output=True, text=True, check=True, timeout=2)
            for interface in json.loads(result.stdout):
                # Exclude VPNs, bridges and containers from unauthenticated LAN access.
                name = interface['ifname']
                if '/' in name or not (Path('/sys/class/net') / name / 'device').exists():
                    continue
                for entry in interface.get('addr_info', []):
                    if entry.get('family') != 'inet' or entry.get('scope') != 'global':
                        continue
                    address = ipaddress.ip_interface(f"{entry['local']}/{entry['prefixlen']}")
                    if any(address.ip in network for network in LAN_RANGES):
                        addresses.add(str(address.ip))
                        networks.append(address.network)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            addresses, networks = set(), []
        self._snapshot = (frozenset(addresses), tuple(networks))
        self._expires = now + 5
        return self._snapshot

    def local_peer(self, peer, bind):
        addresses, _ = self.snapshot()
        return peer in addresses | {'127.0.0.1', '::1', TAILSCALE_IP, bind} - {'0.0.0.0', '::'}

    def allowed_peer(self, peer, bind):
        if self.local_peer(peer, bind):
            return True
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        _, networks = self.snapshot()
        return address in TAILNET or (
            any(address in network for network in LAN_RANGES)
            and any(address in network for network in networks))

    def hosts(self, port, bind):
        addresses, _ = self.snapshot()
        names = addresses | {'127.0.0.1', TAILSCALE_IP, bind, 'octet', 'octet.local',
                             'octet.tailf55458.ts.net'}
        return {f'{name}:{port}' for name in names - {'0.0.0.0', '::'}}

    @staticmethod
    def stream_host(bind):
        return '127.0.0.1' if bind == '0.0.0.0' else bind
