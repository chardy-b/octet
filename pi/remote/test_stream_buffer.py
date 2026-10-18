import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
from http.client import IncompleteRead
import server


class Upstream(io.BytesIO):
    def __init__(self, data, status=200, headers=None, events=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {'Content-Type': 'audio/mp4', 'Content-Length': str(len(data))}
        self.events = events if events is not None else []

    def read(self, count=-1):
        self.events.append(('read', count))
        return super().read(count)


class StreamBuffer(unittest.TestCase):
    def handler(self, headers=None, events=None):
        events = events if events is not None else []
        result = SimpleNamespace(headers=headers or {}, wfile=io.BytesIO(), close_connection=False)
        result.send_response = lambda status: events.append(('response', status))
        result.send_header = MagicMock()
        result.end_headers = MagicMock()
        return result

    def test_prebuffer_before_headers_and_preserve_every_audio_byte(self):
        events = []
        data = bytes(range(256)) * 5000
        handler = self.handler(events=events)
        upstream = Upstream(data, events=events)
        with patch.object(server, 'urlopen', return_value=upstream):
            server.Handler.stream_audio(handler, 'https://example.test/audio')
        self.assertEqual(events[:2], [('read', server.STREAM_PREBUFFER_BYTES), ('response', 200)])
        self.assertEqual(handler.wfile.getvalue(), data)
        handler.send_header.assert_any_call('Content-Length', str(len(data)))
        self.assertTrue(handler.close_connection)

    def test_forward_seek_range_and_partial_response(self):
        handler = self.handler({'Range': 'bytes=1024-'})
        upstream = Upstream(b'partial audio', 206, {'Content-Range': 'bytes 1024-1036/1037', 'Content-Length': '13', 'Accept-Ranges': 'bytes'})
        with patch.object(server, 'urlopen', return_value=upstream) as opening:
            server.Handler.stream_audio(handler, 'https://example.test/audio')
        self.assertEqual(opening.call_args.args[0].get_header('Range'), 'bytes=1024-')
        handler.send_header.assert_any_call('Content-Range', 'bytes 1024-1036/1037')
        self.assertEqual(handler.wfile.getvalue(), b'partial audio')

    def test_reject_malformed_or_multiple_ranges_before_network_access(self):
        with patch.object(server, 'urlopen') as opening:
            for value in ('bytes=0-1,3-4', 'oops', 'bytes=-', 'bytes=1-\r\nHeader: value'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    server.Handler.stream_audio(self.handler({'Range': value}), 'https://example.test/audio')
            opening.assert_not_called()

    def test_empty_source_does_not_send_success_headers(self):
        events = []
        with patch.object(server, 'urlopen', return_value=Upstream(b'')):
            with self.assertRaises(RuntimeError):
                server.Handler.stream_audio(self.handler(events=events), 'https://example.test/audio')
        self.assertEqual(events, [])

    def test_midstream_failure_closes_without_appending_an_error_body(self):
        handler = self.handler()
        upstream = Upstream(b'audio')
        upstream.read = MagicMock(side_effect=[b'audio', IncompleteRead(b'')])
        with patch.object(server, 'urlopen', return_value=upstream):
            server.Handler.stream_audio(handler, 'https://example.test/audio')
        self.assertEqual(handler.wfile.getvalue(), b'audio')
        self.assertTrue(handler.close_connection)


if __name__ == '__main__':
    unittest.main()
