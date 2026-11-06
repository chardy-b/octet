"""Successful Play confirms unmute; a failed unmute cannot silently keep playing."""
import unittest
from unittest.mock import patch

import server
from test_playlists import Player
import test_source_switching


class PlaybackMute(unittest.TestCase):
    def system(self):
        return test_source_switching.SourceSwitching().system()

    def player(self, *commands):
        result = []
        for command in commands:
            if command.startswith(('play', 'next', 'previous')):
                self.state = 'play'
            elif command == 'pause 1':
                self.state = 'pause'
            result.append([('state', self.state)] if command == 'status' else [])
        return result

    def test_transport_from_maintenance_mute_confirms_unmute(self):
        for action, data in (('resume', {}), ('next', {}), ('previous', {}),
                             ('position', {'song_id': '8'})):
            with self.subTest(action=action), self.system() as (_, _, _, dsp, *rest), \
                    patch.object(server, 'mpd', side_effect=self.player), \
                    patch.object(server, 'current_status', return_value={}):
                dsp.muted, self.state = True, 'pause'
                server.action(action, data)
                self.assertFalse(dsp.muted)
                self.assertEqual(self.state, 'play')
                self.assertEqual(dsp.calls[-2:], [('SetMute', False), ('GetMute', None)])

    def test_ignored_unmute_and_timeout_pause_playback_and_report_failure(self):
        for failure in ('ignored', 'timeout'):
            with self.subTest(failure=failure), self.system() as (_, _, _, dsp, *rest), \
                    patch.object(server, 'mpd', side_effect=self.player):
                dsp.muted, self.state = True, 'pause'
                original = dsp.call
                def broken(name, value=None):
                    if name == 'SetMute' and value is False:
                        if failure == 'timeout':
                            raise TimeoutError('DSP reply lost')
                        return
                    return original(name, value)
                dsp.call = broken
                with self.assertLogs(level='WARNING'), self.assertRaisesRegex(RuntimeError, 'Playback was paused'):
                    server.action('resume', {})
                self.assertEqual(self.state, 'pause')
                self.assertTrue(dsp.muted)

    def test_playlist_unmute_failure_does_not_leave_track_advancing(self):
        with self.system() as (origin, _, _, dsp, *rest), \
                patch.object(server, 'STATE', origin.parent), \
                patch.object(server, 'CATALOG', {}):
            player = Player()
            original = dsp.call
            dsp.call = lambda name, value=None: None if name == 'SetMute' and value is False else original(name, value)
            with patch.object(server, 'mpd', side_effect=player), self.assertLogs(level='WARNING'), \
                    self.assertRaisesRegex(RuntimeError, 'Playback was paused'):
                server.play_context([dict(id='1')], 0, dict(kind='track', id='1'))
            self.assertEqual(player.state, 'pause')
            self.assertTrue(dsp.muted)
