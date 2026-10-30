import struct
import unittest
from unittest.mock import MagicMock, patch

import server


class DSPTransport(unittest.TestCase):
    def test_failed_handshake_always_closes_socket(self):
        for error in (TimeoutError('handshake timed out'), RuntimeError('DSP disconnected')):
            sock = MagicMock()
            with self.subTest(error=error), \
                 patch.object(server.socket, 'create_connection', return_value=sock), \
                 patch.object(server, 'read_exact', side_effect=error):
                with self.assertRaises(type(error)):
                    server.DSP().__enter__()
                sock.close.assert_called_once()

    def test_configuration_frames_support_all_length_encodings(self):
        for size in (125, 126, 65535, 65536, 100000):
            with self.subTest(size=size):
                dsp = server.DSP()
                dsp.sock = MagicMock()
                payload = b'x' * size
                dsp.send_frame(payload)
                frame = dsp.sock.sendall.call_args.args[0]
                self.assertEqual(frame[0], 0x81)
                self.assertTrue(frame[1] & 0x80)
                code = frame[1] & 127
                if code == 126:
                    length, offset = struct.unpack('!H', frame[2:4])[0], 4
                elif code == 127:
                    length, offset = struct.unpack('!Q', frame[2:10])[0], 10
                else:
                    length, offset = code, 2
                self.assertEqual(length, size)
                mask, data = frame[offset:offset+4], frame[offset+4:]
                self.assertEqual(bytes(v ^ mask[i % 4] for i, v in enumerate(data)), payload)
