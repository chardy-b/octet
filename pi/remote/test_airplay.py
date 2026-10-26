import json
import unittest
from unittest.mock import MagicMock, patch

import server
from test_audio_sources import config
from audio_sources import AIRPLAY, source_config
from airplay_control import AirPlayController


class AirPlayTests(unittest.TestCase):
    def setUp(self):
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        self.loaded = source_config(config(), AIRPLAY)
        self.dsp.call.side_effect = lambda name, *args: {
            'GetConfigFilePath': str(server.CHANNEL_PROFILE),
            'GetConfigJson': json.dumps(self.loaded), 'GetState': 'Running', 'GetVolume': -18.0,
        }.get(name)
        self.patches = [patch.object(server, 'DSP', return_value=self.dsp),
                        patch.object(server, 'select_source', return_value=None),
                        patch.object(server, 'mpd'),
                        patch.object(server.TUNING, 'status', return_value={'audio_owned': False}),
                        patch('airplay_control.loopback_available', return_value=True),
                        patch('airplay_control.receiver_running', return_value=True),
                        patch('airplay_control.pcm_running', return_value=True)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.control = AirPlayController(server)

    def calls(self):
        return [call.args for call in self.dsp.call.call_args_list]

    def test_begin_waits_for_pcm_then_sets_safe_master_once(self):
        self.control.event('start', now=0)
        self.assertNotIn(('SetMute', False), self.calls())
        self.control.tick(now=1)
        self.assertEqual(self.calls()[-2:], [('SetVolume', -18.0), ('SetMute', False)])
        self.control.event('start', now=2)
        self.assertEqual(self.calls().count(('SetVolume', -18.0)), 1)
        server.mpd.assert_called_once_with('pause 1')

    def test_end_debounce_stale_stop_and_manual_override(self):
        self.control.event('start', now=0)
        self.control.tick(now=1)
        self.control.event('stop', now=2)
        self.control.tick(now=4)  # live PCM: stale stop
        self.assertTrue(self.control.active)
        self.control.event('stop', now=5)
        with patch('airplay_control.pcm_running', return_value=False):
            self.control.tick(now=7)
        self.assertFalse(self.control.active)
        self.assertEqual(self.calls()[-1], ('SetMute', True))
        self.control.event('start', now=8)
        self.control.invalidate()
        self.control.tick(now=9)
        self.assertEqual(self.calls().count(('SetMute', False)), 1)

    def test_tuning_and_failed_prerequisites_never_unmute(self):
        for failure in ('tuning', 'device', 'receiver', 'preset', 'reload'):
            with self.subTest(failure=failure):
                self.dsp.call.reset_mock()
                target = {'tuning': patch.object(server.TUNING, 'status', return_value={'audio_owned': True}),
                          'device': patch('airplay_control.loopback_available', return_value=False),
                          'receiver': patch('airplay_control.receiver_running', return_value=False),
                          'preset': patch.object(server, 'approved', side_effect=RuntimeError('preset')),
                          'reload': patch.object(server, 'select_source', side_effect=RuntimeError('reload'))}[failure]
                with target, self.assertRaises(RuntimeError):
                    self.control.event('start', now=0)
                self.assertNotIn(('SetMute', False), self.calls())
                self.assertIn(('SetMute', True), self.calls())

    def test_tuning_wins_before_readiness_and_receiver_crash_mutes(self):
        self.control.event('start', now=0)
        with patch.object(server.TUNING, 'status', return_value={'audio_owned': True}):
            self.control.tick(now=1)
        self.assertNotIn(('SetMute', False), self.calls())
        self.control.event('start', now=2)
        self.control.tick(now=3)
        with patch('airplay_control.receiver_running', return_value=False):
            self.control.tick(now=4)
        self.assertFalse(self.control.active)
        self.assertEqual(self.calls()[-1], ('SetMute', True))

class IntegrationSafetyTests(AirPlayTests):
    def test_mute_failure_is_retried_without_losing_rollback(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'channel-dsp.yml'
            path.write_bytes(b'new')
            with patch.object(server, 'CHANNEL_PROFILE', path), patch.object(server, 'approved'):
                self.control.event('start', now=0)
                self.control.previous = b'old'
                with patch.object(self.control, 'mute', side_effect=RuntimeError('offline')):
                    with self.assertRaises(RuntimeError):
                        self.control.fail('offline')
                self.control.tick(now=1)
                self.assertEqual(path.read_bytes(), b'old')
                self.assertFalse(self.control.needs_mute)
                self.assertNotIn(('SetMute', False), self.calls())

    def test_status_does_not_treat_selected_source_as_active_cast(self):
        with patch.object(server, 'AIRPLAY_CONTROL', self.control), \
             patch.object(server, 'discover_sources', return_value=[AIRPLAY]), \
             patch.object(server, 'mpd', return_value=[[], [], []]), \
             patch.object(server.TUNING, 'active', return_value=None):
            result = server.current_status()
            self.assertEqual(result['source'], 'airplay')
            self.assertFalse(result['airplay']['active'])
            self.control.event('start', now=0)
            self.assertTrue(server.current_status()['airplay']['preparing'])
            self.control.tick(now=1)
            self.assertTrue(server.current_status()['airplay']['active'])

    def test_manual_protection_edit_revokes_pending_rollback(self):
        self.control.event('start', now=0)
        self.control.previous = b'old'
        with patch.object(server, 'AIRPLAY_CONTROL', self.control), \
             patch.object(server, 'set_channel_control'), \
             patch.object(server, 'current_status', return_value={}):
            server.action('channel-mute', {'output': 2, 'muted': True})
        self.control.tick(now=11)
        self.assertIsNone(self.control.previous)
        self.assertNotIn(('SetMute', False), self.calls())

    def test_rapid_stop_start_reapplies_safe_master(self):
        self.control.event('start', now=0)
        self.control.tick(now=1)
        self.control.event('stop', now=2)
        self.control.event('start', now=2.1)
        self.assertFalse(self.control.active)
        self.control.tick(now=2.2)
        self.assertEqual(self.calls().count(('SetVolume', -18.0)), 2)

    def test_manual_override_rejects_duplicate_live_start(self):
        self.control.event('start', now=0)
        self.control.tick(now=1)
        self.control.invalidate()
        self.control.event('start', now=2)
        self.control.tick(now=3)
        self.assertFalse(self.control.active)
        self.assertEqual(self.calls().count(('SetMute', False)), 1)
        with patch('airplay_control.pcm_running', return_value=False):
            self.control.tick(now=4)
        self.control.event('start', now=5)
        self.control.tick(now=6)
        self.assertTrue(self.control.active)

    def test_timeout_restores_previous_file_and_stays_muted(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'channel-dsp.yml'
            path.write_bytes(b'new config')
            with patch.object(server, 'CHANNEL_PROFILE', path), patch.object(server, 'approved'):
                self.control.event('start', now=0)
                self.control.previous = b'previous config'
                with patch('airplay_control.pcm_running', return_value=False):
                    self.control.tick(now=11)
                self.assertEqual(path.read_bytes(), b'previous config')
        self.assertNotIn(('SetMute', False), self.calls())

    def test_changed_protection_prevents_unmute(self):
        import copy
        self.control.event('start', now=0)
        self.control.expected = copy.deepcopy(self.loaded)
        self.loaded['filters']['output_2_polarity']['parameters']['mute'] = True
        self.control.tick(now=1)
        self.assertNotIn(('SetMute', False), self.calls())

    def test_boot_with_persisted_airplay_only_mutes(self):
        server.initialize_dsp()
        self.assertIn(('SetMute', True), self.calls())
        self.assertNotIn(('SetMute', False), self.calls())

    def test_source_and_tuning_actions_revoke_pending_unmute(self):
        for name, data in [('source', {'id': 'tidal'}), ('auto-start', {})]:
            with self.subTest(name=name):
                self.control.event('start', now=0)
                with patch.object(server, 'AIRPLAY_CONTROL', self.control), \
                     patch.object(server, 'current_status', return_value={}), \
                     patch.object(server.TUNING, 'request'), patch.object(server.INPUT_LEVELS, 'release'):
                    server.action(name, data)
                self.control.tick(now=1)
                self.assertFalse(self.control.active)

    def test_peer_uid_and_exact_command_are_required(self):
        import os
        import socket
        from airplay_control import handle_connection
        for uid, command, accepted in [(os.getuid(), b'start\n', True),
                                        (os.getuid() + 1, b'start\n', False),
                                        (os.getuid(), b'start extra\n', False),
                                        (os.getuid(), b'volume\n', False)]:
            with self.subTest(command=command, uid=uid):
                client, connection = socket.socketpair()
                with client, connection:
                    client.sendall(command)
                    controller = MagicMock()
                    handle_connection(connection, controller, uid)
                    self.assertEqual(client.recv(16), b'OK\n' if accepted else b'ERROR\n')
                    self.assertEqual(controller.event.called, accepted)


class HookSocketTests(unittest.TestCase):
    def test_socket_is_group_accessible_before_listening(self):
        import airplay_control
        with patch('airplay_control.pwd.getpwnam') as user, \
             patch('airplay_control.grp.getgrnam') as group, \
             patch('airplay_control.socket.socket') as socket_factory, \
             patch('airplay_control.Path.unlink'), \
             patch('airplay_control.os.chown') as chown, \
             patch('airplay_control.os.chmod') as chmod:
            user.return_value.pw_uid = 150
            group.return_value.gr_gid = 160
            socket_factory.return_value.listen.side_effect = RuntimeError('stop after bind')
            with self.assertRaisesRegex(RuntimeError, 'stop after bind'):
                airplay_control.serve(MagicMock())
            group.assert_called_once_with('octet-airplay')
            chown.assert_called_once_with(airplay_control.SOCKET, -1, 160)
            chmod.assert_called_once_with(airplay_control.SOCKET, 0o660)
            socket_factory.return_value.bind.assert_called_once_with(airplay_control.SOCKET)

    def test_failed_group_assignment_never_listens(self):
        import airplay_control
        with patch('airplay_control.pwd.getpwnam'), \
             patch('airplay_control.grp.getgrnam'), \
             patch('airplay_control.socket.socket') as socket_factory, \
             patch('airplay_control.Path.unlink') as unlink, \
             patch('airplay_control.os.chown', side_effect=PermissionError('group')):
            with self.assertRaises(PermissionError):
                airplay_control.serve(MagicMock())
            socket_factory.return_value.listen.assert_not_called()
            socket_factory.return_value.close.assert_called_once()
            self.assertEqual(unlink.call_count, 2)


class PCMReadinessTests(unittest.TestCase):
    def test_output_only_stalled_capture_and_parameters_fail_closed(self):
        from pathlib import Path
        import tempfile
        import airplay_control
        from test_audio_sources import loopback_fixture
        for failure in ('output-only', 'stalled', 'hardware-only', 'rate', 'format', 'channels', 'missing', 'progress'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                card = loopback_fixture(Path(directory), 'RUNNING')
                playback = card / 'pcm0p/sub1/status'
                capture = card / 'pcm1c/sub1/status'
                if failure == 'output-only':
                    capture.write_text('closed')
                if failure in ('rate', 'format', 'channels'):
                    capture.with_name('hw_params').write_text({
                        'rate': 'format: S32_LE\nchannels: 2\nrate: 48000',
                        'format': 'format: S16_LE\nchannels: 2\nrate: 44100',
                        'channels': 'format: S32_LE\nchannels: 1\nrate: 44100'}[failure])
                if failure == 'missing':
                    capture.unlink()
                def advance(_):
                    if failure in ('progress', 'hardware-only'):
                        capture.write_text('state: RUNNING\nowner_pid: 123\nhw_ptr: 200\nappl_ptr: '
                                           + ('200' if failure == 'progress' else '100'))
                with patch.object(airplay_control, 'PLAYBACK', playback), \
                     patch.object(airplay_control, 'CAPTURE', capture), \
                     patch.object(airplay_control.time, 'sleep', side_effect=advance):
                    self.assertEqual(airplay_control.pcm_running(), failure == 'progress')


class PreparationDeadlineTests(AirPlayTests):
    def test_unacknowledged_preparation_never_unmutes(self):
        import threading
        cancelled, acknowledged = threading.Event(), threading.Event()
        self.control.event('start', now=0, cancelled=cancelled, acknowledged=acknowledged)
        self.control.tick(now=1)
        self.assertNotIn(('SetMute', False), self.calls())
        cancelled.set()
        self.control.tick(now=2)
        self.assertIsNone(self.control.pending)
        self.assertNotIn(('SetMute', False), self.calls())

    def test_timed_out_preparation_cannot_later_unmute(self):
        import os
        import socket
        import threading
        import airplay_control
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return None
        original = self.control.event
        def event(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()
        client, connection = socket.socketpair()
        with client, connection, patch.object(server, 'select_source', side_effect=blocked), \
             patch.object(self.control, 'event', side_effect=event), \
             patch.object(airplay_control, 'PREPARATION_TIMEOUT', .05):
            client.sendall(b'start\n')
            airplay_control.handle_connection(connection, self.control, os.getuid())
            self.assertTrue(entered.is_set())
            self.assertEqual(client.recv(16), b'ERROR\n')
            self.assertIn(('SetMute', True), self.calls())
            release.set()
            self.assertTrue(finished.wait(2))
            self.control.tick()
            self.assertFalse(self.control.active)
            self.assertIsNone(self.control.pending)
            self.assertNotIn(('SetMute', False), self.calls())


if __name__ == "__main__":
    unittest.main()
