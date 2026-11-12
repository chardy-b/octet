"""Stdlib-only CamillaDSP client for the root DAC broker. No app/state imports."""
import base64
import hashlib
import json
import os
import socket
import struct

def read_exact(sock, count):
    result = bytearray()
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise RuntimeError('DSP disconnected')
        result.extend(chunk)
    return bytes(result)


class DSP:
    """Small synchronous RFC6455 client for CamillaDSP's loopback WebSocket."""
    def __enter__(self):
        self.sock = socket.create_connection(('127.0.0.1', 1234), timeout=4)
        try:
            self.handshake()
        except Exception:
            self.sock.close()
            raise
        return self

    def handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        request = ('GET / HTTP/1.1\r\nHost: 127.0.0.1:1234\r\nUpgrade: websocket\r\n'
                   'Connection: Upgrade\r\nSec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\n\r\n')
        self.sock.sendall(request.encode())
        response = bytearray()
        while not response.endswith(b'\r\n\r\n') and len(response) < 8192:
            response.extend(read_exact(self.sock, 1))
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if not response.endswith(b'\r\n\r\n') or not self.upgrade_accepted(bytes(response), expected):
            raise RuntimeError('DSP connection failed')

    @staticmethod
    def upgrade_accepted(response, expected):
        lines = response.decode('latin-1').split('\r\n')
        if not lines[0].startswith('HTTP/1.1 101 '):
            return False
        headers = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(':')
            if not separator:
                return False
            headers.setdefault(name.strip().lower(), []).append(value.strip())
        accept, upgrade, connection = (headers.get(name, []) for name in ('sec-websocket-accept', 'upgrade', 'connection'))
        return (accept == [expected] and len(upgrade) == 1 and upgrade[0].lower() == 'websocket'
                and any(token.strip().lower() == 'upgrade' for value in connection for token in value.split(',')))

    def __exit__(self, *args):
        self.sock.close()

    def send_frame(self, data, opcode=1):
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0xfe]) + struct.pack('!H', length)
        else:
            header = bytes([0x80 | opcode, 0xff]) + struct.pack('!Q', length)
        self.sock.sendall(header + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(data)))

    def call(self, name, value=None):
        self.send_frame(json.dumps(name if value is None else {name: value}).encode())
        while True:
            first, second = read_exact(self.sock, 2)
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', read_exact(self.sock, 2))[0]
            elif length == 127:
                length = struct.unpack('!Q', read_exact(self.sock, 8))[0]
            if length > 2_000_000:
                raise RuntimeError('DSP response too large')
            mask = read_exact(self.sock, 4) if second & 128 else None
            data = read_exact(self.sock, length)
            if mask:
                data = bytes(v ^ mask[i % 4] for i, v in enumerate(data))
            opcode = first & 15
            if opcode == 9:
                self.send_frame(data, 10)
                continue
            if opcode != 1 or not first & 128:
                raise RuntimeError('Unexpected DSP response')
            result = json.loads(data).get(name, {})
            if result.get('result') != 'Ok':
                raise RuntimeError('DSP could not complete that action')
            return result.get('value')

