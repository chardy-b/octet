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
            'GetMute': True,
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


class BaselineAirPlayTests(unittest.TestCase):
    """Real approval/digest/files; simulated documented Camilla WebSocket API."""
    def setUp(self):
        import copy
        import tempfile
        from pathlib import Path
        import yaml
        import airplay_baseline
        from tuning_proxy import TuningProxy
        self.yaml = yaml
        self.copy = copy.deepcopy
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # TMPDIR may contain symlinks; baseline fixtures need canonical paths.
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / 'dsp'
        self.storage.mkdir()
        tuning_root = self.root / 'autotune'
        (tuning_root / 'baselines').mkdir(parents=True)
        self.original = tuning_root / 'baselines' / 'before-20261026-041307-327269.yml'
        self.config = config()
        # Distinctive coefficients, gains, polarity/mute and output route.
        for i in range(129 - len(self.config['filters'])):
            name = f'baseline_only_{i}'
            self.config['filters'][name] = dict(type='Gain', parameters=dict(gain=-i / 100))
            self.config['pipeline'].append(dict(type='Filter', channels=[i % 8], names=[name]))
        self.config['devices']['playback']['device'] = 'hw:DistinctiveDAC8x'
        self.original.write_text('# Immutable Auto Tune baseline\n' + yaml.safe_dump(self.config))
        self.original_bytes = self.original.read_bytes()
        self.sha = airplay_baseline.digest(self.original_bytes)
        (tuning_root / 'active.json').write_text(json.dumps(dict(config_path=str(self.original), sha256=self.sha, name='Auto Tune baseline')))
        self.lock_path = tuning_root / 'audio.lock'
        self.lock_path.write_bytes(b'lock sentinel')
        self.lock_path.chmod(0o640)
        self.tuning = TuningProxy(tuning_root)
        self.owned = False
        self.path = str(self.original)
        self.loaded = self.copy(self.config)
        self.calls = []
        self.reload_failure = False
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        def call(name, *args):
            self.calls.append((name, *args))
            if name == 'GetConfigFilePath':
                return self.path
            if name == 'GetConfigJson':
                return json.dumps(self.loaded)
            if name == 'GetState':
                return 'Running'
            if name == 'GetVolume':
                return -18
            if name == 'GetMute':
                return True
            if name == 'ReadConfig':
                return args[0]
            if name == 'SetConfigFilePath':
                # Journal must already exist before ANY path change.
                self.assertTrue((self.storage / 'airplay-recovery.json').exists())
                self.path = args[0]
            if name == 'Reload':
                if self.reload_failure:
                    raise RuntimeError('reload failed')
                self.loaded = yaml.safe_load(Path(self.path).read_bytes())
        self.dsp.call.side_effect = call
        for item in [patch.object(airplay_baseline, 'ROOT', self.storage),
                     patch.object(server, 'DSP', return_value=self.dsp),
                     patch.object(server, 'TUNING', self.tuning),
                     patch.object(self.tuning, 'status', side_effect=lambda: {'audio_owned': self.owned}),
                     patch.object(server, 'mpd'), patch.object(server.INPUT_LEVELS, 'release'),
                     patch('airplay_baseline.subprocess.run', return_value=MagicMock(returncode=0)),
                     patch('airplay_control.loopback_available', return_value=True),
                     patch('airplay_control.receiver_running', return_value=True),
                     patch('airplay_control.pcm_running', return_value=True)]:
            item.start()
            self.addCleanup(item.stop)
        self.new_controller()

    def new_controller(self):
        if hasattr(self, 'control'):
            # Simulate process exit: the kernel closes the old lease descriptor.
            self.control.baseline.release()
        self.control = AirPlayController(server)
        self.addCleanup(self.control.baseline.release)

    def assert_lock_held(self):
        import fcntl
        with self.lock_path.open('rb') as competitor:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def assert_lock_free(self):
        import fcntl
        with self.lock_path.open('rb') as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_lock_covers_approval_mutation_cast_duplicates_and_cleanup(self):
        active = self.tuning.active
        call = self.dsp.call.side_effect
        def approval(path):
            self.assert_lock_held()
            return active(path)
        def dsp_call(name, *args):
            if name in ('SetConfigFilePath', 'Reload'):
                self.assert_lock_held()
            return call(name, *args)
        with patch.object(self.tuning, 'active', side_effect=approval), \
             patch.object(self.dsp, 'call', side_effect=dsp_call):
            self.control.event('start', now=0)
            self.control.tick(now=1)
            self.control.event('start', now=2)
            self.assert_lock_held()
            self.control.event('stop', now=3)
            self.control.event('start', now=3.5)
            self.control.tick(now=4)
            self.assert_lock_held()
            self.control.end()
        self.restored()
        self.assert_lock_free()
        self.assertEqual(self.lock_path.read_bytes(), b'lock sentinel')
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o640)

    def test_busy_lock_blocks_start_and_startup_recovery_muted(self):
        import fcntl
        with self.lock_path.open('rb') as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'audio lock'):
                self.control.event('start', now=0)
            self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.control.event('start', now=1)
        self.new_controller()
        self.calls.clear()
        with self.lock_path.open('rb') as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(server, 'AIRPLAY_CONTROL', self.control):
                server.initialize_dsp()
            self.assertTrue(self.control.needs_mute)
            self.assertTrue(self.control.baseline.journal.exists())
            self.assertIn(('SetMute', True), self.calls)
            self.assertNotIn(('SetMute', False), self.calls)
            self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.control.tick(now=2)
        self.restored()
        self.assert_lock_free()

    def test_missing_symlink_and_replaced_lock_fail_closed(self):
        saved = self.lock_path.with_name('saved.lock')
        self.lock_path.rename(saved)
        with self.assertRaisesRegex(RuntimeError, 'audio lock'):
            self.control.event('start', now=0)
        self.lock_path.symlink_to(saved)
        with self.assertRaisesRegex(RuntimeError, 'audio lock'):
            self.control.event('start', now=1)
        self.lock_path.unlink()
        saved.rename(self.lock_path)
        self.control.event('start', now=2)
        self.lock_path.rename(saved)
        self.lock_path.write_bytes(b'replacement')
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.control.tick(now=3)
        self.assertTrue(self.control.baseline.journal.exists())
        self.assertIsNotNone(self.control.baseline._lock_fd)
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.assertNotIn(('SetMute', False), self.calls)
        self.lock_path.unlink()
        saved.rename(self.lock_path)
        self.control.tick(now=4)
        self.restored()
        self.assert_lock_free()

    def test_prejournal_failure_releases_but_pending_failure_retains_lock(self):
        with patch('airplay_baseline.subprocess.run', return_value=MagicMock(returncode=1)):
            with self.assertRaises(RuntimeError):
                self.control.event('start', now=0)
        self.assert_lock_free()
        self.control.event('start', now=1)
        self.reload_failure = True
        with self.assertRaises(RuntimeError):
            self.control.end()
        self.assert_lock_held()
        self.reload_failure = False
        self.control.tick(now=2)
        self.restored()
        self.assert_lock_free()

    def unchanged(self):
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(self.tuning.active(str(self.original))['sha256'], self.sha)

    def restored(self):
        self.assertEqual(self.path, str(self.original))
        self.assertEqual(self.loaded, self.config)
        self.assertFalse(self.control.baseline.exists())
        self.assertFalse(self.control.baseline.candidate.exists())
        self.assertEqual([c for c in self.calls if c[0] == 'SetMute'][-1], ('SetMute', True))
        self.unchanged()

    def test_symlink_baseline_is_rejected(self):
        linked = self.original.with_name('linked.yml')
        linked.symlink_to(self.original)
        self.control.baseline.record = dict(version=1, original=str(linked), sha256=self.sha)
        with self.assertRaisesRegex(RuntimeError, 'AirPlay recovery path is not an Auto Tune baseline'):
            self.control.baseline.original()
        self.unchanged()

    def test_129_filters_exact_pipeline_playback_and_stop_restore(self):
        self.control.event('start', now=0)
        self.assertEqual(len(self.loaded['filters']), 129)
        for key in self.config:
            if key != 'devices':
                self.assertEqual(self.loaded[key], self.config[key])
        allowed = {'capture', 'resampler', 'enable_rate_adjust', 'capture_samplerate', 'target_level', 'adjust_period'}
        for key, value in self.config['devices'].items():
            if key not in allowed:
                self.assertEqual(self.loaded['devices'][key], value)
        self.assertEqual(self.control.baseline.candidate.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.control.baseline.journal.stat().st_mode & 0o777, 0o600)
        self.unchanged()
        self.assertNotIn(('SetMute', False), self.calls)
        self.control.tick(now=1)
        self.assertEqual(self.calls[-2:], [('SetVolume', -18.0), ('SetMute', False)])
        server.mpd.assert_called_once_with('pause 1')
        self.control.event('stop', now=2)
        with patch('airplay_control.pcm_running', return_value=False):
            self.control.tick(now=4)
        self.restored()

    def test_capture_timeout_and_receiver_failure_restore(self):
        for failure in ('pcm', 'receiver'):
            with self.subTest(failure=failure):
                self.control.event('start', now=0)
                target = 'pcm_running' if failure == 'pcm' else 'receiver_running'
                with patch('airplay_control.' + target, return_value=False):
                    self.control.tick(now=11)
                self.restored()
        self.assertNotIn(('SetMute', False), self.calls)

    def test_restart_recovers_before_new_cast(self):
        self.control.event('start', now=0)
        self.new_controller()
        with patch.object(server, 'AIRPLAY_CONTROL', self.control):
            server.initialize_dsp()
        self.restored()
        self.assertNotIn(('SetMute', False), self.calls)

    def test_restore_failure_retains_journal_and_retry_latch(self):
        self.control.event('start', now=0)
        self.reload_failure = True
        with self.assertRaises(RuntimeError):
            self.control.end()
        self.assertTrue(self.control.needs_mute)
        self.assertTrue(self.control.baseline.journal.exists())
        self.reload_failure = False
        self.control.tick(now=12)
        self.restored()

    def test_ownership_seizure_never_overwrites_new_config(self):
        self.control.event('start', now=0)
        self.control.tick(now=1)
        self.owned = True
        self.path = '/var/lib/octet-autotune/new-measurement.yml'
        self.loaded = {'measurement': True}
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.control.tick(now=2)
        self.assertIn(('SetMute', True), self.calls)
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.assertTrue(self.control.baseline.journal.exists())
        self.owned = False
        with self.assertRaises(RuntimeError):
            self.control.tick(now=3)
        self.assertEqual(self.loaded, {'measurement': True})
        self.assertTrue(self.control.needs_mute)
        # Recovery may proceed only once the owner returns the approved baseline.
        self.path, self.loaded = str(self.original), self.copy(self.config)
        self.control.tick(now=4)
        self.restored()

    def test_manual_auto_action_restores_before_handoff(self):
        self.control.event('start', now=0)
        with patch.object(server, 'AIRPLAY_CONTROL', self.control), patch.object(self.tuning, 'request') as request:
            request.side_effect = lambda *args: self.restored()
            server.action('auto-start', {})
            request.assert_called_once_with('start', {})
        self.assertTrue(self.control.suppressed)

    def test_loaded_difference_fails_closed_but_a_comment_edit_does_not(self):
        self.loaded['devices']['playback']['device'] = 'wrong'
        with self.assertRaises(RuntimeError):
            self.control.event('start', now=0)
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.loaded = self.copy(self.config)
        # Profiles are not checksum-checked: a byte change with the same settings plays.
        self.original.write_bytes(self.original_bytes + b'# changed\n')
        self.control.event('start', now=1)
        self.assertTrue(self.control.baseline.journal.exists())
        self.control.end()
        self.original.write_bytes(self.original_bytes)  # restored() also checks the file
        self.restored()

    def test_corrupt_or_oversized_journal_blocks_restart_and_manual_unmute(self):
        for content in (b'{}', b'x' * 8193):
            with self.subTest(size=len(content)):
                self.control.baseline.journal.write_bytes(content)
                self.new_controller()
                with patch.object(server, 'AIRPLAY_CONTROL', self.control):
                    server.initialize_dsp()
                    self.assertTrue(self.control.needs_mute)
                    with self.assertRaises(Exception):
                        server.action('mute', {'muted': False})
                self.assertTrue(self.control.baseline.journal.exists())
                self.assertNotIn(('SetMute', False), self.calls)

    def test_failed_journal_sync_never_switches_path(self):
        with patch('airplay_baseline.os.fdatasync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.control.event('start', now=0)
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.unchanged()

    def test_crash_at_journal_path_and_reload_boundaries(self):
        from airplay_baseline import persist
        for boundary in ('journal', 'path', 'reload'):
            with self.subTest(boundary=boundary):
                self.new_controller()
                normal = self.dsp.call.side_effect
                def crash_call(name, *args):
                    result = normal(name, *args)
                    if ((boundary == 'path' and name == 'SetConfigFilePath')
                            or (boundary == 'reload' and name == 'Reload')):
                        raise SystemExit('simulated process death')
                    return result
                def crash_persist(path, data, mode):
                    persist(path, data, mode)
                    if boundary == 'journal' and path.suffix == '.json':
                        raise SystemExit('simulated process death')
                with patch('airplay_baseline.persist', side_effect=crash_persist), \
                     patch.object(self.dsp, 'call', side_effect=crash_call):
                    with self.assertRaises(SystemExit):
                        self.control.event('start', now=0)
                self.assertTrue(self.control.baseline.journal.exists())
                self.new_controller()
                with patch.object(server, 'AIRPLAY_CONTROL', self.control):
                    server.initialize_dsp()
                self.restored()

    def test_unapproved_baseline_never_falls_back_to_channel_profile(self):
        # Not the profile selected on the DSP page.
        (self.tuning.root / 'active.json').write_text(json.dumps(dict(
            config_path=str(self.original.with_name('other.yml')), sha256=self.sha)))
        with self.assertRaises(RuntimeError):
            self.control.event('start', now=0)
        self.assertEqual(self.path, str(self.original))
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.assertNotIn(('SetMute', False), self.calls)

    def test_candidate_check_and_journal_directory_sync_fail_closed(self):
        from airplay_baseline import sync_directory
        with patch('airplay_baseline.subprocess.run', return_value=MagicMock(returncode=1)):
            with self.assertRaises(RuntimeError):
                self.control.event('start', now=0)
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        def sync(root):
            if self.control.baseline.journal.exists():
                raise OSError('directory sync failed')
            sync_directory(root)
        with patch('airplay_baseline.sync_directory', side_effect=sync):
            with self.assertRaises(OSError):
                self.control.event('start', now=0)
        # Recovery can select only the original, never an undurable candidate.
        self.assertFalse(any(c == ('SetConfigFilePath', str(self.control.baseline.candidate)) for c in self.calls))
        self.assertTrue(self.control.needs_mute)
        self.control.tick(now=1)
        self.restored()

    def test_readiness_requires_exact_config_and_capture_progress(self):
        self.control.event('start', now=0)
        expected = self.copy(self.loaded)
        self.loaded['filters']['unexpected'] = dict(type='Gain', parameters=dict(gain=10))
        self.control.tick(now=1)
        self.assertNotIn(('SetMute', False), self.calls)
        self.loaded = expected
        with patch('airplay_control.pcm_running', return_value=False):
            self.control.tick(now=2)
        self.assertNotIn(('SetMute', False), self.calls)
        self.control.tick(now=3)
        self.assertTrue(self.control.active)

    def test_manual_source_does_not_discard_failed_recovery(self):
        self.control.event('start', now=0)
        self.reload_failure = True
        with patch.object(server, 'AIRPLAY_CONTROL', self.control), \
             patch.object(server, 'select_source') as select:
            with self.assertRaises(RuntimeError):
                server.action('source', {'id': 'tidal'})
            select.assert_not_called()
        self.assertTrue(self.control.baseline.journal.exists())
        self.assertTrue(self.control.needs_mute)
        self.reload_failure = False
        self.control.tick(now=1)
        self.restored()


if __name__ == '__main__':
    unittest.main()
