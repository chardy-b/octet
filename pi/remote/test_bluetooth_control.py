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
import test_bluetooth_contract


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
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
            TUNING=SimpleNamespace(root=self.root, active=lambda p: {'sha256': digest(self.data)},
                                   status=lambda: {'audio_owned': False}),
            AIRPLAY_CONTROL=SimpleNamespace(active=False, pending=None, baseline=SimpleNamespace(exists=lambda: False)))
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
        self.control.tick(generation)
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['config'], self.config)
        self.assertFalse(self.control.route.exists())

    def test_stale_tick_and_stop_recover_start_always_disabled(self):
        for event in ('tick', 'stop', 'start'):
            with self.subTest(event=event):
                self.prepare()
                if event == 'start':
                    with self.assertRaisesRegex(RuntimeError, 'disabled'):
                        self.control.start('old')
                else:
                    getattr(self.control, event)('old')
                self.assertFalse(self.control.route.exists())
                self.assertTrue(self.state['mute'])

    def test_failed_restore_retains_journal_and_lock(self):
        self.prepare()
        self.state['config'] = {}
        self.bad_reload = True
        with patch('bluetooth_route.time.sleep'), self.assertRaisesRegex(RuntimeError, 'restore not verified'):
            self.control.stop()
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()
        self.assertTrue(self.state['mute'])

    def test_changed_snapshot_baseline_or_lock_retains_recovery(self):
        for target in ('snapshot', 'baseline', 'lock'):
            with self.subTest(target=target):
                if not self.control.route.exists():
                    self.prepare()
                path = {'snapshot': self.control.route.snapshot, 'baseline': self.path,
                        'lock': self.root / 'audio.lock'}[target]
                saved = path.read_bytes()
                if target == 'lock':
                    path.rename(self.root / 'old.lock')
                    path.touch()
                else:
                    path.chmod(0o600)
                    path.write_bytes(b'changed')
                with self.assertRaises(RuntimeError):
                    self.control.stop()
                self.assertTrue(self.control.route.journal.exists())
                self.assertTrue(self.state['mute'])
                if target != 'lock':
                    path.write_bytes(saved)

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
        with self.assertRaisesRegex(RuntimeError, 'approved'):
            self.prepare()

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
                for field, value in (('active', True), ('pending', object())):
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
