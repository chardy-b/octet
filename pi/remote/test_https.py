import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import server


class HttpsProxyTests(unittest.TestCase):
    def test_https_controls_keep_origin_and_local_display_guards(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        port = http.server_address[1]
        secure_host = 'octet.tailf55458.ts.net'
        proxy = {'Host': secure_host, 'X-Forwarded-Host': secure_host,
                 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '100.68.188.11',
                 'Origin': 'https://' + secure_host, 'Content-Type': 'application/json'}

        def request(path, headers):
            conn = HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                conn.request('POST', path, body=b'{}', headers=headers)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        try:
            with patch.object(server, 'BIND', '127.0.0.1'), patch.object(server, 'PORT', port), \
                    patch.object(server, 'action', return_value={'ok': True}) as action:
                self.assertEqual(request('/api/control/probe', proxy), (200, {'ok': True}))
                action.assert_called_once_with('probe', {})
                action.reset_mock()
                for override in ({'Origin': 'https://evil.example'}, {'Origin': 'http://' + secure_host},
                                 {'Host': 'evil.example'}, {'X-Forwarded-Host': 'evil.example'},
                                 {'X-Forwarded-Proto': 'http'}):
                    with self.subTest(override=override):
                        self.assertEqual(request('/api/control/probe', {**proxy, **override})[0], 403)
                action.assert_not_called()
                self.assertEqual(request('/api/control/visualizer-frame', proxy),
                                 (403, {'error': 'Local display required'}))
                direct = {'Host': f'127.0.0.1:{port}', 'Origin': f'http://127.0.0.1:{port}',
                          'Content-Type': 'application/json'}
                self.assertEqual(request('/api/control/probe', direct)[0], 200)
        finally:
            http.shutdown()
            http.server_close()

    def test_other_tailnet_devices_cannot_impersonate_the_proxy(self):
        handler = object.__new__(server.Handler)
        handler.headers = {'Host': 'octet.tailf55458.ts.net',
                           'X-Forwarded-Host': 'octet.tailf55458.ts.net',
                           'X-Forwarded-Proto': 'https'}
        handler.client_address = ('100.68.188.11', 12345)
        self.assertFalse(handler.https_proxy())
        self.assertFalse(handler.valid_host())


if __name__ == '__main__':
    unittest.main()
