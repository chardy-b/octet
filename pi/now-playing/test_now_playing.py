import ipaddress
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import now_playing as publisher
from install import patch_page


class NowPlayingTests(unittest.TestCase):
    def test_airplay_title_and_artist_with_no_artwork_or_queue(self):
        metadata = {'xesam:title': {'data': 'Life Is Simple in the Moonlight'},
                    'xesam:artist': {'data': ['The Strokes']},
                    'xesam:album': {'data': 'Angles'}, 'mpris:artUrl': {'data': 'file:///secret'},
                    'mpris:length': {'data': 255261000}}
        status = dict(source='airplay', source_label='AirPlay', airplay={'active': True}, queue=['private'])
        song = publisher.describe(status, lambda name: metadata if name == 'Metadata' else 'Playing')
        self.assertEqual(song['title'], 'Life Is Simple in the Moonlight')
        self.assertEqual(song['artist'], 'The Strokes')
        self.assertEqual(song['duration'], 255.261)
        self.assertNotIn('queue', song)
        self.assertNotIn('cover', song)

    def test_tidal_paused_stopped_and_source_switch_clear_old_song(self):
        status = dict(source='tidal', source_label='Tidal', playback='pause', current={'title': 'Track', 'artist': 'Artist'})
        self.assertEqual(publisher.describe(status)['state'], 'paused')
        status['playback'] = 'stop'
        self.assertEqual(publisher.describe(status)['title'], '')
        status.update(source='airplay', airplay={'active': False})
        self.assertEqual(publisher.describe(status)['title'], '')
        status.update(source='usb:1', dsp_state='Running')
        self.assertEqual(publisher.describe(status)['title'], '')

    def test_airplay_missing_metadata_does_not_reuse_queued_tidal_song(self):
        status = dict(source='airplay', source_label='AirPlay', airplay={'active': True}, current={'title': 'Old Tidal track'})
        def offline(name):
            raise OSError('No receiver')
        song = publisher.describe(status, offline)
        self.assertEqual(song['title'], '')
        self.assertFalse(song['metadata_available'])

    def test_only_same_wifi_subnet_and_wifi_destination_allowed(self):
        wifi = [ipaddress.ip_interface('192.168.68.63/22')]
        self.assertTrue(publisher.allowed_peer('192.168.68.61', '192.168.68.63', wifi))
        for peer, destination in [('100.105.61.72', '192.168.68.63'), ('127.0.0.1', '192.168.68.63'),
                                  ('192.168.68.61', '100.100.177.81'), ('8.8.8.8', '192.168.68.63'),
                                  ('192.168.72.1', '192.168.68.63')]:
            self.assertFalse(publisher.allowed_peer(peer, destination, wifi))
        self.assertFalse(publisher.allowed_peer('192.168.68.61', '192.168.68.63', []))

    def test_snapshot_failure_never_returns_cached_song(self):
        cache = publisher.Snapshot()
        cache.value = {'title': 'Old song'}
        with patch.object(publisher, 'urlopen', side_effect=OSError('Offline')):
            with self.assertRaises(OSError):
                cache.read()

    def test_page_link_preserves_live_customizations_and_is_idempotent(self):
        app = 'customization<footer>Octet</footer>'
        updated = patch_page(app)
        self.assertIn('customization', updated)
        self.assertEqual(patch_page(updated), updated)
        with self.assertRaises(RuntimeError):
            patch_page('unexpected layout')

    def test_artwork_is_local_hashed_cached_and_cleared_on_source_change(self):
        data = b'\xff\xd8\xff' + b'cover bytes'
        cache = publisher.Artwork()
        with patch.object(publisher, 'read_artwork', return_value=(data, 'image/jpeg')) as read:
            path = cache.update('airplay', 'file:///cover')
            self.assertEqual(path, '/api/artwork/' + hashlib.sha256(data).hexdigest())
            self.assertEqual(cache.update('airplay', 'file:///cover'), path)
            read.assert_called_once()
            self.assertEqual(cache.get(path.rsplit('/', 1)[1]), (data, 'image/jpeg'))
            cache.update('tidal', '')
            self.assertIsNone(cache.get(path.rsplit('/', 1)[1]))

    def test_airplay_art_reads_only_the_cover_cache(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.object(publisher, 'ART_ROOT', Path(directory)):
            path = Path(directory) / 'cover-test.jpg'
            path.write_bytes(b'\xff\xd8\xffcover')
            self.assertEqual(publisher.read_artwork('airplay', 'file:///tmp/shairport-sync/.cache/coverart/cover-test.jpg'),
                             (b'\xff\xd8\xffcover', 'image/jpeg'))
            for uri in ('file:///etc/passwd', 'https://example.com/cover.jpg',
                        'file:///tmp/shairport-sync/.cache/coverart/../../secret'):
                with self.assertRaises(ValueError):
                    publisher.read_artwork('airplay', uri)

    def test_invalid_and_oversized_artwork_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch.object(publisher, 'ART_ROOT', Path(directory)):
            path = Path(directory) / 'cover-test.jpg'
            for data in (b'<svg>not allowed</svg>', b'\xff\xd8\xff' + b'x' * publisher.MAX_ART_BYTES):
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    publisher.read_artwork('airplay', 'file:///tmp/shairport-sync/.cache/coverart/cover-test.jpg')
        for uri in ('http://resources.tidal.com/images/a.jpg', 'https://evil.example/images/a.jpg',
                    'https://resources.tidal.com@evil.example/images/a.jpg'):
            with self.assertRaises(ValueError):
                publisher.read_artwork('tidal', uri)

    def test_missing_art_keeps_song_text_and_retries_without_reusing_old_cover(self):
        cache = publisher.Artwork()
        status = dict(source='tidal', source_label='Tidal', playback='play',
                      current={'title': 'Song', 'cover': 'https://resources.tidal.com/images/cover.jpg'})
        with patch.object(publisher, 'read_artwork', side_effect=OSError('Missing')) as read:
            song = publisher.describe(status, artwork=cache)
            self.assertEqual(song['title'], 'Song')
            self.assertIsNone(song['artwork'])
            publisher.describe(status, artwork=cache)
            read.assert_called_once()
        status['playback'] = 'stop'
        self.assertIsNone(publisher.describe(status, artwork=cache)['artwork'])


if __name__ == '__main__':
    unittest.main()
