import base64
import hashlib
import json
import os
import socket
import struct
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import server


class UnixDspEndpoint:
    """Real AF_UNIX RFC6455 server standing in for the DSP proxy socket."""
    def __init__(self, directory, replies=None):
        self.path = os.path.join(directory, 'control.sock')
        self.replies, self.requests, self.handshakes = replies or {}, [], []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(4)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    @staticmethod
    def exact(conn, count):
        data = b''
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                try:
                    self.session(conn)
                except (EOFError, OSError):
                    pass

    def session(self, conn):
        head = b''
        while not head.endswith(b'\r\n\r\n'):
            head += self.exact(conn, 1)
        self.handshakes.append(head)
        key = next(line.split(b': ', 1)[1] for line in head.split(b'\r\n') if line.lower().startswith(b'sec-websocket-key'))
        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        while True:
            first, second = self.exact(conn, 2)
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', self.exact(conn, 2))[0]
            mask = self.exact(conn, 4)
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(self.exact(conn, length)))
            if first & 15 == 8:
                return
            request = json.loads(payload)
            name = request if isinstance(request, str) else next(iter(request))
            self.requests.append(request)
            body = json.dumps({name: self.replies.get(name, {'result': 'Ok'})}).encode()
            conn.sendall(bytes([0x81, len(body)]) + body)

    def close(self):
        self.listener.close()


class UnixDspTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        real = socket.create_connection
        self.tcp = []
        def refuse(address, *args, **kwargs):
            self.tcp.append(address)
            raise AssertionError('direct TCP dial: %r' % (address,))
        patcher = patch.object(server.socket, 'create_connection', refuse)
        patcher.start()
        self.addCleanup(patcher.stop)

    def endpoint(self, replies=None):
        endpoint = UnixDspEndpoint(self.tmp.name, replies)
        self.addCleanup(endpoint.close)
        patcher = patch.object(server, 'DSP_SOCKET', endpoint.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return endpoint

    def test_production_socket_path(self):
        self.assertEqual(server.DSP_SOCKET, '/run/octet-dsp/control.sock')

    def test_call_uses_unix_websocket_and_never_dials_tcp(self):
        endpoint = self.endpoint({'GetVolume': {'result': 'Ok', 'value': -42.5}})
        with server.DSP() as dsp:
            self.assertEqual(dsp.call('GetVolume'), -42.5)
            dsp.call('SetMute', True)
        self.assertEqual(endpoint.requests, ['GetVolume', {'SetMute': True}])
        self.assertTrue(endpoint.handshakes[0].startswith(b'GET / HTTP/1.1\r\n'))
        self.assertEqual(self.tcp, [])

    def test_rejected_command_keeps_error_semantics(self):
        self.endpoint({'SetMute': {'result': 'Error'}})
        with server.DSP() as dsp, self.assertRaisesRegex(RuntimeError, 'could not complete'):
            dsp.call('SetMute', False)

    def test_missing_socket_fails_closed_without_direct_fallback(self):
        missing = os.path.join(self.tmp.name, 'absent.sock')
        with patch.object(server, 'DSP_SOCKET', missing), self.assertRaises(OSError):
            server.DSP().__enter__()
        self.assertEqual(self.tcp, [])

    def test_missing_socket_never_unmutes(self):
        missing = os.path.join(self.tmp.name, 'absent.sock')
        with patch.object(server, 'DSP_SOCKET', missing), self.assertRaises(OSError):
            server.action('mute', {'muted': False})
        self.assertEqual(self.tcp, [])

    def test_connection_timeout_is_four_seconds(self):
        self.endpoint()
        with server.DSP() as dsp:
            self.assertEqual(dsp.sock.gettimeout(), 4)


class TrackedSocket(socket.socket):
    """Real socket that records whether the DSP client closed it."""
    instances = []
    send_error = None
    clamp = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False
        self.instances.append(self)

    def close(self):
        self.closed = True
        super().close()

    def sendall(self, data, *args):
        if self.send_error:
            raise self.send_error
        return super().sendall(data, *args)

    def settimeout(self, value):
        super().settimeout(self.clamp if self.clamp is not None and value else value)


GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class HandshakeFailures(unittest.TestCase):
    """Every failure inside DSP.__enter__ must close the Unix socket and never try TCP."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tcp = []
        def refuse(address, *args, **kwargs):
            self.tcp.append(address)
            raise AssertionError('direct TCP dial: %r' % (address,))
        TrackedSocket.instances, TrackedSocket.send_error, TrackedSocket.clamp = [], None, None
        shim = types.SimpleNamespace(AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM,
                                     socket=TrackedSocket, create_connection=refuse)
        patcher = patch.object(server, 'socket', shim)
        patcher.start()
        self.addCleanup(patcher.stop)

    def listen(self, respond):
        """respond(conn, key, accept) writes whatever the fake peer should send."""
        self.count = getattr(self, 'count', 0) + 1
        path = os.path.join(self.tmp.name, 'h%d.sock' % self.count)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        self.addCleanup(listener.close)
        def run():
            conn, _ = listener.accept()
            with conn:
                head = b''
                while not head.endswith(b'\r\n\r\n'):
                    chunk = conn.recv(1)
                    if not chunk:
                        return
                    head += chunk
                key = next(line.split(b': ', 1)[1] for line in head.split(b'\r\n')
                           if line.lower().startswith(b'sec-websocket-key'))
                accept = base64.b64encode(hashlib.sha1(key + GUID).digest())
                respond(conn, key, accept)
        threading.Thread(target=run, daemon=True).start()
        patcher = patch.object(server, 'DSP_SOCKET', path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_rejected(self, error=RuntimeError, message=None):
        with self.assertRaises(error) as caught:
            server.DSP().__enter__()
        if message:
            self.assertRegex(str(caught.exception), message)
        self.assertEqual(len(TrackedSocket.instances), 1)
        self.assertTrue(TrackedSocket.instances[0].closed)
        self.assertEqual(self.tcp, [])

    @staticmethod
    def reply(accept, extra=(b'Upgrade: websocket', b'Connection: Upgrade'), status=b'HTTP/1.1 101 Switching Protocols'):
        lines = [status, *extra]
        if accept is not None:
            lines.append(b'Sec-WebSocket-Accept: ' + accept)
        return b'\r\n'.join(lines) + b'\r\n\r\n'

    def test_sendall_failure_closes_socket(self):
        self.listen(lambda conn, key, accept: None)
        TrackedSocket.send_error = BrokenPipeError('gone')
        self.assert_rejected(BrokenPipeError)

    def test_eof_mid_header_closes_socket(self):
        self.listen(lambda conn, key, accept: conn.sendall(b'HTTP/1.1 101 Swit'))
        self.assert_rejected(RuntimeError, 'DSP disconnected')

    def test_timeout_closes_socket(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.listen(lambda conn, key, accept: release.wait(5))
        TrackedSocket.clamp = 0.1
        self.assert_rejected(socket.timeout)

    def test_oversized_header_closes_socket(self):
        self.listen(lambda conn, key, accept: conn.sendall(b'HTTP/1.1 101 OK\r\nX: ' + b'a' * 9000))
        self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_wrong_accept_closes_socket(self):
        self.listen(lambda conn, key, accept: conn.sendall(self.reply(base64.b64encode(b'x' * 20))))
        self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_non_101_status_closes_socket(self):
        self.listen(lambda conn, key, accept: conn.sendall(self.reply(accept, status=b'HTTP/1.1 403 Forbidden')))
        self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_accept_hash_hidden_in_other_header_is_rejected(self):
        # The expected hash appears in the response, but not as Sec-WebSocket-Accept.
        self.listen(lambda conn, key, accept: conn.sendall(
            self.reply(base64.b64encode(b'y' * 20), extra=(b'Upgrade: websocket', b'Connection: Upgrade', b'X-Spoof: ' + accept))))
        self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_accept_hash_as_prefix_or_suffix_is_rejected(self):
        for value in (lambda a: a + b'AAAA', lambda a: b'AAAA' + a):
            with self.subTest():
                TrackedSocket.instances.clear()
                self.listen(lambda conn, key, accept, value=value: conn.sendall(self.reply(value(accept))))
                self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_missing_accept_is_rejected(self):
        self.listen(lambda conn, key, accept: conn.sendall(self.reply(None)))
        self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_missing_or_wrong_upgrade_is_rejected(self):
        for extra in ((b'Connection: Upgrade',), (b'Upgrade: h2c', b'Connection: Upgrade')):
            with self.subTest(extra=extra):
                TrackedSocket.instances.clear()
                self.listen(lambda conn, key, accept, extra=extra: conn.sendall(self.reply(accept, extra)))
                self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_missing_or_wrong_connection_is_rejected(self):
        for extra in ((b'Upgrade: websocket',), (b'Upgrade: websocket', b'Connection: keep-alive')):
            with self.subTest(extra=extra):
                TrackedSocket.instances.clear()
                self.listen(lambda conn, key, accept, extra=extra: conn.sendall(self.reply(accept, extra)))
                self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_duplicate_conflicting_accept_is_rejected(self):
        self.listen(lambda conn, key, accept: conn.sendall(
            self.reply(accept, extra=(b'Upgrade: websocket', b'Connection: Upgrade',
                                      b'Sec-WebSocket-Accept: ' + base64.b64encode(b'z' * 20)))))
        self.assert_rejected(RuntimeError, 'DSP connection failed')

    def test_valid_headers_accept_case_and_token_variants(self):
        self.listen(lambda conn, key, accept: conn.sendall(
            b'HTTP/1.1 101 Switching Protocols\r\nupgrade: WebSocket\r\nconnection: keep-alive, Upgrade\r\n'
            b'sec-websocket-accept: ' + accept + b'\r\n\r\n'))
        with server.DSP() as dsp:
            self.assertFalse(TrackedSocket.instances[0].closed)
        self.assertTrue(TrackedSocket.instances[0].closed)
        self.assertEqual(self.tcp, [])

    def test_connect_failure_closes_socket(self):
        with patch.object(server, 'DSP_SOCKET', os.path.join(self.tmp.name, 'none.sock')):
            self.assert_rejected(FileNotFoundError)


class PlaybackGuards(unittest.TestCase):
    def dsp(self, config=server.PROFILE):
        mock = MagicMock()
        mock.__enter__.return_value = mock
        mock.call.side_effect = lambda command, *args: config if command == 'GetConfigFilePath' else json.dumps({'devices': {'capture': {'device': 'hw:Loopback,1,0'}}}) if command == 'GetConfigJson' else None
        return mock

    def test_volume_rejects_excessive_or_nonfinite_levels(self):
        with patch.object(server, 'DSP') as dsp:
            for level in (-61, -17, 0, float('inf'), float('nan')):
                with self.subTest(level=level), self.assertRaises(ValueError):
                    server.action('volume', {'db': level})
            dsp.assert_not_called()

    def test_unmute_rejects_old_three_way_configuration(self):
        dsp = self.dsp('/opt/camilladsp/configs/3way-lr4.yml')
        with patch.object(server, 'DSP', return_value=dsp):
            with self.assertRaises(RuntimeError):
                server.action('mute', {'muted': False})
        self.assertNotIn(('SetMute', False), [tuple(c.args) for c in dsp.call.call_args_list])

    def test_mute_is_allowed_even_with_wrong_configuration(self):
        dsp = self.dsp('other.yml')
        with patch.object(server, 'DSP', return_value=dsp), patch.object(server, 'current_status', return_value={}):
            server.action('mute', {'muted': True})
        dsp.call.assert_called_once_with('SetMute', True)

    def test_failed_play_mutes_again(self):
        dsp = self.dsp()
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), \
             patch.object(server, 'CATALOG', {'123': {'id': '123'}}), \
             patch.object(server, 'DSP', return_value=dsp), \
             patch.object(server, 'mpd', side_effect=[[[('Id', '9')]], RuntimeError('offline')]):
            with self.assertRaises(RuntimeError):
                server.action('play', {'id': '123'})
        self.assertEqual(dsp.call.call_args.args, ('SetMute', True))

    def test_invalid_track_cannot_enter_mpd_command(self):
        for identifier in ('../123', '1\nstop', '-1', 'http://example.com', '1;stop'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                server.track_id(identifier)

    def test_mpd_arguments_reject_command_injection(self):
        for value in ('x\nplay', 'x\rstop', 'x\0stop'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.quote(value)

    def test_both_existing_plugin_and_flat_session_formats(self):
        self.assertEqual(server.decode_credentials({'token_type': {'data': 'Bearer'}, 'is_pkce': {'data': False}}),
                         {'token_type': 'Bearer', 'is_pkce': False})
        self.assertEqual(server.decode_credentials({'token_type': 'Bearer'}), {'token_type': 'Bearer'})

    def test_now_playing_and_artwork_routes_use_live_bridge(self):
        handler = object.__new__(server.Handler)
        handler.valid_host = lambda: True
        handler.send = MagicMock()
        key = 'a' * 64
        with patch.object(server, 'fetch_song', return_value={'title': 'Song'}), \
             patch.object(server, 'fetch_artwork', return_value=(b'\xff\xd8\xffx', 'image/jpeg')) as artwork:
            handler.path = '/api/now-playing'
            handler.do_GET()
            self.assertEqual(handler.send.call_args.args, ({'song': {'title': 'Song'}},))
            handler.path = '/api/now-playing/artwork/' + key
            handler.do_GET()
            artwork.assert_called_once_with(key)
            self.assertEqual(handler.send.call_args.args, (b'\xff\xd8\xffx',))
            self.assertEqual(handler.send.call_args.kwargs, {'content_type': 'image/jpeg'})
            for error in (OSError('offline'), ValueError('hash')):
                artwork.side_effect = error
                handler.do_GET()
                self.assertEqual(handler.send.call_args.args, ({'error': 'Artwork unavailable'}, 404))

    def test_now_playing_without_song_reports_null(self):
        handler = object.__new__(server.Handler)
        handler.valid_host = lambda: True
        handler.send = MagicMock()
        handler.path = '/api/now-playing'
        with patch.object(server, 'fetch_song', return_value=None):
            handler.do_GET()
        self.assertEqual(handler.send.call_args.args, ({'song': None},))

    def test_invalid_artwork_keys_never_reach_the_bridge(self):
        handler = object.__new__(server.Handler)
        handler.valid_host = lambda: True
        handler.send = MagicMock()
        with patch.object(server, 'fetch_artwork') as artwork, patch.object(server, 'tidal') as tidal:
            for key in ('a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, '../' + 'a' * 61, 'a' * 64 + '/x', ''):
                with self.subTest(key=key):
                    handler.path = '/api/now-playing/artwork/' + key
                    handler.do_GET()
                    self.assertEqual(handler.send.call_args.args, ({'error': 'Not found'}, 404))
            artwork.assert_not_called()
            tidal.assert_not_called()

    def test_artwork_response_uses_bridge_image_type_with_safety_headers(self):
        handler = object.__new__(server.Handler)
        handler.valid_host = lambda: True
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()
        handler.path = '/api/now-playing/artwork/' + 'b' * 64
        png = b'\x89PNG\r\n\x1a\nbody'
        with patch.object(server, 'fetch_artwork', return_value=(png, 'image/png')):
            handler.do_GET()
        headers = dict(call.args for call in handler.send_header.call_args_list)
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(headers['Content-Length'], str(len(png)))
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        handler.wfile.write.assert_called_once_with(png)

    def test_installer_packages_now_playing_bridge(self):
        installer = (Path(server.__file__).parent / 'install.py').read_text()
        self.assertIn("'now_playing_bridge.py'", installer)

    def test_remote_footer_links_wifi_now_playing(self):
        page = (Path(server.__file__).parent / 'index.html').read_text()
        footer = page[page.index('<footer>'):page.index('</footer>')]
        self.assertIn('<a href="http://octet.local:8768/">Now playing · Wi-Fi</a>', footer)


class InitializationSafety(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.muted = False
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        self.dsp.call.side_effect = self.call
        self.control = MagicMock(needs_mute=False, error=None)
        self.control.recover.side_effect = self.recover
        for item in (patch.object(server, 'DSP', return_value=self.dsp),
                     patch.object(server, 'AIRPLAY_CONTROL', self.control),
                     patch.object(server, 'LEVELS_UNLOCKED'),
                     patch.object(server, 'ACTIVE_CAPTURE_CHANNELS')):
            item.start()
            self.addCleanup(item.stop)

    def call(self, name, *args):
        self.calls.append((name, *args))
        if name == 'SetMute':
            self.muted = args[0]
        if name == 'GetMute':
            return self.muted
        if name == 'GetVolume':
            return -18
        if name == 'GetConfigJson':
            return '{}'

    def recover(self):
        self.calls.append(('recover',))
        self.assertTrue(self.muted)
        self.muted = False  # A reload can change mute state.
        self.control.needs_mute = False

    def test_mute_is_verified_before_and_after_recovery(self):
        server.initialize_dsp()
        self.assertEqual(self.calls[:5], [
            ('SetMute', True), ('GetMute',), ('recover',),
            ('SetMute', True), ('GetMute',)])
        self.assertTrue(self.muted)

    def test_recovery_failure_still_remutes_and_retains_retry(self):
        def fail():
            self.recover()
            raise OSError('broken recovery journal')
        self.control.recover.side_effect = fail
        server.initialize_dsp()
        self.assertTrue(self.muted)
        self.assertEqual(self.calls[3:5], [('SetMute', True), ('GetMute',)])
        self.assertTrue(self.control.needs_mute)
        self.assertIn('recovery failed', self.control.error)

    def test_initial_mute_failure_skips_recovery_and_retries(self):
        for command in ('SetMute', 'GetMute'):
            with self.subTest(command=command):
                self.control.recover.reset_mock()
                failed = False
                def fail_once(name, *args):
                    nonlocal failed
                    if name == command and not failed:
                        failed = True
                        raise OSError('DSP unavailable')
                    return self.call(name, *args)
                self.dsp.call.side_effect = fail_once
                server.initialize_dsp()
                self.control.recover.assert_not_called()
                self.assertTrue(self.muted)
                self.assertTrue(self.control.needs_mute)

    def test_unconfirmed_mute_prevents_both_services_starting(self):
        for value in (False, None, 1):
            with self.subTest(value=value):
                self.control.recover.reset_mock()
                self.dsp.call.side_effect = lambda name, *args: value if name == 'GetMute' else self.call(name, *args)
                with patch.object(server.os, 'umask'), \
                     patch.object(server.Path, 'exists', return_value=True), \
                     patch.object(server.threading, 'Thread') as thread, \
                     patch.object(server, 'ThreadingHTTPServer') as http, \
                     self.assertRaisesRegex(RuntimeError, 'refusing remote startup'):
                    server.main()
                thread.assert_not_called()
                http.assert_not_called()
                self.control.recover.assert_not_called()
                self.assertTrue(self.control.needs_mute)

    def test_post_recovery_mute_failure_is_fatal(self):
        for command in ('SetMute', 'GetMute', 'connect', 'unmuted'):
            with self.subTest(command=command):
                self.calls.clear()
                def fail_after_recovery(name, *args):
                    if command == 'unmuted' and ('recover',) in self.calls and name == 'GetMute':
                        return False
                    if ('recover',) in self.calls and name == command:
                        raise OSError('DSP disconnected after reload')
                    return self.call(name, *args)
                self.dsp.call.side_effect = fail_after_recovery
                def connect():
                    if command == 'connect' and ('recover',) in self.calls:
                        raise OSError('connection refused')
                    return self.dsp
                with patch.object(server, 'DSP', side_effect=connect), \
                     self.assertRaisesRegex(RuntimeError, 'refusing remote startup'):
                    server.initialize_dsp()
                self.assertTrue(self.control.needs_mute)


if __name__ == '__main__':
    unittest.main()
