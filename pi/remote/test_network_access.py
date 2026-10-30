import ipaddress
import json
from pathlib import Path
import runpy
import subprocess
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import server
from network_access import NetworkAccess


SNAPSHOT = (frozenset({'192.168.68.63'}), (ipaddress.ip_network('192.168.68.0/22'),))


class NetworkAccessTests(unittest.TestCase):
    def test_existing_install_patch_preserves_other_code_and_is_repeatable(self):
        patch_server = runpy.run_path(str(Path(__file__).with_name('enable-lan.py')))['patch_server']
        current = Path(server.__file__).read_text(encoding='utf-8')
        self.assertEqual(patch_server(current), current)
        legacy = current.replace('from network_access import NetworkAccess\n', '')
        legacy = legacy.replace("BIND = os.environ.get('OCTET_BIND', '0.0.0.0')",
                                "BIND = os.environ.get('OCTET_BIND', '100.100.177.81')")
        legacy = legacy.replace('NETWORK = NetworkAccess()\n', '')
        legacy = legacy.replace('        if not NETWORK.allowed_peer(self.client_address[0], BIND):\n            return False\n', '')
        legacy = legacy.replace('host in NETWORK.hosts(PORT, BIND)',
                                "host in {f'{BIND}:{PORT}', f'octet:{PORT}', f'octet.tailf55458.ts.net:{PORT}'}")
        legacy = legacy.replace('not NETWORK.local_peer(self.client_address[0], BIND)',
                                "self.client_address[0] not in {BIND, '127.0.0.1', '::1'}")
        legacy = legacy.replace('NETWORK.local_peer(self.client_address[0], BIND)',
                                "self.client_address[0] in {BIND, '127.0.0.1', '::1'}")
        legacy = legacy.replace('    host = NETWORK.stream_host(BIND)\n', '    host = BIND\n')
        legacy += "\n# Preserve an independent live customization.\n"
        updated = patch_server(legacy)
        self.assertIn('independent live customization', updated)
        self.assertEqual(patch_server(updated), updated)
        # Legacy releases also construct their stream addresses inline.
        inline = legacy.replace('f\'http://{host}:{PORT}/stream/', 'f\'http://{BIND}:{PORT}/stream/')
        self.assertIn('f\'http://{NETWORK.stream_host(BIND)}:{PORT}/stream/', patch_server(inline))
        with self.assertRaises(RuntimeError):
            patch_server(legacy.replace('    def valid_host(self):', '    def changed_guard(self):'))

    def test_only_connected_private_subnet_and_tailnet_are_allowed(self):
        policy = NetworkAccess()
        with patch.object(policy, 'snapshot', return_value=SNAPSHOT):
            for peer in ('192.168.68.12', '192.168.71.254', '127.0.0.1', '100.68.188.11'):
                self.assertTrue(policy.allowed_peer(peer, '0.0.0.0'), peer)
            for peer in ('192.168.72.12', '10.0.0.2', '172.16.0.2', '8.8.8.8', '::ffff:192.168.68.12'):
                self.assertFalse(policy.allowed_peer(peer, '0.0.0.0'), peer)
            self.assertFalse(policy.local_peer('192.168.68.12', '0.0.0.0'))
            self.assertTrue(policy.local_peer('192.168.68.63', '0.0.0.0'))
            self.assertNotIn('0.0.0.0:8767', policy.hosts(8767, '0.0.0.0'))

    def test_discovery_excludes_virtual_interfaces_and_refreshes_after_dhcp(self):
        def interface(name, address):
            return dict(ifname=name, addr_info=[dict(family='inet', scope='global', local=address, prefixlen=24)])
        first = [interface('wlan0', '192.168.1.5'), interface('docker0', '172.17.0.1'),
                 interface('tailscale0', '100.100.177.81'), interface('eth0', '8.8.8.8')]
        second = [interface('wlan0', '192.168.2.5')]
        policy = NetworkAccess()
        with patch('network_access.Path.exists', side_effect=lambda: True) as exists, \
                patch('network_access.subprocess.run') as run, \
                patch('network_access.time.monotonic', side_effect=[0, 1, 6, 12]):
            # Only wlan0/eth0 represent physical interfaces on this machine.
            exists.side_effect = [True, False, False, True, True]
            run.return_value.stdout = json.dumps(first)
            self.assertEqual(policy.snapshot()[0], {'192.168.1.5'})
            self.assertEqual(policy.snapshot()[0], {'192.168.1.5'})
            run.return_value.stdout = json.dumps(second)
            self.assertEqual(policy.snapshot()[0], {'192.168.2.5'})
            run.side_effect = subprocess.TimeoutExpired('ip', 2)
            self.assertEqual(policy.snapshot(), (frozenset(), ()))

    def test_wildcard_listener_uses_loopback_for_mpd_streams(self):
        with patch.object(server, 'BIND', '0.0.0.0'):
            self.assertTrue(server.queue_url('123').startswith('http://127.0.0.1:8767/stream/123?'))


class LanHttpTests(unittest.TestCase):
    def test_lan_page_and_controls_without_auth_keep_network_and_origin_guards(self):
        class Handler(server.Handler):
            peer = '192.168.68.12'

            def __init__(self, request, client_address, http):
                super().__init__(request, (self.peer, client_address[1]), http)

        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        port = http.server_port

        def request(method, path, host='octet.local', origin=None, extra=None):
            headers = {'Host': f'{host}:{port}', 'Content-Type': 'application/json'}
            if origin:
                headers['Origin'] = origin
            headers.update(extra or {})
            conn = HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                conn.request(method, path, body=b'{}' if method == 'POST' else None, headers=headers)
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()

        try:
            with patch.object(server, 'BIND', '0.0.0.0'), patch.object(server, 'PORT', port), \
                    patch.object(server.NETWORK, 'snapshot', return_value=SNAPSHOT), \
                    patch.object(server, 'action', return_value={'ok': True}) as action:
                for host in ('octet.local', 'octet', '192.168.68.63'):
                    self.assertEqual(request('GET', '/', host)[0], 200)
                    self.assertEqual(request('POST', '/api/control/probe', host, f'http://{host}:{port}')[0], 200)
                self.assertEqual(action.call_count, 3)
                action.reset_mock()
                self.assertEqual(request('POST', '/api/control/probe', origin='https://evil.example')[0], 403)
                self.assertEqual(request('GET', '/', 'evil.example')[0], 403)
                self.assertEqual(request('POST', '/api/control/visualizer-frame')[0], 403)
                forwarded = {'Host': 'octet.tailf55458.ts.net', 'X-Forwarded-Proto': 'https',
                             'X-Forwarded-Host': 'octet.tailf55458.ts.net'}
                self.assertEqual(request('GET', '/', extra=forwarded)[0], 403)
                for peer in ('8.8.8.8', '10.0.0.2', '192.168.72.2'):
                    Handler.peer = peer
                    self.assertEqual(request('GET', '/')[0], 403)
                    self.assertEqual(request('HEAD', '/visualizer-media/sky.mp4')[0], 403)
                    self.assertEqual(request('POST', '/api/control/probe')[0], 403)
                action.assert_not_called()
        finally:
            http.shutdown()
            http.server_close()


if __name__ == '__main__':
    unittest.main()
