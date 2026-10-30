"""Playback order tests against a stateful MPD double, with no live audio."""
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlparse

import server
import test_playlists as fixtures

PLAYLIST = fixtures.PLAYLIST


class Queue(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.player = fixtures.Player()
        self.dsp = fixtures.Playlists().dsp()
        self.stack.enter_context(patch.object(server, 'STATE', Path(tmp)))
        self.stack.enter_context(patch.object(server, 'CATALOG', {str(i): dict(id=str(i), title=f'Track {i}') for i in range(1, 20)}))
        self.stack.enter_context(patch.object(server, 'mpd', side_effect=self.player))
        self.stack.enter_context(patch.object(server, 'DSP', return_value=self.dsp))
        self.stack.enter_context(patch.object(server.TUNING, 'status', return_value={}))
        self.stack.enter_context(patch.object(server, 'discover_sources', return_value=[dict(id='tidal')]))

    def start(self, ids=(1, 2, 3), position=0):
        return server.play_context([server.CATALOG[str(i)] for i in ids], position,
                                   dict(kind='playlist', id=PLAYLIST, title='Evening'))

    def upcoming(self):
        status = server.current_status()
        return status['current']['id'], [row['id'] for row in status['manual_queue']], [row['id'] for row in status['autoplay']]

    def test_queue_end_and_play_next_both_precede_autoplay(self):
        self.start()
        server.action('enqueue', dict(id='4'))
        server.action('enqueue', dict(id='5', placement='end'))
        server.action('enqueue', dict(id='6', placement='next'))
        server.action('enqueue', dict(id='7', placement='end'))
        self.assertEqual(self.upcoming(), ('1', ['6', '4', '5', '7'], ['2', '3']))
        played = ['1']
        for _ in range(6):
            # Natural advancement uses the same MPD order, with no status polling required.
            self.player('next')
            played.append(self.upcoming()[0])
        self.assertEqual(played, ['1', '6', '4', '5', '7', '2', '3'])
        self.assertEqual(self.upcoming(), ('3', [], []))

    def test_adding_while_manual_song_plays_keeps_remaining_context(self):
        self.start(position=1)
        server.action('enqueue', dict(id='4'))
        server.action('next', {})
        server.action('enqueue', dict(id='5'))
        server.action('enqueue', dict(id='6', placement='next'))
        self.assertEqual(self.upcoming(), ('4', ['6', '5'], ['3']))
        self.assertEqual(server.queue_context()['source']['title'], 'Evening')

    def test_duplicate_tracks_use_entry_ids_and_clear_preserves_current_and_autoplay(self):
        self.start()
        server.action('enqueue', dict(id='2'))
        server.action('enqueue', dict(id='2'))
        status = server.current_status()
        first, second = [row['song_id'] for row in status['manual_queue']]
        self.assertNotEqual(first, second)
        server.action('remove-queued', dict(song_id=first))
        self.assertEqual(self.upcoming(), ('1', ['2'], ['2', '3']))
        server.action('next', {})
        server.action('enqueue', dict(id='4'))
        server.action('clear-queue', {})
        self.assertEqual(self.upcoming(), ('2', [], ['2', '3']))
        self.assertEqual(self.player.state, 'play')

    def test_new_context_preserves_pending_manual_queue_and_discards_old_autoplay(self):
        self.start()
        server.action('enqueue', dict(id='4'))
        server.action('enqueue', dict(id='5'))
        server.action('next', {})
        self.start(ids=(6, 7, 8), position=1)
        self.assertEqual(self.upcoming(), ('7', ['5'], ['8']))
        self.assertEqual([urlparse(url).path.rsplit('/', 1)[-1] for _, url in self.player.queue], ['6', '7', '5', '8'])

    def test_single_song_starts_fresh_context_retains_requests_and_does_not_fake_radio(self):
        self.start()
        server.action('enqueue', dict(id='4'))
        server.action('play', dict(id='9'))
        self.assertEqual(self.upcoming(), ('9', ['4'], []))
        self.assertEqual(server.current_status()['autoplay_source']['kind'], 'track')

    def test_queue_metadata_survives_reload_and_ignores_reused_mpd_ids(self):
        self.start()
        server.action('enqueue', dict(id='4'))
        context = server.queue_context()
        self.assertTrue(context['manual'])
        self.assertEqual(self.upcoming(), ('1', ['4'], ['2', '3']))
        sid = next(iter(context['manual']))
        self.player.queue = [(key, url.replace('/4', '/9') if key == sid else url) for key, url in self.player.queue]
        self.assertEqual(self.upcoming(), ('1', [], ['9', '2', '3']))

    def test_mpd_restart_reassigned_ids_preserve_membership_and_duplicate_requests(self):
        self.start()
        server.action('enqueue', dict(id='2'))
        server.action('enqueue', dict(id='2'))
        self.assertEqual(len({url for _, url in self.player.queue}), 5)
        self.player.queue = [(str(i + 800), url) for i, (_, url) in enumerate(self.player.queue)]
        self.player.current = '800'
        self.assertEqual(self.upcoming(), ('1', ['2', '2'], ['2', '3']))
        self.assertEqual(server.current_status()['autoplay_source']['title'], 'Evening')
        server.action('enqueue', dict(id='4'))
        self.assertEqual(self.upcoming(), ('1', ['2', '2', '4'], ['2', '3']))
        server.action('remove-queued', dict(song_id='801'))
        server.action('clear-queue', {})
        self.assertEqual(self.upcoming(), ('1', [], ['2', '3']))

    def test_invalid_placement_and_stale_removal_do_not_change_player(self):
        self.start()
        before = list(self.player.queue)
        for placement in ('tail', True, None, '0\nstop'):
            with self.subTest(placement=placement), self.assertRaises(ValueError):
                server.action('enqueue', dict(id='4', placement=placement))
        for sid in ('999', '1\nstop', None):
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                server.action('remove-queued', dict(song_id=sid))
        self.assertEqual(self.player.queue, before)

    def test_album_selected_track_loads_all_pages_and_continues_in_order(self):
        album = fixtures.Playlist([fixtures.track(i) for i in range(1, 206)])
        session = SimpleNamespace(album=lambda identifier: album)
        with patch.object(server, 'tidal', return_value=session):
            result = server.action('play-album', dict(id='99', position=101, track_id='102'))
        self.assertEqual(album.pages, [(100, 0), (100, 100), (100, 200)])
        self.assertEqual(result['current']['id'], '102')
        self.assertEqual(result['autoplay'][0]['id'], '103')
        self.assertEqual(len(result['autoplay']), 103)
        self.assertEqual(result['autoplay_source']['kind'], 'album')
        self.player('next')
        self.assertEqual(server.current_status()['current']['id'], '103')

    def test_paused_and_empty_queue_enqueues_do_not_start_or_unmute(self):
        self.player.queue = []
        self.player.current = None
        self.player.state = 'stop'
        server.action('enqueue', dict(id='4'))
        self.assertEqual(self.player.state, 'stop')
        self.assertEqual(server.current_status()['manual_queue'][0]['id'], '4')
        self.assertNotIn('SetMute', [call.args[0] for call in self.dsp.call.call_args_list])


if __name__ == '__main__':
    unittest.main()
