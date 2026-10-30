import json
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid
from urllib.parse import urlparse
import server


PLAYLIST = '11111111-2222-4333-8444-555555555555'


def track(number, available=True):
    return SimpleNamespace(id=number, name=f'Track {number}', available=available,
                           artists=[SimpleNamespace(name='Artist')], duration=120,
                           album=SimpleNamespace(name='Album', cover=None))


class Playlist:
    def __init__(self, tracks, identifier=PLAYLIST):
        self.id = identifier
        self.name = 'Test playlist'
        self.num_tracks = len(tracks)
        self.creator = SimpleNamespace(name='Listener')
        self.rows = tracks
        self.pages = []

    def image(self, size):
        raise AttributeError('No cover')

    def tracks(self, limit, offset):
        self.pages.append((limit, offset))
        return self.rows[offset:offset + limit]


class Player:
    def __init__(self):
        self.queue = [('71', 'old-one'), ('72', 'old-two')]
        self.current = '71'
        self.next_id = 100
        self.modes = dict(random='1', single='1', repeat='1', consume='1')
        self.adds = 0
        self.fail_add = None
        self.fail_play = False
        self.state = 'play'

    def __call__(self, *commands):
        results = []
        for command in commands:
            parts = shlex.split(command)
            name = parts[0]
            if name == 'status':
                status = [('state', self.state)]
                positions = [i for i, row in enumerate(self.queue) if row[0] == self.current]
                if positions:
                    status.extend([('song', str(positions[0])), ('songid', self.current)])
                results.append(status)
            elif name == 'playlistinfo':
                results.append([pair for identifier, url in self.queue
                                for pair in [('file', url), ('Id', identifier)]])
            elif name == 'addid':
                self.adds += 1
                if self.adds == self.fail_add:
                    raise RuntimeError('Queue load failed')
                identifier = str(self.next_id)
                self.next_id += 1
                if len(parts) > 2:
                    self.queue.insert(int(parts[2]), (identifier, parts[1]))
                else:
                    self.queue.append((identifier, parts[1]))
                results.append([('Id', identifier)])
            elif name == 'deleteid':
                self.queue = [row for row in self.queue if row[0] != parts[1]]
                results.append([])
            elif name == 'playid':
                if self.fail_play:
                    raise RuntimeError('Player offline')
                self.current = parts[1]
                self.state = 'play'
                results.append([])
            elif name == 'next':
                position = next(i for i, row in enumerate(self.queue) if row[0] == self.current) + 1
                if position < len(self.queue):
                    self.current = self.queue[position][0]
                else:
                    self.state = 'stop'
                results.append([])
            elif name in self.modes:
                self.modes[name] = parts[1]
                results.append([])
            else:
                raise AssertionError('Unexpected command: ' + command)
        return results


class Playlists(unittest.TestCase):
    def dsp(self):
        dsp = MagicMock()
        dsp.__enter__.return_value = dsp
        dsp.call.side_effect = lambda name, *args: str(server.CHANNEL_PROFILE) if name == 'GetConfigFilePath' else json.dumps({'devices': {'capture': {'device': 'hw:Loopback,1,0'}}}) if name == 'GetConfigJson' else False
        return dsp

    def contents(self):
        return dict(playlist=dict(id=PLAYLIST), tracks=[dict(id=str(i)) for i in (1, 2, 3)])

    def test_playlist_ids_reject_urls_and_command_text(self):
        for value in (None, True, '../playlist', PLAYLIST + '\nstop', 'https://tidal.com/playlist/' + PLAYLIST):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.playlist_id(value)
        self.assertEqual(server.playlist_id(PLAYLIST.upper()), PLAYLIST)

    def test_playlist_library_paginates_beyond_fifty(self):
        rows = [Playlist([], str(uuid.UUID(int=i + 1))) for i in range(51)]
        user = SimpleNamespace(playlist_and_favorite_playlists=lambda offset, limit: rows[offset:offset + limit])
        with patch.object(server, 'tidal', return_value=SimpleNamespace(user=user)):
            first = server.playlist_page()
            second = server.playlist_page(first['next_offset'])
        self.assertEqual(len(first['playlists']), 50)
        self.assertTrue(first['has_more'])
        self.assertEqual(len(second['playlists']), 1)
        self.assertFalse(second['has_more'])

    def test_all_track_pages_keep_order_skip_unavailable_and_cache(self):
        playlist = Playlist([track(i, available=i != 102) for i in range(1, 206)])
        session = SimpleNamespace(playlist=MagicMock(return_value=playlist))
        with patch.object(server, 'tidal', return_value=session), patch.object(server, 'PLAYLIST_CACHE', {}), patch.object(server, 'CATALOG', {}):
            contents = server.playlist_contents(PLAYLIST)
            self.assertEqual([row['id'] for row in contents['tracks']], [str(i) for i in range(1, 206) if i != 102])
            self.assertEqual(contents['unavailable'], 1)
            self.assertEqual(playlist.pages, [(100, 0), (100, 100), (100, 200)])
            self.assertIs(server.playlist_contents(PLAYLIST), contents)
            self.assertEqual(session.playlist.call_count, 1)
            server.playlist_contents(PLAYLIST, refresh=True)
            self.assertEqual(session.playlist.call_count, 2)

    def test_play_selected_song_replaces_queue_and_enables_following_tracks(self):
        player, dsp = Player(), self.dsp()
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'CATALOG', {}), patch.object(server, 'playlist_contents', return_value=self.contents()), patch.object(server, 'mpd', side_effect=player), patch.object(server, 'DSP', return_value=dsp), patch.object(server, 'current_status', return_value={}):
            server.action('play-playlist', dict(id=PLAYLIST, position=1, track_id='2'))
        self.assertEqual([urlparse(url).path.rsplit('/', 1)[-1] for _, url in player.queue], ['1', '2', '3'])
        self.assertEqual(player.current, player.queue[1][0])
        self.assertEqual(player.modes, dict(random='0', single='0', repeat='0', consume='0'))
        self.assertEqual(dsp.call.call_args.args, ('SetMute', False))
        self.assertNotIn('SetVolume', [call.args[0] for call in dsp.call.call_args_list])

    def test_play_all_starts_at_first_track(self):
        player = Player()
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'CATALOG', {}), patch.object(server, 'playlist_contents', return_value=self.contents()), patch.object(server, 'mpd', side_effect=player), patch.object(server, 'DSP', return_value=self.dsp()), patch.object(server, 'current_status', return_value={}):
            server.action('play-playlist', dict(id=PLAYLIST))
        self.assertEqual(player.current, player.queue[0][0])

    def test_invalid_selection_and_changed_playlist_do_not_touch_playback(self):
        with patch.object(server, 'playlist_contents', return_value=self.contents()), patch.object(server, 'mpd') as mpd:
            for position in (True, -1, 0.5, 3):
                with self.subTest(position=position), self.assertRaises(ValueError):
                    server.play_playlist(dict(id=PLAYLIST, position=position))
            with self.assertRaises(RuntimeError):
                server.play_playlist(dict(id=PLAYLIST, position=1, track_id='3'))
            mpd.assert_not_called()

    def test_queue_loading_failure_retains_old_music(self):
        player, dsp = Player(), self.dsp()
        player.fail_add = 2
        old = list(player.queue)
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'CATALOG', {}), patch.object(server, 'playlist_contents', return_value=self.contents()), patch.object(server, 'mpd', side_effect=player), patch.object(server, 'DSP', return_value=dsp):
            with self.assertRaises(RuntimeError):
                server.play_playlist(dict(id=PLAYLIST))
        self.assertEqual(player.queue, old)
        self.assertEqual(player.current, '71')
        self.assertNotIn('SetMute', [call.args[0] for call in dsp.call.call_args_list])

    def test_playback_failure_leaves_output_muted(self):
        player, dsp = Player(), self.dsp()
        player.fail_play = True
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), patch.object(server, 'CATALOG', {}), patch.object(server, 'playlist_contents', return_value=self.contents()), patch.object(server, 'mpd', side_effect=player), patch.object(server, 'DSP', return_value=dsp):
            with self.assertRaises(RuntimeError):
                server.play_playlist(dict(id=PLAYLIST))
        self.assertEqual(dsp.call.call_args.args, ('SetMute', True))


if __name__ == '__main__':
    unittest.main()
