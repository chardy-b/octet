import hashlib
import json
import time
import unittest
from unittest.mock import patch

import now_playing_bridge as bridge


class Response:
    def __init__(self, body, kind='application/json'):
        self.body = body
        self.headers = {'Content-Type': kind}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, n):
        return self.body[:n]


class BridgeTests(unittest.TestCase):
    def test_song_and_artwork_are_validated_and_proxied(self):
        body = b'\xff\xd8\xffimage'
        key = hashlib.sha256(body).hexdigest()
        song = dict(version=1, state='playing', updated_at=time.time(), source='airplay',
                    title='What More Can I Say', artist='Notations', album='Still Here',
                    artwork='/api/artwork/' + key)
        with patch.object(bridge, 'urlopen', return_value=Response(json.dumps(song).encode())):
            result = bridge.fetch_song()
            assert result is not None
            self.assertEqual(result['artwork'], '/api/now-playing/artwork/' + key)
        with patch.object(bridge, 'urlopen', return_value=Response(body, 'image/jpeg')):
            self.assertEqual(bridge.fetch_artwork(key), (body, 'image/jpeg'))
        with self.assertRaises(ValueError):
            bridge.fetch_artwork('http://example.com')

    def test_stopped_stale_and_invalid_songs_are_cleared(self):
        song = dict(version=1, state='playing', updated_at=time.time() - 60,
                    source='airplay', title='Old', artist='Artist', album='Album', artwork=None)
        for changed in ({}, {'state': 'stopped', 'updated_at': time.time()},
                        {'title': '<script>' * 100, 'updated_at': time.time()}):
            with self.subTest(changed=changed), patch.object(bridge, 'urlopen',
                return_value=Response(json.dumps({**song, **changed}).encode())):
                self.assertIsNone(bridge.fetch_song())
        with patch.object(bridge, 'urlopen', side_effect=OSError('offline')):
            self.assertIsNone(bridge.fetch_song())


class ArtworkValidationTests(unittest.TestCase):
    def fetch(self, body, kind, key=None):
        key = key or hashlib.sha256(body).hexdigest()
        with patch.object(bridge, 'urlopen', return_value=Response(body, kind)):
            return bridge.fetch_artwork(key)

    def test_invalid_keys_are_rejected_before_any_request(self):
        for key in ('a' * 63, 'a' * 65, 'A' * 64, 'g' * 64, '../' + 'a' * 61, 'a' * 64 + '\n', ''):
            with self.subTest(key=key), patch.object(bridge, 'urlopen') as opened:
                with self.assertRaises(ValueError):
                    bridge.fetch_artwork(key)
                opened.assert_not_called()

    def test_hash_mismatch_is_rejected(self):
        body = b'\xff\xd8\xffimage'
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            self.fetch(body, 'image/jpeg', key=hashlib.sha256(b'other').hexdigest())

    def test_supported_types_are_returned_without_parameters(self):
        for body, kind in ((b'\x89PNG\r\n\x1a\nimage', 'image/png'),
                           (b'RIFF\0\0\0\0WEBPimage', 'image/webp'),
                           (b'\xff\xd8\xffimage', 'image/jpeg; charset=binary')):
            with self.subTest(kind=kind):
                self.assertEqual(self.fetch(body, kind), (body, kind.split(';')[0]))

    def test_disallowed_or_mismatched_content_types_are_rejected(self):
        jpeg = b'\xff\xd8\xffimage'
        for body, kind in ((jpeg, 'text/html'), (jpeg, 'image/svg+xml'), (jpeg, ''),
                           (jpeg, 'IMAGE/JPEG'), (jpeg, 'image/png'),
                           (b'<svg onload=alert(1)>', 'image/jpeg'),
                           (b'RIFF\0\0\0\0WAVEdata', 'image/webp')):
            with self.subTest(kind=kind, body=body[:12]), self.assertRaises(ValueError):
                self.fetch(body, kind)

    def test_oversized_artwork_is_rejected(self):
        body = b'\xff\xd8\xff' + b'\0' * (2 * 1024 * 1024)
        with self.assertRaises(ValueError):
            self.fetch(body, 'image/jpeg')


if __name__ == '__main__':
    unittest.main()
