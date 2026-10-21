import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import server


class PlaybackGuards(unittest.TestCase):
    def dsp(self, config=server.PROFILE):
        mock = MagicMock()
        mock.__enter__.return_value = mock
        mock.call.side_effect = lambda command, *args: config if command == 'GetConfigFilePath' else json.dumps({'devices': {'capture': {'device': 'hw:Loopback,1,0'}}}) if command == 'GetConfigJson' else None
        return mock

    def test_volume_rejects_excessive_or_nonfinite_levels(self):
        with patch.object(server, 'DSP') as dsp:
            for level in (-61, -17, 0, float('inf'), float('nan')):
                with self.subTest(level=level), self.assertRaises(ValueError):
                    server.action('volume', {'db': level})
            dsp.assert_not_called()

    def test_unmute_rejects_old_three_way_configuration(self):
        dsp = self.dsp('/opt/camilladsp/configs/3way-lr4.yml')
        with patch.object(server, 'DSP', return_value=dsp):
            with self.assertRaises(RuntimeError):
                server.action('mute', {'muted': False})
        self.assertNotIn(('SetMute', False), [tuple(c.args) for c in dsp.call.call_args_list])

    def test_mute_is_allowed_even_with_wrong_configuration(self):
        dsp = self.dsp('other.yml')
        with patch.object(server, 'DSP', return_value=dsp), patch.object(server, 'current_status', return_value={}):
            server.action('mute', {'muted': True})
        dsp.call.assert_called_once_with('SetMute', True)

    def test_failed_play_mutes_again(self):
        dsp = self.dsp()
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'STATE', Path(tmp)), \
             patch.object(server, 'CATALOG', {'123': {'id': '123'}}), \
             patch.object(server, 'DSP', return_value=dsp), \
             patch.object(server, 'mpd', side_effect=[[[('Id', '9')]], RuntimeError('offline')]):
            with self.assertRaises(RuntimeError):
                server.action('play', {'id': '123'})
        self.assertEqual(dsp.call.call_args.args, ('SetMute', True))

    def test_invalid_track_cannot_enter_mpd_command(self):
        for identifier in ('../123', '1\nstop', '-1', 'http://example.com', '1;stop'):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                server.track_id(identifier)

    def test_mpd_arguments_reject_command_injection(self):
        for value in ('x\nplay', 'x\rstop', 'x\0stop'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.quote(value)

    def test_both_existing_plugin_and_flat_session_formats(self):
        self.assertEqual(server.decode_credentials({'token_type': {'data': 'Bearer'}, 'is_pkce': {'data': False}}),
                         {'token_type': 'Bearer', 'is_pkce': False})
        self.assertEqual(server.decode_credentials({'token_type': 'Bearer'}), {'token_type': 'Bearer'})


if __name__ == '__main__':
    unittest.main()
