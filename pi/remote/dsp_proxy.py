"""Peer-credential-checked Unix socket proxy to CamillaDSP's private WebSocket listener.

The proxy is a byte relay: it never parses WebSocket frames or CamillaDSP commands, so the
RFC 6455 handshake, frames and close sequence pass through unchanged. It only decides who may
connect (SO_PEERCRED against two exact, root-configured UIDs) and bounds time and concurrency.
The destination is fixed; there is no generic forwarding.

Filesystem contract (enforced here or left to the service unit):
- The socket's parent directory must be owned by the proxy's own effective UID and not
  group/world-writable. It is opened with O_NOFOLLOW and the socket is bound relative to that
  open directory, so a swapped path component cannot redirect the bind. systemd's
  RuntimeDirectory= creates it (owner octet-dsp-proxy, group octet-dsp-control, 0750); its
  ancestors (/run) are root-controlled by assumption.
- The proxy never unlinks by pathname. An existing entry at the socket path, stale or live, makes
  startup fail closed, and stop() only closes the listener. Removing the socket is the
  RuntimeDirectory= lifecycle's job in the later unit.
- The production socket mode is 0660; group ownership is a later unit gate.
Network-namespace placement and client migration are also later changes.
"""
import ipaddress
import json
import logging
import os
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time

SOCKET_PATH = '/run/octet-dsp/control.sock'
BACKEND = ('127.0.0.1', 1234)
CONFIG_PATH = '/etc/octet/dsp-proxy.json'
CONFIG_KEYS = {'upmpdcli_uid', 'autotune_uid'}
MAX_CONFIG_BYTES = 4096
CHUNK = 65536
DRAIN_TIMEOUT = 2.0
SOCKET_MODE = 0o660

log = logging.getLogger('octet.dsp_proxy')


class ProxyConfigError(Exception):
    pass


def load_config(path=CONFIG_PATH, required_owner=0):
    """Return {'upmpdcli': uid, 'autotune': uid} from a root-owned, non-writable regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise ProxyConfigError('DSP proxy configuration is unavailable: %s' % error.strerror) from error
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ProxyConfigError('DSP proxy configuration is not a regular file')
        if info.st_uid != required_owner:
            raise ProxyConfigError('DSP proxy configuration has the wrong owner')
        if info.st_mode & 0o022:
            raise ProxyConfigError('DSP proxy configuration is group- or world-writable')
        if info.st_size > MAX_CONFIG_BYTES:
            raise ProxyConfigError('DSP proxy configuration is too large')
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ProxyConfigError('DSP proxy configuration is too large')
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise ProxyConfigError('DSP proxy configuration is not valid JSON') from error
    if not isinstance(data, dict) or set(data) != CONFIG_KEYS:
        raise ProxyConfigError('DSP proxy configuration must contain exactly %s' % ', '.join(sorted(CONFIG_KEYS)))
    for value in data.values():
        # UID 0 and the 32-bit "unset" value are never a valid service identity here.
        if type(value) is not int or not 0 < value < 2**32 - 1:
            raise ProxyConfigError('DSP proxy configuration UIDs must be exact non-root integers')
    if data['upmpdcli_uid'] == data['autotune_uid']:
        raise ProxyConfigError('DSP proxy configuration requires distinct controller and Auto Tune UIDs')
    return {'upmpdcli': data['upmpdcli_uid'], 'autotune': data['autotune_uid']}


class DspProxy:
    def __init__(self, path, backend, authorized, *, connect_timeout=2.0, handshake_timeout=5.0,
                 idle_timeout=60.0, max_connections=8, socket_mode=0o600, parent_owner=None):
        host, port = backend
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback or type(port) is not int or not 0 < port < 65536:
            raise ValueError('DSP backend must be a loopback IP address and TCP port')
        self.path, self.backend = str(path), (host, port)
        self.allowed = frozenset(authorized.values())
        self.connect_timeout, self.handshake_timeout = connect_timeout, handshake_timeout
        self.idle_timeout, self.max_connections, self.socket_mode = idle_timeout, max_connections, socket_mode
        self.parent_owner = os.geteuid() if parent_owner is None else parent_owner
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.listener = None
        self.acceptor = None
        self.workers = {}   # thread -> (client, backend socket or None)

    @property
    def active(self):
        with self.lock:
            return len(self.workers)

    def start(self):
        parent, name = os.path.split(self.path)
        if not parent or not name:
            raise ValueError('DSP proxy socket path must include a directory and a name')
        # Hold the directory open: checks and bind both go through this one inode.
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        listener = None
        try:
            info = os.fstat(fd)
            if info.st_uid != self.parent_owner:
                raise PermissionError('DSP proxy socket directory has the wrong owner')
            if info.st_mode & 0o022:
                raise PermissionError('DSP proxy socket directory is group- or world-writable')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o777 & ~self.socket_mode)   # creates the socket with exactly this mode
            try:
                # bind() fails with EADDRINUSE on any existing entry, so no check-then-act window.
                listener.bind('/proc/self/fd/%d/%s' % (fd, name))
            finally:
                os.umask(old)
            listener.listen(16)
            listener.settimeout(0.1)
        except BaseException:
            if listener:
                listener.close()
            raise
        finally:
            os.close(fd)
        self.listener = listener
        self.acceptor = threading.Thread(target=self._accept, name='dsp-proxy-accept', daemon=True)
        self.acceptor.start()

    def stop(self):
        self.stopping.set()
        if self.acceptor:
            self.acceptor.join()
            self.acceptor = None
        if self.listener:
            self.listener.close()
            self.listener = None
        with self.lock:
            workers = list(self.workers.items())
        for _, socks in workers:
            for sock in socks:
                self._shutdown(sock)
        for thread, _ in workers:
            thread.join()

    @staticmethod
    def _shutdown(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass

    def peer_uid(self, conn):
        try:
            _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
        except (OSError, struct.error):
            return None
        return uid

    def _accept(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self.lock:
                full = len(self.workers) >= self.max_connections
                if not full and not self.stopping.is_set():
                    thread = threading.Thread(target=self._serve, args=(conn,), name='dsp-proxy-conn', daemon=True)
                    self.workers[thread] = (conn,)
                else:
                    thread = None
            if thread:
                thread.start()
            else:
                conn.close()

    def _serve(self, client):
        me = threading.current_thread()
        upstream = None
        try:
            uid = self.peer_uid(client)
            if uid is None or uid not in self.allowed:
                log.warning('DSP proxy denied a local peer (uid %s)', uid)
                return
            try:
                upstream = socket.create_connection(self.backend, timeout=self.connect_timeout)
            except OSError:
                log.warning('DSP backend is unavailable')
                return
            with self.lock:
                self.workers[me] = (client, upstream)
            if self.stopping.is_set():
                return
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._relay(client, upstream)
        except OSError:
            pass
        finally:
            for sock in (client, upstream):
                if sock:
                    sock.close()
            with self.lock:
                self.workers.pop(me, None)

    def _relay(self, client, upstream):
        peers = {client: upstream, upstream: client}
        reading = set(peers)
        established = False
        deadline = time.monotonic() + self.handshake_timeout
        while reading and not self.stopping.is_set():
            wait = deadline - time.monotonic()
            if wait <= 0:
                return
            ready, _, _ = select.select(list(reading), [], [], min(wait, 0.5))
            for sock in ready:
                data = sock.recv(CHUNK)
                if not data:
                    reading.discard(sock)
                    try:
                        peers[sock].shutdown(socket.SHUT_WR)
                    except OSError:
                        return
                    # Half-closed: allow the other direction a short time to drain, then finish.
                    deadline = min(deadline, time.monotonic() + DRAIN_TIMEOUT)
                    continue
                peers[sock].settimeout(max(deadline - time.monotonic(), 0.1) if not established else self.idle_timeout)
                peers[sock].sendall(data)
                if sock is upstream:
                    established = True   # backend answered: the handshake window is over
                if len(reading) == 2:
                    deadline = time.monotonic() + (self.idle_timeout if established else self.handshake_timeout)


def main():
    logging.basicConfig(level=logging.INFO, format='%(name)s: %(message)s')
    try:
        authorized = load_config(CONFIG_PATH)
    except ProxyConfigError as error:
        log.error('%s; refusing to start', error)
        return 1
    proxy = DspProxy(SOCKET_PATH, BACKEND, authorized, socket_mode=SOCKET_MODE)
    done = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: done.set())
    try:
        proxy.start()
    except OSError as error:
        log.error('Could not listen on %s: %s', SOCKET_PATH, error)
        return 1
    try:
        done.wait()
    finally:
        proxy.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
