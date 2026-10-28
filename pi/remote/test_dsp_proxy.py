import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import threading
import time
import signal
import unittest
from unittest import mock

import dsp_proxy
from dsp_proxy import DspProxy, ProxyConfigError, load_config

GUID = b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
KEY = base64.b64encode(b'0123456789abcdef')
REQUEST = (b'GET / HTTP/1.1\r\nHost: 127.0.0.1:1234\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
           b'Sec-WebSocket-Key: ' + KEY + b'\r\nSec-WebSocket-Version: 13\r\n\r\n')


def frame(opcode, payload, mask=None):
    head = bytes([0x80 | opcode])
    n = len(payload)
    flag = 0x80 if mask else 0
    head += bytes([flag | n]) if n < 126 else bytes([flag | 126]) + struct.pack('>H', n)
    if mask:
        return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + payload


def read_exact(sock, n):
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise EOFError(data)
        data += chunk
    return data


def read_until(sock, marker):
    data = b''
    while marker not in data:
        chunk = sock.recv(1)
        if not chunk:
            raise EOFError(data)
        data += chunk
    return data


class FakeBackend:
    """Minimal local TCP WebSocket server: handshake, echoes data frames, answers close."""

    def __init__(self, mode='echo'):
        self.mode = mode
        self.requests = []
        self.connections = 0
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        with conn:
            try:
                if self.mode == 'silent':
                    time.sleep(5)
                    return
                request = read_until(conn, b'\r\n\r\n')
                self.requests.append(request)
                accept = base64.b64encode(hashlib.sha1(KEY + GUID).digest())
                conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                             b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
                while True:
                    head = read_exact(conn, 2)
                    opcode, n = head[0] & 15, head[1] & 127
                    if n == 126:
                        n = struct.unpack('>H', read_exact(conn, 2))[0]
                    mask = read_exact(conn, 4) if head[1] & 128 else b'\0\0\0\0'
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(conn, n)))
                    if opcode == 8:
                        conn.sendall(frame(8, payload))
                        return
                    conn.sendall(frame(opcode, payload))
            except (EOFError, OSError):
                return

    def close(self):
        try:
            self.listener.shutdown(socket.SHUT_RDWR)   # wake the blocked accept so the port really closes
        except OSError:
            pass
        self.listener.close()
        self.thread.join(2)


class ProxyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'control.sock')
        self.backend = FakeBackend()
        self.addCleanup(self.backend.close)
        self.proxy = None

    def start(self, authorized=None, **options):
        me = os.getuid()
        self.proxy = DspProxy(self.path, ('127.0.0.1', self.backend.port),
                              authorized if authorized is not None else {'upmpdcli': me, 'autotune': me + 4242}, **options)
        self.proxy.start()
        self.addCleanup(self.proxy.stop)
        return self.proxy

    def dial(self):
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.settimeout(3)
        sock.connect(self.path)
        return sock

    def handshake(self, sock):
        sock.sendall(REQUEST)
        return read_until(sock, b'\r\n\r\n')

    def recv_closed(self, sock):
        # Closing with unread request bytes may surface as a reset instead of a clean EOF.
        try:
            return sock.recv(1)
        except ConnectionResetError:
            return b''

    def assert_closed_without_backend(self, sock):
        self.assertEqual(self.recv_closed(sock), b'')
        time.sleep(0.2)
        self.assertEqual(self.backend.connections, 0)


class AuthorizationTests(ProxyCase):
    def test_upmpdcli_uid_is_forwarded(self):
        self.start({'upmpdcli': os.getuid(), 'autotune': os.getuid() + 1})
        sock = self.dial()
        self.assertIn(b'101 Switching Protocols', self.handshake(sock))

    def test_distinct_autotune_uid_is_forwarded(self):
        self.start({'upmpdcli': os.getuid() + 1, 'autotune': os.getuid()})
        self.assertIn(b'101', self.handshake(self.dial()))

    def test_unlisted_uid_is_denied_and_backend_never_dialed(self):
        self.start({'upmpdcli': os.getuid() + 1, 'autotune': os.getuid() + 2})
        sock = self.dial()
        try:
            sock.sendall(REQUEST)
        except OSError:
            pass
        self.assert_closed_without_backend(sock)

    def test_octet_service_uid_is_denied(self):
        # The test process stands in for the `octet` UID, which is deliberately not configured.
        self.start({'upmpdcli': 996, 'autotune': 997})
        self.assert_closed_without_backend(self.dial())

    def test_unknown_peer_credentials_fail_closed(self):
        self.start()
        self.proxy.peer_uid = lambda conn: None
        self.assert_closed_without_backend(self.dial())

    def test_socket_is_not_world_accessible_by_default(self):
        self.start()
        self.assertEqual(os.stat(self.path).st_mode & 0o077, 0)


class ForwardingTests(ProxyCase):
    def test_handshake_bytes_are_forwarded_unchanged(self):
        self.start()
        sock = self.dial()
        response = self.handshake(sock)
        accept = base64.b64encode(hashlib.sha1(KEY + GUID).digest())
        self.assertIn(b'Sec-WebSocket-Accept: ' + accept, response)
        self.assertEqual(self.backend.requests, [REQUEST])

    def test_masked_text_binary_and_large_frames_round_trip(self):
        self.start()
        sock = self.dial()
        self.handshake(sock)
        for opcode, payload in ((1, b'{"GetState":null}'), (2, bytes(range(125))), (1, b'x' * 3000)):
            sock.sendall(frame(opcode, payload, mask=b'\x01\x02\x03\x04'))
            head = read_exact(sock, 2)
            n = head[1] & 127
            if n == 126:
                n = struct.unpack('>H', read_exact(sock, 2))[0]
            self.assertEqual(head[0], 0x80 | opcode)
            self.assertEqual(read_exact(sock, n), payload)

    def test_fragmented_writes_are_reassembled_by_backend(self):
        self.start()
        sock = self.dial()
        self.handshake(sock)
        data = frame(1, b'split', mask=b'\x09\x08\x07\x06')
        for byte in data:
            sock.sendall(bytes([byte]))
        self.assertEqual(read_exact(sock, 7), frame(1, b'split'))

    def test_close_handshake_reaches_client_then_eof(self):
        self.start()
        sock = self.dial()
        self.handshake(sock)
        sock.sendall(frame(8, struct.pack('>H', 1000), mask=b'\0\0\0\0'))
        self.assertEqual(read_exact(sock, 4), frame(8, struct.pack('>H', 1000)))
        self.assertEqual(sock.recv(1), b'')

    def test_client_disconnect_closes_backend_connection(self):
        self.start()
        sock = self.dial()
        self.handshake(sock)
        sock.close()
        deadline = time.monotonic() + 2
        while self.proxy.active and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.proxy.active, 0)

    def test_concurrent_connections(self):
        self.start()
        socks = [self.dial() for _ in range(3)]
        for sock in socks:
            self.handshake(sock)
        for i, sock in enumerate(socks):
            sock.sendall(frame(1, b'n%d' % i, mask=b'\0\0\0\0'))
        for i, sock in enumerate(socks):
            self.assertEqual(read_exact(sock, 4), frame(1, b'n%d' % i))

    def test_connection_limit(self):
        self.start(max_connections=1)
        first = self.dial()
        self.handshake(first)
        second = self.dial()
        self.assertEqual(second.recv(1), b'')
        first.sendall(frame(1, b'a', mask=b'\0\0\0\0'))
        self.assertEqual(read_exact(first, 3), frame(1, b'a'))


class FailureTests(ProxyCase):
    def test_backend_absent_closes_client_promptly(self):
        self.backend.close()
        self.start(connect_timeout=0.5)
        sock = self.dial()
        sock.sendall(REQUEST)
        started = time.monotonic()
        self.assertEqual(self.recv_closed(sock), b'')
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.proxy.active, 0)

    def test_idle_client_before_handshake_times_out(self):
        self.start(handshake_timeout=0.3)
        sock = self.dial()
        started = time.monotonic()
        self.assertEqual(self.recv_closed(sock), b'')
        self.assertLess(time.monotonic() - started, 2)

    def test_idle_established_connection_times_out(self):
        self.start(idle_timeout=0.3)
        sock = self.dial()
        self.handshake(sock)
        started = time.monotonic()
        self.assertEqual(self.recv_closed(sock), b'')
        self.assertLess(time.monotonic() - started, 2)

    def test_silent_backend_times_out(self):
        self.backend.mode = 'silent'
        self.start(handshake_timeout=0.3)
        sock = self.dial()
        sock.sendall(REQUEST)
        self.assertEqual(self.recv_closed(sock), b'')

    def test_activity_extends_idle_timeout(self):
        self.start(idle_timeout=0.5)
        sock = self.dial()
        self.handshake(sock)
        for _ in range(4):
            time.sleep(0.25)
            sock.sendall(frame(9, b'', mask=b'\0\0\0\0'))
            self.assertEqual(read_exact(sock, 2), frame(9, b''))

    def test_stop_closes_active_connections_and_leaves_path_alone(self):
        self.start()
        sock = self.dial()
        self.handshake(sock)
        self.proxy.stop()
        self.assertEqual(self.recv_closed(sock), b'')
        self.assertTrue(os.path.exists(self.path))   # runtime-directory cleanup belongs to the service unit
        self.assertEqual(self.proxy.active, 0)
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith('dsp-proxy')])

    def test_stop_is_idempotent_and_refuses_new_connections_though_path_remains(self):
        self.start()
        self.proxy.stop()
        self.proxy.stop()
        self.assertTrue(os.path.exists(self.path))
        with self.assertRaises(ConnectionRefusedError):
            self.dial()
        self.assertEqual(self.backend.connections, 0)

    def test_socket_swapped_in_during_stop_is_preserved(self):
        self.start()
        other_path = os.path.join(self.tmp.name, 'other.sock')
        other = socket.socket(socket.AF_UNIX)
        self.addCleanup(other.close)
        other.bind(other_path)
        other.listen(1)
        os.replace(other_path, self.path)   # another process's socket now owns the pathname
        before = os.lstat(self.path)
        self.proxy.stop()
        after = os.lstat(self.path)
        self.assertEqual((before.st_ino, before.st_dev), (after.st_ino, after.st_dev))
        peer = self.dial()   # still reaches the replacement, not the stopped proxy
        other.settimeout(2)
        other.accept()[0].close()
        peer.close()

    def test_never_unlinks_by_pathname(self):
        with mock.patch('os.unlink') as unlink, mock.patch('os.remove') as remove, mock.patch('os.rmdir') as rmdir:
            self.start()
            self.proxy.stop()
        for call in (unlink, remove, rmdir):
            call.assert_not_called()

    def test_refuses_any_preexisting_path_and_preserves_it(self):
        Path(self.path).write_text('keep')
        with self.assertRaises(OSError):
            self.start()
        self.assertEqual(Path(self.path).read_text(), 'keep')

    def test_refuses_stale_socket_and_preserves_it(self):
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(self.path)
        stale.close()
        before = os.lstat(self.path)
        with self.assertRaises(OSError):
            self.start()
        after = os.lstat(self.path)
        self.assertTrue(stat.S_ISSOCK(after.st_mode))
        self.assertEqual(before.st_ino, after.st_ino)

    def test_refuses_live_socket_and_preserves_it(self):
        live = socket.socket(socket.AF_UNIX)
        self.addCleanup(live.close)
        live.bind(self.path)
        live.listen(1)
        with self.assertRaises(OSError):
            self.start()
        live.settimeout(2)
        self.dial().close()
        live.accept()[0].close()

    def test_refuses_symlink_at_path(self):
        target = os.path.join(self.tmp.name, 'target')
        os.symlink(target, self.path)
        with self.assertRaises(OSError):
            self.start()
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.islink(self.path))

    def test_requested_socket_mode_is_applied_exactly(self):
        self.start(socket_mode=0o660)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o660)

    def test_umask_is_restored_after_start(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.start(socket_mode=0o660)
        self.assertEqual(os.umask(0o022), 0o022)


class ParentDirectoryTests(ProxyCase):
    def make(self, mode, name='run'):
        parent = os.path.join(self.tmp.name, name)
        os.mkdir(parent)
        os.chmod(parent, mode)
        self.path = os.path.join(parent, 'control.sock')
        return parent

    def test_private_parent_is_accepted(self):
        self.make(0o750)
        self.start()
        self.assertIn(b'101', self.handshake(self.dial()))

    def test_group_writable_parent_is_refused(self):
        self.make(0o770)
        with self.assertRaises(OSError):
            self.start()
        self.assertFalse(os.path.lexists(self.path))

    def test_world_writable_parent_is_refused(self):
        self.make(0o757)
        with self.assertRaises(OSError):
            self.start()

    def test_parent_with_wrong_owner_is_refused(self):
        self.make(0o750)
        with self.assertRaises(OSError):
            self.start(parent_owner=os.geteuid() + 1)

    def test_missing_parent_is_refused(self):
        self.path = os.path.join(self.tmp.name, 'absent', 'control.sock')
        with self.assertRaises(OSError):
            self.start()

    def test_symlinked_parent_is_refused(self):
        real = self.make(0o750)
        link = os.path.join(self.tmp.name, 'link')
        os.symlink(real, link)
        self.path = os.path.join(link, 'control.sock')
        with self.assertRaises(OSError):
            self.start()

    def test_parent_that_is_a_file_is_refused(self):
        file = os.path.join(self.tmp.name, 'file')
        Path(file).write_text('x')
        self.path = os.path.join(file, 'control.sock')
        with self.assertRaises(OSError):
            self.start()


    def test_backend_must_be_loopback(self):
        with self.assertRaises(ValueError):
            DspProxy(self.path, ('192.0.2.1', 1234), {'upmpdcli': 1, 'autotune': 2})


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'dsp-proxy.json'

    def write(self, value, mode=0o640):
        self.path.write_text(value if isinstance(value, str) else json.dumps(value))
        os.chmod(self.path, mode)

    def load(self, **kw):
        return load_config(self.path, required_owner=os.getuid(), **kw)

    def test_valid_config(self):
        self.write({'upmpdcli_uid': 120, 'autotune_uid': 121})
        self.assertEqual(self.load(), {'upmpdcli': 120, 'autotune': 121})

    def test_absent_config_fails_closed(self):
        with self.assertRaises(ProxyConfigError):
            self.load()

    def test_rejects_wrong_owner(self):
        self.write({'upmpdcli_uid': 120, 'autotune_uid': 121})
        with self.assertRaises(ProxyConfigError):
            load_config(self.path, required_owner=os.getuid() + 1)

    def test_rejects_group_or_world_writable(self):
        self.write({'upmpdcli_uid': 120, 'autotune_uid': 121}, mode=0o660)
        with self.assertRaises(ProxyConfigError):
            self.load()
        self.write({'upmpdcli_uid': 120, 'autotune_uid': 121}, mode=0o602)
        with self.assertRaises(ProxyConfigError):
            self.load()

    def test_rejects_symlink(self):
        self.write({'upmpdcli_uid': 120, 'autotune_uid': 121})
        link = Path(self.tmp.name) / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaises(ProxyConfigError):
            load_config(link, required_owner=os.getuid())

    def test_rejects_bad_contents(self):
        for bad in ('not json', '[]', {}, {'upmpdcli_uid': 120}, {'upmpdcli_uid': 120, 'autotune_uid': 120},
                    {'upmpdcli_uid': 120, 'autotune_uid': '121'}, {'upmpdcli_uid': True, 'autotune_uid': 121},
                    {'upmpdcli_uid': 0, 'autotune_uid': 121}, {'upmpdcli_uid': -1, 'autotune_uid': 121},
                    {'upmpdcli_uid': 120, 'autotune_uid': 121, 'extra': 1},
                    {'upmpdcli_uid': 120, 'autotune_uid': 2**32 - 1}):
            self.write(bad)
            with self.subTest(bad=bad), self.assertRaises(ProxyConfigError):
                self.load()

    def test_rejects_oversized_config(self):
        self.write(' ' * 5000)
        with self.assertRaises(ProxyConfigError):
            self.load()

    def test_deployment_constants_are_fixed(self):
        self.assertEqual(dsp_proxy.SOCKET_PATH, '/run/octet-dsp/control.sock')
        self.assertEqual(dsp_proxy.BACKEND, ('127.0.0.1', 1234))

    def test_production_main_uses_fixed_path_backend_and_mode_0660(self):
        created = []

        class Fake:
            def __init__(self, *args, **kwargs):
                created.append((args, kwargs))

            def start(self):
                os.kill(os.getpid(), signal.SIGTERM)   # handlers are installed before start

            def stop(self):
                created.append('stopped')

        old = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
        for number, handler in old.items():
            self.addCleanup(signal.signal, number, handler)
        with mock.patch.object(dsp_proxy, 'load_config', return_value={'upmpdcli': 5, 'autotune': 6}), \
                mock.patch.object(dsp_proxy, 'DspProxy', Fake):
            self.assertEqual(dsp_proxy.main(), 0)
        (args, kwargs), stopped = created
        self.assertEqual(args, ('/run/octet-dsp/control.sock', ('127.0.0.1', 1234), {'upmpdcli': 5, 'autotune': 6}))
        self.assertEqual(kwargs, {'socket_mode': 0o660})
        self.assertEqual(stopped, 'stopped')

    def test_main_fails_closed_without_config(self):
        original = dsp_proxy.CONFIG_PATH
        dsp_proxy.CONFIG_PATH = str(Path(self.tmp.name) / 'missing.json')
        self.addCleanup(setattr, dsp_proxy, 'CONFIG_PATH', original)
        self.assertNotEqual(dsp_proxy.main(), 0)


if __name__ == '__main__':
    unittest.main()
