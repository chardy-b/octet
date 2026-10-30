"""Deployment reconciliation: peer, host, proxy and stream boundaries."""
import io
import ipaddress
import json
import subprocess
import unittest
import tempfile
from pathlib import Path
from email.message import Message
from unittest.mock import MagicMock, patch

import server
from network_access import NetworkAccess
import test_playlists as playlists


class NetworkSecurity(unittest.TestCase):
    def setUp(self):
        self.network = NetworkAccess()
        self.snapshot = (frozenset({'192.168.1.10'}), (ipaddress.ip_network('192.168.1.0/24'),))
        self.patches = [patch.object(server, 'NETWORK', self.network),
                        patch.object(server, 'BIND', '0.0.0.0'),
                        patch.object(server, 'PORT', 8767),
                        patch.object(self.network, 'snapshot', return_value=self.snapshot)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def handler(self, peer='192.168.1.20', host='octet.local:8767', **headers):
        h = object.__new__(server.Handler)
        h.client_address = (peer, 1234)
        h.headers = Message()
        h.headers['Host'] = host
        for key, value in headers.items():
            h.headers[key] = value
        h.send = MagicMock()
        h.rfile = io.BytesIO(b'{}')
        h.path = '/api/control/mute'
        return h

    def test_peers_and_hosts_fail_closed(self):
        for peer in ('127.0.0.1', '192.168.1.20', '100.80.1.2'):
            self.assertTrue(self.handler(peer).valid_host())
        for peer in ('192.168.2.20', '10.1.1.1', '8.8.8.8', '::ffff:192.168.1.20', 'invalid'):
            self.assertFalse(self.handler(peer).valid_host())
        for host in ('evil.example:8767', '0.0.0.0:8767', 'octet.local', 'octet.local:80', 'octet.local:8767.evil'):
            self.assertFalse(self.handler(host=host).valid_host())

    def test_proxy_headers_require_local_peer_and_known_forwarded_host(self):
        forwarded = {'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'octet.tailf55458.ts.net'}
        local = self.handler('127.0.0.1', 'octet.tailf55458.ts.net', **forwarded)
        self.assertTrue(local.valid_host())
        self.assertEqual(local.request_origin(), 'https://octet.tailf55458.ts.net')
        for peer in ('192.168.1.20', '100.80.1.2'):
            remote = self.handler(peer, 'octet.tailf55458.ts.net', **forwarded)
            self.assertFalse(remote.https_proxy())
            self.assertFalse(remote.valid_host())
        forwarded['X-Forwarded-Host'] = 'evil.example'
        self.assertFalse(self.handler('127.0.0.1', **forwarded).https_proxy())

    def test_post_checks_origin_before_action(self):
        cases = [('192.168.1.20', 'http://octet.local:8767', {}, True),
                 ('192.168.1.20', 'https://evil.example', {}, False),
                 ('192.168.1.20', 'null', {}, False),
                 ('127.0.0.1', 'https://octet.tailf55458.ts.net',
                  {'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'octet.tailf55458.ts.net'}, True),
                 ('100.80.1.2', 'https://octet.tailf55458.ts.net',
                  {'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'octet.tailf55458.ts.net'}, False)]
        for peer, origin, headers, allowed in cases:
            with self.subTest(peer=peer, origin=origin):
                h = self.handler(peer, Origin=origin, **headers)
                h.headers['Content-Type'] = 'application/json'
                h.headers['Content-Length'] = '2'
                with patch.object(server, 'action', return_value={}) as action:
                    h.do_POST()
                    self.assertEqual(action.called, allowed)
                if not allowed:
                    self.assertEqual(h.send.call_args.args[1], 403)

    def test_stream_urls_for_single_and_playlist_use_loopback(self):
        self.assertEqual(self.network.stream_host('100.100.177.81'), '100.100.177.81')
        fixture = playlists.Playlists()
        player = playlists.Player()
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), \
             patch.object(server, 'DSP', return_value=fixture.dsp()), \
             patch.object(server, 'CATALOG', {'1': {'id': '1'}}), \
             patch.object(server, 'current_status', return_value={}), \
             patch.object(server, 'mpd', side_effect=player), \
             patch.object(server, 'playlist_contents', return_value=fixture.contents()):
            server.action('enqueue', {'id': '1'})
            self.assertTrue(any(url.startswith('http://127.0.0.1:8767/stream/1?entry=') for _, url in player.queue))
            server.action('play-playlist', {'id': playlists.PLAYLIST})
            self.assertTrue(all(url.startswith('http://127.0.0.1:8767/stream/') for _, url in player.queue))


class Discovery(unittest.TestCase):
    def test_only_physical_private_interfaces_and_failure_closes_lan(self):
        interfaces = [dict(ifname=name, addr_info=[dict(family='inet', scope='global', local=ip, prefixlen=24)])
                      for name, ip in [('eth0', '192.168.1.10'), ('docker0', '172.17.0.1'), ('eth1', '8.8.8.8')]]
        network = NetworkAccess()
        with patch('network_access.Path.exists', autospec=True, side_effect=lambda p: '/docker0/' not in str(p)), \
             patch('network_access.subprocess.run', return_value=MagicMock(stdout=json.dumps(interfaces))) as run:
            self.assertEqual(network.snapshot()[0], frozenset({'192.168.1.10'}))
            self.assertTrue(network.allowed_peer('192.168.1.20', '0.0.0.0'))
            run.assert_called_once()
        for error in (OSError(), subprocess.TimeoutExpired('ip', 2), ValueError()):
            network._expires = 0
            with patch('network_access.subprocess.run', side_effect=error):
                self.assertEqual(network.snapshot(), (frozenset(), ()))
                self.assertFalse(network.allowed_peer('192.168.1.20', '0.0.0.0'))
                self.assertTrue(network.allowed_peer('100.80.1.2', '0.0.0.0'))
