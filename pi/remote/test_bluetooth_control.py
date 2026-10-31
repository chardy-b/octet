import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml
from airplay_baseline import digest
from bluetooth_control import BluetoothControl
from bluetooth_transport import StreamGeneration
import test_bluetooth_contract


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'baselines').mkdir()
        (self.root / 'audio.lock').touch()
        self.config = test_bluetooth_contract.ContractTests().baseline()
        self.path = self.root / 'baselines/approved.yml'
        self.data = b'# preserve exact comments and bytes\n' + yaml.safe_dump(self.config).encode()
        self.path.write_bytes(self.data)
        self.state = dict(path=str(self.path), config=copy.deepcopy(self.config), mute=False, running='Running')
        self.calls = []
        self.unreachable = False
        self.ignore_mute = False
        self.bad_reload = False
        self.dsp = Mock()
        self.dsp.__enter__ = Mock(return_value=self.dsp)
        self.dsp.__exit__ = Mock(return_value=False)
        self.dsp.call.side_effect = self.call
        self.server = SimpleNamespace(LOCK=threading.RLock(), DSP=lambda: self.dsp,
            CHANNEL_PROFILE=self.root / 'channel-dsp.yml', select_source=Mock(),
            TUNING=SimpleNamespace(root=self.root, active=lambda p: {'sha256': digest(self.data)},
                                   status=lambda: {'audio_owned': False}),
            AIRPLAY_CONTROL=SimpleNamespace(active=False, pending=None, restore_pending=None,
                                            needs_mute=False, hook_busy=threading.Lock(),
                                            baseline=SimpleNamespace(exists=lambda: False)))
        self.stop_bridge = Mock()
        self.control = self.controller()
        self.addCleanup(self.control.route.release)

    def controller(self):
        control = BluetoothControl(self.server, self.stop_bridge)
        control.route.root = self.root
        return control

    def call(self, name, *args):
        self.calls.append((name, *args))
        if self.unreachable:
            raise RuntimeError('DSP unreachable')
        if name == 'SetMute' and not self.ignore_mute:
            self.state['mute'] = args[0]
        if name == 'SetConfigFilePath':
            self.assertTrue(self.state['mute'])
            self.assertTrue(self.control.route.journal.exists())
            self.state['path'] = args[0]
        if name == 'Reload' and not self.bad_reload:
            self.state['config'] = yaml.safe_load(Path(self.state['path']).read_bytes())
        if name == 'ReadConfig':
            return args[0]
        return {'GetMute': self.state['mute'], 'GetState': self.state['running'],
                'GetConfigFilePath': self.state['path'],
                'GetConfigJson': json.dumps(self.state['config'])}.get(name)

    def prepare(self):
        return self.control.route.begin(str(self.path), self.config)

    def tearDown(self):
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertFalse(any(c[0] == 'SetVolume' for c in self.calls))

    def test_startup_mutes_and_stops_without_unmute(self):
        self.control.startup()
        self.assertTrue(self.state['mute'])
        self.assertTrue(self.control.mute_verified)
        self.stop_bridge.assert_called_once()

    def test_unreachable_dsp_stops_bridge_no_physical_mute_claim(self):
        self.unreachable = True
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            self.control.startup()
        self.stop_bridge.assert_called_once()
        self.assertFalse(self.control.mute_verified)

    def test_mute_failure_prevents_journal_and_path_changes(self):
        self.ignore_mute = True
        with self.assertRaisesRegex(RuntimeError, 'mute'):
            self.prepare()
        self.assertFalse(self.control.route.exists())
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))

    def test_snapshot_is_exact_readonly_and_journal_precedes_any_path_change(self):
        self.prepare()
        self.assertEqual(self.control.route.snapshot.read_bytes(), self.data)
        self.assertEqual(self.control.route.snapshot.stat().st_mode & 0o777, 0o400)
        self.assertTrue(self.control.route.journal.exists())
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))

    def test_crash_recovery_restores_original_path_content_muted(self):
        self.prepare()
        self.control.route.release()  # process death closes the fd
        self.state.update(path=str(self.control.route.candidate), config={}, mute=False)
        recovered = self.controller()
        self.addCleanup(recovered.route.release)
        recovered.startup()
        self.assertEqual(self.state['path'], str(self.path))
        self.assertEqual(self.state['config'], self.config)
        self.assertEqual(self.path.read_bytes(), self.data)
        self.assertTrue(self.state['mute'])
        self.assertFalse(recovered.route.exists())

    def test_real_other_process_flock_contention_and_release(self):
        self.prepare()
        code = 'import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)'
        def contender():
            return subprocess.run([sys.executable, '-c', code, str(self.root / 'audio.lock')], capture_output=True)
        self.assertNotEqual(contender().returncode, 0)
        self.control.stop()
        self.assertEqual(contender().returncode, 0)

    def test_lock_owned_by_other_process_refuses_prepare(self):
        code = 'import fcntl,sys; f=open(sys.argv[1]); fcntl.flock(f,fcntl.LOCK_EX); print("held",flush=True); sys.stdin.read()'
        with subprocess.Popen([sys.executable, '-c', code, str(self.root / 'audio.lock')], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as child:
            try:
                self.assertEqual(child.stdout.readline(), b'held\n')
                with self.assertRaisesRegex(RuntimeError, 'lock unavailable'):
                    self.prepare()
            finally:
                child.communicate(b'')
        self.assertFalse(self.control.route.exists())

    def test_running_config_drift_recovers_muted(self):
        generation = self.prepare()
        self.state['config']['filters']['0']['gain'] = 999
        self.state['mute'] = False
        self.control.tick()
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['config'], self.config)
        self.assertFalse(self.control.route.exists())

    def test_orphan_tick_and_stop_recover_start_refuses_pending_or_unpaired(self):
        for event in ('tick', 'stop'):
            with self.subTest(event=event):
                self.prepare()
                getattr(self.control, event)()
                self.assertFalse(self.control.route.exists())
                self.assertTrue(self.state['mute'])
        self.prepare()
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'recovery already pending'):
            self.control.start('12:34:56:78:9A:BC')
        self.assertEqual(self.calls, [])
        self.control.stop()
        self.calls.clear()
        # The default is every phone paired with hci0; with none paired, start refuses.
        from bluetooth_identity import paired_addresses
        self.assertIs(self.control.policy, paired_addresses)
        def none_paired():
            raise RuntimeError('No paired Bluetooth phone')
        self.control.policy = none_paired
        with self.assertRaisesRegex(RuntimeError, 'No paired'):
            self.control.start('12:34:56:78:9A:BC')
        self.assertEqual(self.calls, [])

    def test_failed_restore_retains_journal_and_lock(self):
        self.prepare()
        self.state['config'] = {}
        self.bad_reload = True
        with patch('bluetooth_route.time.sleep'), self.assertRaisesRegex(RuntimeError, 'restore not verified'):
            self.control.stop()
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()
        self.assertTrue(self.state['mute'])

    def test_replaced_lock_retains_recovery(self):
        self.prepare()
        lock = self.root / 'audio.lock'
        lock.rename(self.root / 'old.lock')
        lock.touch()
        with self.assertRaises(RuntimeError):
            self.control.stop()
        self.assertTrue(self.control.route.journal.exists())
        self.assertTrue(self.state['mute'])

    def test_profile_bytes_are_not_checksum_checked(self):
        # A comment edit keeps the same settings: recovery still restores.
        self.prepare()
        self.path.chmod(0o600)
        self.path.write_bytes(self.data + b'# edited\n')
        self.control.stop()
        self.assertFalse(self.control.route.exists())
        self.assertTrue(self.state['mute'])

    def test_airplay_autotune_and_unapproved_baselines_refused(self):
        self.server.AIRPLAY_CONTROL.active = True
        with self.assertRaisesRegex(RuntimeError, 'AirPlay'):
            self.prepare()
        self.server.AIRPLAY_CONTROL.active = False
        self.server.TUNING.status = lambda: {'audio_owned': True}
        with self.assertRaisesRegex(RuntimeError, 'Auto Tune'):
            self.prepare()
        self.server.TUNING.status = lambda: {'audio_owned': False}
        self.server.TUNING.active = lambda p: None
        with self.assertRaisesRegex(RuntimeError, 'active profile'):
            self.prepare()

    def test_profile_whose_filters_omit_defaults_is_accepted(self):
        # Live Oct 31: filters without "description" load as description: null.
        def with_defaults(config):
            config = copy.deepcopy(config)
            for item in config.get('filters', {}).values():
                item.setdefault('description', None)
            return config
        self.state['config'] = with_defaults(self.config)
        plain = self.call
        def call(name, *args):
            if name == 'ReadConfig':
                self.calls.append((name,))
                return yaml.safe_dump(with_defaults(yaml.safe_load(args[0])))
            result = plain(name, *args)
            if name == 'Reload' and not self.bad_reload:
                self.state['config'] = with_defaults(self.state['config'])
            return result
        self.dsp.call.side_effect = call
        self.control.route.begin(str(self.path), self.state['config'])
        self.assertEqual(self.control.route.original()[1], self.state['config'])
        self.control.stop()
        self.assertTrue(self.state['mute'])

    def test_source_working_copy_returns_to_the_approved_profile_first(self):
        from source_profiles import SourceProfiles
        profiles = SourceProfiles(self.root)
        working = profiles.target(self.path)
        working.write_bytes(self.data)
        profiles.save(working, self.path)
        self.state['path'] = str(working)
        self.server.TUNING.active = lambda p: {'sha256': digest(self.data)} if p == str(self.path) else None
        self.server.select_source.side_effect = lambda source: self.state.update(path=str(self.path))
        self.control._idle()
        self.server.select_source.assert_called_once_with('tidal')
        # AirPlay owning audio is refused before anything is switched.
        self.state['path'] = str(working)
        self.server.select_source.reset_mock()
        self.server.AIRPLAY_CONTROL.active = True
        with self.assertRaisesRegex(RuntimeError, 'AirPlay'):
            self.control._idle()
        self.server.select_source.assert_not_called()

    def test_real_server_airplay_interface_guards_bluetooth_ownership(self):
        import server
        from airplay_control import AirPlayController

        # Importing server does not run main or start any audio service.
        self.assertIsInstance(server.AIRPLAY_CONTROL, AirPlayController)
        self.assertIs(server.AIRPLAY_CONTROL.server, server)
        control = BluetoothControl(server, self.stop_bridge)
        control.route.root = self.root
        self.addCleanup(control.route.release)
        with patch.object(server, 'TUNING', self.server.TUNING), \
             patch.object(server, 'DSP') as dsp, \
             patch.object(server.AIRPLAY_CONTROL, 'active', False), \
             patch.object(server.AIRPLAY_CONTROL, 'pending', None), \
             patch.object(server.AIRPLAY_CONTROL.baseline, 'root', self.root):
            with server.LOCK:
                control.route.acquire()
                control.route.ownership()
                # CHANNEL_PROFILE casts own audio without any baseline journal.
                for field, value in (('active', True), ('pending', object()),
                                     ('restore_pending', b'previous channel profile'),
                                     ('needs_mute', True)):
                    with self.subTest(field=field), \
                         patch.object(server.AIRPLAY_CONTROL, field, value):
                        with self.assertRaisesRegex(RuntimeError, 'AirPlay owns audio'):
                            control.route.ownership()
                server.AIRPLAY_CONTROL.baseline.journal.write_text('{}')
                with self.assertRaisesRegex(RuntimeError, 'AirPlay owns audio'):
                    control.route.ownership()
            dsp.assert_not_called()
        self.stop_bridge.assert_not_called()

    def test_journal_write_failure_never_changes_path(self):
        from bluetooth_route import persist
        def fail_journal(path, data, mode):
            if path.suffix == '.json':
                raise OSError('injected journal failure')
            persist(path, data, mode)
        with patch('bluetooth_route.persist', side_effect=fail_journal), self.assertRaises(OSError):
            self.prepare()
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.assertFalse(self.control.route.exists())

    def test_stop_failure_defers_restore_and_retains_mute_and_evidence(self):
        self.prepare()
        candidate = str(self.control.route.candidate)
        self.state.update(path=candidate, config={})
        self.stop_bridge.side_effect = RuntimeError('owned bridge stop failed')
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'stop failed.*restore deferred'):
            self.control.stop()
        # Unconfirmed termination: muted, never restored or reloaded.
        self.assertEqual(self.calls, [('SetMute', True), ('GetMute',)])
        self.assertEqual((self.state['path'], self.state['config']), (candidate, {}))
        self.assertTrue(self.state['mute'])
        self.assertTrue(self.control.mute_verified)
        self.assertEqual(set(self.control.errors), {'stop', 'recovery'})
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()
        self.stop_bridge.side_effect = None
        self.control.stop()
        self.assertEqual(self.state['config'], self.config)
        self.assertFalse(self.control.route.exists())

    def test_all_failure_stages_captured_and_restore_never_attempted_after_stop_failure(self):
        self.prepare()
        self.unreachable = True
        self.stop_bridge.side_effect = RuntimeError('stop failed')
        with patch.object(self.control.route, 'recover', wraps=self.control.route.recover) as recover:
            with self.assertRaisesRegex(RuntimeError, 'mute:.*stop:.*recovery:.*deferred'):
                self.control.fail('injected')
            recover.assert_not_called()
        self.assertEqual(set(self.control.errors), {'mute', 'stop', 'recovery'})
        self.assertFalse(self.control.mute_verified)
        self.assertTrue(self.control.route.exists())
        self.control.route.check_lock()

    def test_mute_failure_alone_still_attempts_restore_without_cleanup(self):
        self.prepare()
        self.unreachable = True
        with patch.object(self.control.route, 'recover', wraps=self.control.route.recover) as recover:
            with self.assertRaisesRegex(RuntimeError, 'mute:.*recovery:'):
                self.control.fail('injected')
            recover.assert_called_once_with(cleanup=False)
        self.assertEqual(set(self.control.errors), {'mute', 'recovery'})
        self.assertTrue(self.control.route.exists())

    def test_invalid_journal_formats(self):
        self.prepare()
        original = self.control.route.journal.read_bytes()
        for field, value in [('sha256', None), ('sha256', 'g' * 64),
                             ('sha256', 'A' * 64), ('generation', 'z' * 32),
                             ('generation', '0' * 31 + '\n'), ('version', True)]:
            with self.subTest(field=field, value=value):
                record = json.loads(original)
                record[field] = value
                self.control.route.journal.write_text(json.dumps(record))
                with self.assertRaisesRegex(RuntimeError, 'Invalid Bluetooth'):
                    self.control.stop()
                self.assertTrue(self.control.route.journal.exists())
        self.control.route.journal.write_bytes(original)
        self.control.stop()

    def test_replaced_journal_even_identical_record_is_retained(self):
        self.prepare()
        journal = self.control.route.journal
        saved = journal.read_bytes()
        journal.rename(self.root / 'previous.json')
        journal.write_bytes(saved)
        with self.assertRaisesRegex(RuntimeError, 'inode changed'):
            self.control.stop()
        self.assertEqual(journal.read_bytes(), saved)
        self.control.route.check_lock()

    def test_symlink_journal_never_followed_or_removed(self):
        self.prepare()
        journal = self.control.route.journal
        target = self.root / 'other.json'
        journal.rename(target)
        journal.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.control.stop()
        self.assertTrue(journal.is_symlink())
        self.assertTrue(target.exists())

    def test_cleanup_directory_sync_failure_retains_lock_and_evidence(self):
        self.prepare()
        with patch('bluetooth_route.sync_directory', side_effect=OSError('fsync failed')):
            with self.assertRaisesRegex(RuntimeError, 'fsync failed'):
                self.control.stop()
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()

    def test_journal_publish_never_overwrites_existing_file(self):
        from bluetooth_route import persist
        journal = self.control.route.journal
        journal.write_bytes(b'other recovery evidence')
        with self.assertRaises(FileExistsError):
            persist(journal, b'new record', 0o600)
        self.assertEqual(journal.read_bytes(), b'other recovery evidence')

    def test_changed_baseline_during_reload_is_not_overwritten(self):
        self.prepare()
        old_call = self.dsp.call.side_effect
        def change(name, *args):
            result = old_call(name, *args)
            if name == 'Reload':
                self.path.write_bytes(b'new baseline')
            return result
        self.dsp.call.side_effect = change
        with self.assertRaises(RuntimeError):
            self.control.stop()
        self.assertEqual(self.path.read_bytes(), b'new baseline')
        self.assertTrue(self.control.route.journal.exists())

    def test_symlink_tmpdir_fixture_in_fresh_process(self):
        alias = self.root / 'temp-link'
        alias.symlink_to(self.root, target_is_directory=True)
        env = dict(os.environ, TMPDIR=str(alias), TEMP=str(alias), TMP=str(alias))
        result = subprocess.run([sys.executable, '-m', 'unittest',
            'test_bluetooth_control.RecoveryTests.test_crash_recovery_restores_original_path_content_muted'],
            cwd=Path(__file__).resolve().parent, env=env, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_unlink_failure_retains_journal_and_lock(self):
        self.prepare()
        with patch.object(Path, 'unlink', side_effect=OSError('unlink failed')):
            with self.assertRaisesRegex(RuntimeError, 'unlink failed'):
                self.control.stop()
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()

    def test_journal_swap_at_cleanup_boundary_is_retained(self):
        self.prepare()
        original = self.control.route.original
        def swap():
            result = original()
            journal = self.control.route.journal
            journal.rename(self.root / 'old-journal')
            journal.write_bytes(b'other owner')
            return result
        with patch.object(self.control.route, 'original', side_effect=swap):
            with self.assertRaisesRegex(RuntimeError, 'inode changed'):
                self.control.route.cleanup_journal()
        self.assertEqual(self.control.route.journal.read_bytes(), b'other owner')
        self.control.route.check_lock()

    def test_cleanup_sync_failure_can_be_retried(self):
        self.prepare()
        with patch('bluetooth_route.sync_directory', side_effect=[OSError('fsync failed'), None]):
            with self.assertRaisesRegex(RuntimeError, 'fsync failed'):
                self.control.stop()
        self.control.stop()
        self.assertFalse(self.control.route.exists())

    def test_cleanup_lock_held_through_directory_sync(self):
        self.prepare()
        from bluetooth_route import sync_directory
        def check(root):
            self.control.route.check_lock()
            self.assertFalse(self.control.route.journal.exists())
            sync_directory(root)
        with patch('bluetooth_route.sync_directory', side_effect=check):
            self.control.stop()
        self.assertIsNone(self.control.route._lock_fd)

    def test_duplicate_and_nonobject_journal_rejected_after_restart(self):
        self.prepare()
        for raw in [b'[]', b'null', b'{"version":1,"version":1}', b'x' * 8193]:
            self.control.route.journal.write_bytes(raw)
            recovered = self.controller()
            self.control.route.release()
            try:
                with self.assertRaises(RuntimeError):
                    recovered.startup()
                self.assertTrue(recovered.route.journal.exists())
                recovered.route.check_lock()
            finally:
                recovered.route.release()

    def test_cleanup_refuses_unknown_or_unmuted_dsp(self):
        self.prepare()
        for change in [dict(mute=False), dict(config={}), dict(running='Starting')]:
            saved = copy.deepcopy(self.state)
            self.state.update(change)
            with self.assertRaisesRegex(RuntimeError, 'drifted'):
                self.control.route.cleanup_journal()
            self.assertTrue(self.control.route.journal.exists())
            self.control.route.check_lock()
            self.state = saved


class PrepareTests(unittest.TestCase):
    controller = RecoveryTests.controller
    call = RecoveryTests.call
    tearDown = RecoveryTests.tearDown

    def setUp(self):
        RecoveryTests.setUp(self)
        self.server.INPUT_LEVELS = Mock()
        self.check = patch('bluetooth_route.subprocess.run', return_value=SimpleNamespace(returncode=0))
        self.run_check = self.check.start()
        self.addCleanup(self.check.stop)

    def load(self):
        return self.control.route.prepare(44100, 'S16_LE')

    def test_checked_exact_candidate_and_crash_recovery(self):
        expected = self.load()
        self.run_check.assert_called_once_with(
            ['/opt/camilladsp/camilladsp', '--check', str(self.control.route.candidate)],
            capture_output=True, timeout=10, shell=False)
        self.assertEqual(expected['filters'], self.config['filters'])
        self.assertEqual(expected['pipeline'], self.config['pipeline'])
        self.assertEqual(expected['devices']['capture']['device'], 'hw:Loopback,1,2')
        self.assertEqual(self.state['config'], expected)
        self.assertTrue(self.state['mute'])
        self.control.route.release()
        recovered = self.controller()
        self.addCleanup(recovered.route.release)
        recovered.startup()
        self.assertEqual(self.state['config'], self.config)
        self.assertFalse(recovered.route.exists())

    def test_check_failure_or_missing_binary_never_switches(self):
        for result in (SimpleNamespace(returncode=1), FileNotFoundError('Camilla missing')):
            self.run_check.side_effect = result if isinstance(result, Exception) else None
            self.run_check.return_value = result
            with self.assertRaises((RuntimeError, FileNotFoundError)):
                self.load()
            self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
            self.assertTrue(self.control.route.journal.exists())
            self.control.route.check_lock()
            self.control.stop()
            self.calls.clear()

    def test_check_races_retain_evidence_without_path_switch(self):
        for race in ('candidate', 'approval', 'airplay', 'autotune', 'dsp'):
            with self.subTest(race=race):
                self.setUp()
                airplay_journal = self.root / 'airplay-recovery.json'
                airplay_journal.write_bytes(b'preserve AirPlay evidence')
                def check(*args, **kwargs):
                    if race == 'candidate':
                        self.control.route.candidate.write_bytes(b'changed')
                    elif race == 'approval':
                        self.server.TUNING.active = lambda p: None
                    elif race == 'airplay':
                        self.server.AIRPLAY_CONTROL.pending = object()
                    elif race == 'autotune':
                        self.server.TUNING.status = lambda: {'audio_owned': True}
                    else:
                        self.state['config'] = {}
                    return SimpleNamespace(returncode=0)
                self.run_check.side_effect = check
                with self.assertRaises(RuntimeError):
                    self.load()
                self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
                self.assertEqual(airplay_journal.read_bytes(), b'preserve AirPlay evidence')
                self.assertTrue(self.control.route.journal.exists())
                self.control.route.check_lock()

    def test_journal_sync_failure_prevents_check_and_switch(self):
        with patch('bluetooth_route.sync_directory', side_effect=OSError('durability')):
            with self.assertRaisesRegex(OSError, 'durability'):
                self.load()
        self.run_check.assert_not_called()
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()

    def test_reload_failure_and_wrong_loaded_config_retain_evidence(self):
        self.bad_reload = True
        with patch('bluetooth_route.time.sleep'), self.assertRaisesRegex(RuntimeError, 'not verified'):
            self.load()
        self.assertTrue(self.state['mute'])
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()

    def test_unsupported_pcm_never_switches(self):
        for rate, fmt in ((96000, 'S16_LE'), (True, 'S32_LE'), (48000, 'FLOAT_LE')):
            with self.assertRaises(ValueError):
                self.control.route.prepare(rate, fmt)
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))

    def test_any_approved_filter_count_is_accepted(self):
        # The approved digest, not a fixed filter count, identifies the baseline.
        self.config['filters'].pop(next(iter(self.config['filters'])))
        self.data = yaml.safe_dump(self.config).encode()
        self.path.write_bytes(self.data)
        self.state['config'] = copy.deepcopy(self.config)
        self.load()
        self.assertTrue(any(c[0] == 'SetConfigFilePath' for c in self.calls))

    def test_reload_exception_and_mute_drift_fail_muted(self):
        original_call = self.dsp.call.side_effect
        def call(name, *args):
            result = original_call(name, *args)
            if name == 'Reload':
                self.state['mute'] = False
                raise RuntimeError('reload interrupted')
            return result
        self.dsp.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, 'reload interrupted'):
            self.load()
        self.assertTrue(self.state['mute'])
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()

    def test_airplay_real_journal_blocks_prepare_untouched(self):
        from airplay_baseline import BaselineRoute
        airplay = BaselineRoute(self.server)
        airplay.root = self.root
        self.server.AIRPLAY_CONTROL.baseline = airplay
        airplay.journal.write_bytes(b'AirPlay recovery evidence')
        with self.assertRaisesRegex(RuntimeError, 'AirPlay'):
            self.load()
        self.assertEqual(airplay.journal.read_bytes(), b'AirPlay recovery evidence')
        self.run_check.assert_not_called()
        self.assertFalse(self.control.route.exists())

    def test_existing_bluetooth_generation_never_overwritten(self):
        self.control.route.begin(str(self.path), self.config)
        record = self.control.route.journal.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'pending'):
            self.load()
        self.assertEqual(self.control.route.journal.read_bytes(), record)
        self.run_check.assert_not_called()
        self.control.route.check_lock()


class AutoValidationTests(unittest.TestCase):
    """start_auto refuses a malformed generation before any policy, bus or DSP access."""

    def test_malformed_generation_touches_nothing(self):
        server = Mock()
        policy, identity = Mock(), Mock()
        control = BluetoothControl(server, policy=policy, identity=identity)
        device = '/org/bluez/hci0/dev_12_34_56_78_9A_BC'
        good = StreamGeneration(':1.10', ':1.11', device, device + '/fd6', '12:34:56:78:9A:BC', 0, 1)
        for bad in ('12:34:56:78:9A:BC', good._replace(bluez_owner='org.bluez'),
                    good._replace(bluealsa_owner=None), good._replace(device_path=device + '/x'),
                    good._replace(transport_path=device + '/fd6/extra'), good._replace(codec=1),
                    good._replace(codec=True), good._replace(counter=0), good._replace(address=None)):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                control.start_auto(bad, lambda generation: True)
        policy.assert_not_called()
        identity.assert_not_called()
        self.assertEqual(server.mock_calls, [])
        self.assertIsNone(control.session)
