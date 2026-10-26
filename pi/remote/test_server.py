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


class InitializationSafety(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.muted = False
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        self.dsp.call.side_effect = self.call
        self.control = MagicMock(needs_mute=False, error=None)
        self.control.recover.side_effect = self.recover
        for item in (patch.object(server, 'DSP', return_value=self.dsp),
                     patch.object(server, 'AIRPLAY_CONTROL', self.control),
                     patch.object(server, 'LEVELS_UNLOCKED'),
                     patch.object(server, 'ACTIVE_CAPTURE_CHANNELS')):
            item.start()
            self.addCleanup(item.stop)

    def call(self, name, *args):
        self.calls.append((name, *args))
        if name == 'SetMute':
            self.muted = args[0]
        if name == 'GetMute':
            return self.muted
        if name == 'GetVolume':
            return -18
        if name == 'GetConfigJson':
            return '{}'

    def recover(self):
        self.calls.append(('recover',))
        self.assertTrue(self.muted)
        self.muted = False  # A reload can change mute state.
        self.control.needs_mute = False

    def test_mute_is_verified_before_and_after_recovery(self):
        server.initialize_dsp()
        self.assertEqual(self.calls[:5], [
            ('SetMute', True), ('GetMute',), ('recover',),
            ('SetMute', True), ('GetMute',)])
        self.assertTrue(self.muted)

    def test_recovery_failure_still_remutes_and_retains_retry(self):
        def fail():
            self.recover()
            raise OSError('broken recovery journal')
        self.control.recover.side_effect = fail
        server.initialize_dsp()
        self.assertTrue(self.muted)
        self.assertEqual(self.calls[3:5], [('SetMute', True), ('GetMute',)])
        self.assertTrue(self.control.needs_mute)
        self.assertIn('recovery failed', self.control.error)

    def test_initial_mute_failure_skips_recovery_and_retries(self):
        for command in ('SetMute', 'GetMute'):
            with self.subTest(command=command):
                self.control.recover.reset_mock()
                failed = False
                def fail_once(name, *args):
                    nonlocal failed
                    if name == command and not failed:
                        failed = True
                        raise OSError('DSP unavailable')
                    return self.call(name, *args)
                self.dsp.call.side_effect = fail_once
                server.initialize_dsp()
                self.control.recover.assert_not_called()
                self.assertTrue(self.muted)
                self.assertTrue(self.control.needs_mute)

    def test_unconfirmed_mute_prevents_both_services_starting(self):
        for value in (False, None, 1):
            with self.subTest(value=value):
                self.control.recover.reset_mock()
                self.dsp.call.side_effect = lambda name, *args: value if name == 'GetMute' else self.call(name, *args)
                with patch.object(server.os, 'umask'), \
                     patch.object(server.Path, 'exists', return_value=True), \
                     patch.object(server.threading, 'Thread') as thread, \
                     patch.object(server, 'ThreadingHTTPServer') as http, \
                     self.assertRaisesRegex(RuntimeError, 'refusing remote startup'):
                    server.main()
                thread.assert_not_called()
                http.assert_not_called()
                self.control.recover.assert_not_called()
                self.assertTrue(self.control.needs_mute)

    def test_post_recovery_mute_failure_is_fatal(self):
        for command in ('SetMute', 'GetMute', 'connect', 'unmuted'):
            with self.subTest(command=command):
                self.calls.clear()
                def fail_after_recovery(name, *args):
                    if command == 'unmuted' and ('recover',) in self.calls and name == 'GetMute':
                        return False
                    if ('recover',) in self.calls and name == command:
                        raise OSError('DSP disconnected after reload')
                    return self.call(name, *args)
                self.dsp.call.side_effect = fail_after_recovery
                def connect():
                    if command == 'connect' and ('recover',) in self.calls:
                        raise OSError('connection refused')
                    return self.dsp
                with patch.object(server, 'DSP', side_effect=connect), \
                     self.assertRaisesRegex(RuntimeError, 'refusing remote startup'):
                    server.initialize_dsp()
                self.assertTrue(self.control.needs_mute)


if __name__ == '__main__':
    unittest.main()
