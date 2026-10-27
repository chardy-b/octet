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


if __name__ == '__main__':
    unittest.main()
