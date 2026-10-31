"""Runtime controller: real supervisor/route against simulated DSP, MPD, bus and PCM.

No BlueZ, ALSA, CamillaDSP or audio device is contacted. This is not hardware
acceptance; it exercises ordering, arbitration and fail-closed behavior.
"""
import fcntl
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bluetooth_bridge import PCM, BridgeSupervisor
from bluetooth_control import BluetoothControl, handle_connection
from bluetooth_transport import StreamGeneration
import test_bluetooth_control

MAC = '12:34:56:78:9A:BC'


class RuntimeTests(unittest.TestCase):
    call = test_bluetooth_control.RecoveryTests.call

    def controller(self, **kwargs):
        options = dict(policy=lambda: self.allowed(), identity=self.identity,
                       supervisor=self.factory, timeout=self.timeout, interval=0.01)
        options.update(kwargs)
        control = BluetoothControl(self.server, **options)
        control.route.root = self.root
        return control

    def setUp(self):
        self.timeout = 5.0
        self.allowed = lambda: (MAC,)
        self.identity = Mock(return_value=MAC)
        test_bluetooth_control.RecoveryTests.setUp(self)
        self.control.route.release()
        self.state.update(volume=-10.0, mpd='play')
        self.mpd_calls = []
        self.server.CHANNEL_PROFILE = Path('/var/lib/octet-dsp/channel-dsp.yml')
        self.server.INPUT_LEVELS = Mock()
        self.server.ACTIVE_CAPTURE_CHANNELS = None
        self.server.mpd = self.mpd
        airplay = self.server.AIRPLAY_CONTROL
        airplay.hook_busy = threading.Lock()
        self.child = Mock(pid=123)
        self.child.poll.return_value = None
        self.child.wait.return_value = 0
        self.spawn = Mock(return_value=self.child)
        self.pointer = 0
        self.frozen = False
        self.phone_streams = True
        self.supervisors = []
        dsp_call = self.dsp.call.side_effect
        def call(name, *args):
            if name == 'SetVolume':
                self.calls.append((name, *args))
                if not self.state.get('ignore_volume'):
                    self.state['volume'] = args[0]
                return None
            if name == 'GetVolume':
                self.calls.append((name,))
                return self.state['volume']
            return dsp_call(name, *args)
        self.dsp.call.side_effect = call
        check = patch('bluetooth_route.subprocess.run', return_value=SimpleNamespace(returncode=0))
        self.run_check = check.start()
        self.addCleanup(check.stop)
        self.control = self.controller()
        self.addCleanup(self.shutdown)

    def tearDown(self):
        # Never more than the single guarded unmute, and never above -18 dB.
        unmutes = [i for i, c in enumerate(self.calls) if c == ('SetMute', False)]
        self.assertLessEqual(len(unmutes), 1)
        for index in unmutes:
            volumes = [c for c in self.calls[:index] if c[0] == 'SetVolume']
            self.assertTrue(volumes or self.state['volume'] <= -18)
        self.assertNotIn('play', [c.split()[0] for c in self.mpd_calls])
        self.assertFalse(any(c[0] == 'SetVolume' and c[1] > -18 for c in self.calls))

    def shutdown(self):
        if self.control.session is not None:
            try:
                self.control.stop()
            except RuntimeError:
                pass
        thread = self.control._thread
        if thread is not None:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.control.route.release()

    def mpd(self, *commands):
        self.mpd_calls.extend(commands)
        results = []
        for command in commands:
            if command == 'pause 1' and self.state['mpd'] == 'play':
                self.state['mpd'] = 'pause'
            results.append([['state', self.state['mpd']]] if command == 'status' else [])
        return results

    def factory(self, fail_closed, **kwargs):
        supervisor = BridgeSupervisor(
            fail_closed, owner=lambda name: b's ":1.42"\n', observe=self.observe,
            spawn=self.spawn, task=self.task, interval=0.01, stall_timeout=0.5, stop_timeout=0.2, **kwargs)
        self.supervisors.append(supervisor)
        return supervisor

    @staticmethod
    def task(pid, tid):
        # Fake /proc: bluealsa-aplay main 123 owns sub2 through IO worker task 125.
        if (pid, tid) not in ((123, 123), (123, 125)):
            raise RuntimeError('Playback is not owned by bridge')

    def observe(self):
        if not self.phone_streams:
            return None, None
        if not self.frozen:
            self.pointer += 64
        def pcm(owner):
            return PCM(44100, 'S16_LE', 2, owner, self.pointer, self.pointer)
        # CamillaDSP only opens capture sub2 once the candidate is loaded.
        loaded = self.state['path'] == str(self.control.route.candidate)
        return pcm(125), pcm(456) if loaded else None

    def wait_for(self, condition, seconds=5):
        end = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > end:
                self.fail('condition not reached')
            time.sleep(0.01)

    def index(self, call):
        return self.calls.index(call)

    def test_manual_start_routes_sub2_unmutes_once_at_safe_level_and_stop_restores(self):
        status = self.control.start(MAC.lower())
        self.assertEqual(status, dict(active=True, preparing=False, recovery_pending=False, error=None))
        candidate = str(self.control.route.candidate)
        self.assertEqual(self.state['path'], candidate)
        self.assertEqual(self.state['config']['devices']['capture']['device'], 'hw:Loopback,1,2')
        self.assertEqual(self.state['config']['filters'], self.config['filters'])
        self.assertEqual(self.spawn.call_args.args[0], ['bluealsa-aplay', '--profile-a2dp',
            '--pcm=hw:Loopback,0,2', '--volume=software', '--single-audio', MAC])
        # Mute first, then MPD pause, then journal-protected path switch, then
        # the bounded level, then the single unmute.
        first_mute = self.index(('SetMute', True))
        self.assertLess(first_mute, self.index(('SetConfigFilePath', candidate)))
        self.assertEqual(self.mpd_calls[:2], ['pause 1', 'status'])
        self.assertLess(self.index(('SetVolume', -18.0)), self.index(('SetMute', False)))
        self.assertEqual(self.state['volume'], -18.0)
        self.assertFalse(self.state['mute'])
        self.assertEqual(self.server.ACTIVE_CAPTURE_CHANNELS, (0, 1))
        self.control.tick()
        self.assertTrue(self.control.status()['active'])
        self.control.stop()
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['path'], str(self.path))
        self.assertEqual(self.state['config'], self.config)
        self.assertFalse(self.control.route.exists())
        self.control._thread.join(5)
        self.child.terminate.assert_called_once()
        self.assertEqual(self.control.status(), dict(active=False, preparing=False,
                                                     recovery_pending=False, error=None))
        self.assertEqual(self.state['mpd'], 'pause')

    def test_quieter_existing_level_is_kept(self):
        self.state['volume'] = -30.0
        self.control.start(MAC)
        self.assertFalse(any(c[0] == 'SetVolume' for c in self.calls))
        self.assertFalse(self.state['mute'])

    def test_unpaired_phone_or_bluez_down_never_touches_audio(self):
        def none_paired():
            raise RuntimeError('No paired Bluetooth phone')
        def bluez_down():
            raise OSError('busctl failed')
        for allowed, error in ((none_paired, RuntimeError), (lambda: ('11:22:33:44:55:66',), ValueError)):
            with self.subTest(error=error):
                self.allowed = allowed
                with self.assertRaises(error):
                    self.control.start(MAC)
        for allowed, cause in ((none_paired, 'No paired'), (bluez_down, 'busctl failed')):
            with self.subTest(cause=cause):
                self.allowed = allowed
                with self.assertRaisesRegex(RuntimeError, 'Bluetooth unavailable.*' + cause):
                    self.control.start(MAC)
        self.identity.assert_not_called()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.mpd_calls, [])
        self.spawn.assert_not_called()

    def test_disconnected_or_untrusted_phone_refused_before_muting(self):
        self.identity.side_effect = RuntimeError('Bluetooth identity unavailable or rejected: Trusted')
        with self.assertRaisesRegex(RuntimeError, 'Trusted'):
            self.control.start(MAC)
        self.identity.assert_called_once_with(MAC, (MAC,))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.mpd_calls, [])
        self.spawn.assert_not_called()
        self.assertIsNone(self.control.session)

    def test_every_airplay_ownership_state_refuses_without_muting(self):
        airplay = self.server.AIRPLAY_CONTROL
        journal = SimpleNamespace(exists=lambda: True)
        for field, value in (('active', True), ('pending', 1.0), ('restore_pending', b'channel'),
                             ('needs_mute', True), ('baseline', journal), ('hook_busy', None)):
            with self.subTest(field=field):
                saved = getattr(airplay, field)
                if field == 'hook_busy':
                    airplay.hook_busy.acquire()
                else:
                    setattr(airplay, field, value)
                try:
                    with self.assertRaisesRegex(RuntimeError, 'AirPlay'):
                        self.control.start(MAC)
                finally:
                    if field == 'hook_busy':
                        airplay.hook_busy.release()
                    else:
                        setattr(airplay, field, saved)
        self.assertNotIn(('SetMute', True), self.calls)
        self.assertEqual(self.mpd_calls, [])
        self.assertIsNone(self.control.session)

    def test_unsaved_profile_or_autotune_refuses_without_muting(self):
        tuning = self.server.TUNING
        saved = tuning.active, tuning.status
        for change in ('unapproved', 'autotune', 'unknown'):
            with self.subTest(change=change):
                if change == 'unapproved':
                    self.server.TUNING.active = lambda p: None
                elif change == 'autotune':
                    self.server.TUNING.status = lambda: {'audio_owned': True}
                else:
                    self.server.TUNING.status = lambda: {}
                with self.assertRaises(RuntimeError):
                    self.control.start(MAC)
                self.state['path'] = str(self.path)
                tuning.active, tuning.status = saved
        self.assertNotIn(('SetMute', True), self.calls)
        self.assertEqual(self.mpd_calls, [])

    def test_mpd_that_will_not_pause_fails_closed_before_route_change(self):
        self.mpd_calls.clear()
        def stubborn(*commands):
            self.mpd_calls.extend(commands)
            return [[['state', 'play']]] if commands == ('status',) else [[]]
        self.server.mpd = stubborn
        with self.assertRaisesRegex(RuntimeError, 'did not pause'):
            self.control.start(MAC)
        self.assertTrue(self.state['mute'])
        self.spawn.assert_not_called()
        self.assertFalse(self.control.route.exists())
        self.assertFalse(any(c[0] == 'SetConfigFilePath' for c in self.calls))
        self.mpd_calls.clear()

    def test_whole_start_operation_deadline_when_phone_never_streams(self):
        self.timeout = 0.3
        self.control = self.controller()
        self.phone_streams = False
        began = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.control.start(MAC)
        self.assertLess(time.monotonic() - began, 2)
        self.assertTrue(self.state['mute'])
        self.assertFalse(self.control.route.exists())
        self.control._thread.join(5)
        self.child.terminate.assert_called_once()
        # The supervisor shares the controller's absolute deadline.
        self.assertLessEqual(self.supervisors[0].deadline - began, 0.3 + 0.05)

    def test_slow_preparation_consumes_the_same_deadline_and_recovers(self):
        self.timeout = 0.4
        self.control = self.controller()
        def slow_check(*args, **kwargs):
            time.sleep(0.6)
            return SimpleNamespace(returncode=0)
        self.run_check.side_effect = slow_check
        with self.assertRaisesRegex(RuntimeError, 'deadline'):
            self.control.start(MAC)
        self.control._thread.join(5)
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['path'], str(self.path))
        self.assertFalse(self.control.route.exists())

    def test_capture_progress_must_be_fresh_at_unmute(self):
        # Capture opens but pointers never advance: readiness is never granted.
        self.timeout = 0.5
        self.control = self.controller()
        self.frozen = True
        with self.assertRaises(RuntimeError):
            self.control.start(MAC)
        self.control._thread.join(5)
        self.assertTrue(self.state['mute'])
        self.assertFalse(self.control.route.exists())

    def test_stall_after_activation_mutes_and_restores(self):
        self.control.start(MAC)
        self.frozen = True
        self.control._thread.join(5)
        self.assertIsNone(self.control.session)
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['path'], str(self.path))
        self.assertIn('stalled', self.control.status()['error'])

    def test_live_drift_volume_or_mute_change_fails_closed_on_tick(self):
        changes = [dict(config={}), dict(volume=-6.0), dict(mute=True)]
        for change in changes:
            with self.subTest(change=change):
                self.control.start(MAC)
                self.state.update(change)
                if 'config' in change:
                    self.state['config'] = {'devices': {}}
                self.control.tick()
                self.assertIsNone(self.control.session)
                self.assertTrue(self.state['mute'])
                self.assertEqual(self.state['path'], str(self.path))
                self.control._thread.join(5)
                self.calls.clear()
                self.state.update(volume=-18.0)

    def test_stopped_dsp_fails_closed_and_retains_recovery_evidence(self):
        self.control.start(MAC)
        self.state['running'] = 'Paused'
        with patch('bluetooth_route.time.sleep'), self.assertRaisesRegex(RuntimeError, 'restore not verified'):
            self.control.tick()
        self.assertTrue(self.state['mute'])
        self.assertTrue(self.control.route.journal.exists())
        self.assertTrue(self.control.owns_audio())
        self.assertTrue(self.control.status()['recovery_pending'])
        with self.assertRaisesRegex(RuntimeError, 'recovery already pending'):
            self.control.start(MAC)
        self.state['running'] = 'Running'
        self.control.tick()  # watchdog retries the orphaned recovery
        self.assertFalse(self.control.route.exists())
        self.assertEqual(self.state['path'], str(self.path))

    def test_volume_that_cannot_be_limited_never_unmutes(self):
        self.state['ignore_volume'] = True
        with self.assertRaisesRegex(RuntimeError, '-18 dB'):
            self.control.start(MAC)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue(self.state['mute'])
        self.state['volume'] = -18.0

    def test_stop_during_preparation_window_never_unmutes(self):
        self.timeout = 3.0
        self.control = self.controller()
        self.frozen = True
        errors = []
        def start():
            try:
                self.control.start(MAC)
            except RuntimeError as error:
                errors.append(error)
        thread = threading.Thread(target=start)
        thread.start()
        self.wait_for(lambda: self.control.session is not None
                      and self.control.session.expected is not None)
        self.control.stop()
        thread.join(5)
        self.assertEqual(len(errors), 1)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertEqual(self.state['path'], str(self.path))

    def test_second_start_and_restart_while_stopping_are_refused(self):
        self.control.start(MAC)
        with self.assertRaisesRegex(RuntimeError, 'already pending'):
            self.control.start(MAC)
        self.control.stop()

    def test_identity_loss_after_activation_fails_closed(self):
        self.control.start(MAC)
        self.identity.side_effect = RuntimeError('Bluetooth identity unavailable or rejected: Connected')
        self.control._thread.join(5)
        self.assertIsNone(self.control.session)
        self.assertTrue(self.state['mute'])
        self.assertIn('Connected', self.control.status()['error'])
        self.assertFalse(self.control.route.exists())

    def restore_index(self):
        return self.index(('SetConfigFilePath', str(self.path)))

    def test_dsp_restore_only_after_owned_child_terminated_and_reaped(self):
        for trigger in ('stop', 'stall'):
            with self.subTest(trigger=trigger):
                self.child.terminate.side_effect = lambda: self.calls.append(('terminate',))
                self.child.wait.side_effect = lambda timeout: self.calls.append(('reaped',))
                self.control.start(MAC)
                if trigger == 'stop':
                    self.control.stop()
                else:  # supervisor-initiated: its callback runs the same fail()
                    self.frozen = True
                    self.wait_for(lambda: self.control.session is None)
                self.control._thread.join(5)
                first_mute = max(i for i, c in enumerate(self.calls[:self.index(('terminate',))])
                                 if c == ('SetMute', True))
                self.assertLess(first_mute, self.index(('terminate',)))
                self.assertLess(self.index(('reaped',)), self.restore_index())
                self.assertEqual(self.calls.count(('terminate',)), 1)
                self.assertFalse(self.control.route.exists())
                self.calls.clear()
                self.frozen = False
                self.state.update(volume=-18.0)

    def test_bridge_termination_failure_retains_mute_journal_and_retry_recovers(self):
        self.control.start(MAC)
        candidate = str(self.control.route.candidate)
        self.child.wait.side_effect = subprocess.TimeoutExpired('bluealsa-aplay', 0.2)
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'termination not confirmed.*restore deferred'):
            self.control.stop()
        self.child.kill.assert_called_once()
        self.control._thread.join(5)
        # Muted, candidate never restored or reloaded while the bridge may live.
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['path'], candidate)
        self.assertFalse(any(c[0] in ('SetConfigFilePath', 'Reload') for c in self.calls))
        self.assertTrue(self.control.route.journal.exists())
        self.control.route.check_lock()
        self.assertTrue(self.control.owns_audio())  # AirPlay stays refused
        self.assertTrue(self.control.status()['recovery_pending'])
        with self.assertRaisesRegex(RuntimeError, 'already pending'):
            self.control.start(MAC)
        began = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'not exited after SIGKILL'):
            self.control.tick()  # monitor logs this; still unconfirmed
        self.assertLess(time.monotonic() - began, 0.2)  # retry never blocks LOCK
        self.assertEqual(self.state['path'], candidate)
        self.child.poll.return_value = -9  # the killed child is finally reaped
        self.control.tick()
        self.assertEqual(self.state['path'], str(self.path))
        self.assertEqual(self.state['config'], self.config)
        self.assertTrue(self.state['mute'])
        self.assertFalse(self.control.owns_audio())
        self.assertFalse(self.control.status()['recovery_pending'])

    def test_final_unmute_holds_server_lock_and_audio_lock_lease(self):
        seen = []
        dsp_call = self.dsp.call.side_effect
        def call(name, *args):
            if (name, *args) == ('SetMute', False):
                seen.append(self.server.LOCK._is_owned())
                fd = os.open(self.root / 'audio.lock', os.O_RDONLY)
                try:  # a cooperative Auto Tune writer cannot take the lease now
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
            return dsp_call(name, *args)
        self.dsp.call.side_effect = call
        self.control.start(MAC)
        self.assertEqual(seen, [True])

    def test_lost_audio_lock_lease_before_unmute_never_unmutes(self):
        dsp_call = self.dsp.call.side_effect
        def call(name, *args):
            if name == 'SetVolume':
                # The shared lock inode is replaced after the clamp.
                lock = self.root / 'audio.lock'
                lock.rename(self.root / 'old.lock')
                lock.touch()
            return dsp_call(name, *args)
        self.dsp.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, 'lock'):
            self.control.start(MAC)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue(self.state['mute'])

    def test_external_volume_raise_between_clamp_and_unmute_never_unmutes(self):
        dsp_call = self.dsp.call.side_effect
        reads = []
        def call(name, *args):
            result = dsp_call(name, *args)
            if name == 'GetVolume' and any(c[0] == 'SetVolume' for c in self.calls):
                reads.append(result)
                if len(reads) == 1:
                    self.state['volume'] = -6.0  # non-cooperative writer after clamp read-back
            return result
        self.dsp.call.side_effect = call
        with self.assertRaisesRegex(RuntimeError, '-18 dB'):
            self.control.start(MAC)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue(self.state['mute'])
        self.state['volume'] = -18.0

    def test_external_change_right_after_unmute_is_remuted_on_same_connection(self):
        dsp_call = self.dsp.call.side_effect
        def call(name, *args):
            result = dsp_call(name, *args)
            if (name, *args) == ('SetMute', False):
                self.state['volume'] = -6.0  # unpreventable: no compare-and-set
            return result
        self.dsp.call.side_effect = call
        def connect():
            self.calls.append(('connect',))
            return self.dsp
        self.dsp.__enter__.side_effect = connect
        with self.assertRaisesRegex(RuntimeError, '-18 dB'):
            self.control.start(MAC)
        unmute = self.index(('SetMute', False))
        remute = self.calls.index(('SetMute', True), unmute)
        # Re-muted before any new connection (i.e. before fail() even starts).
        self.assertNotIn(('connect',), self.calls[unmute:remute])
        self.assertNotIn(('SetMute', False), self.calls[unmute + 1:])
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['path'], str(self.path))
        self.state['volume'] = -18.0

    def test_orphaned_journal_is_recovered_by_watchdog(self):
        self.control.route.begin(str(self.path), self.config)
        self.control.route.release()
        self.state['mute'] = False
        restarted = self.controller()
        self.addCleanup(restarted.route.release)
        self.assertTrue(restarted.owns_audio())
        restarted.tick()
        self.assertTrue(self.state['mute'])
        self.assertFalse(restarted.route.exists())
        self.assertFalse(restarted.owns_audio())

    def generation(self, counter=1):
        device = '/org/bluez/hci0/dev_12_34_56_78_9A_BC'
        return StreamGeneration(':1.10', ':1.11', device, device + '/fd6', MAC, 0, counter)

    def auto(self, generation=None, current=lambda generation: True):
        self.state['mpd'] = 'stop'
        return self.control.start_auto(generation or self.generation(), current)

    def assert_untouched(self):
        self.assertEqual(self.calls, [])
        self.assertNotIn('pause 1', self.mpd_calls)
        self.spawn.assert_not_called()
        self.assertIsNone(self.control.session)
        self.assertFalse(self.control.route.exists())

    def test_auto_refuses_connection_only_and_fabricated_generation(self):
        self.state['mpd'] = 'stop'
        forged = self.generation()._replace(transport_path='/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA/fd6')
        for bad in (MAC, (MAC,), None, forged, self.generation()._replace(address=MAC.lower()),
                    self.generation()._replace(counter=True)):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                self.control.start_auto(bad, lambda generation: True)
        with self.assertRaises(TypeError):
            self.control.start_auto(self.generation(), None)
        self.assert_untouched()
        self.identity.assert_not_called()

    def test_auto_stale_before_start_or_unlisted_refuses_without_mutation(self):
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            self.auto(current=lambda generation: False)
        def broken(generation):
            raise RuntimeError('cache unavailable')
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            self.auto(current=broken)
        self.allowed = lambda: ('11:22:33:44:55:66',)
        with self.assertRaises(ValueError):
            self.auto()
        self.assert_untouched()

    def test_auto_takes_over_a_playing_queue_by_pausing_it(self):
        self.state['mpd'] = 'play'
        self.control.start_auto(self.generation(), lambda generation: True)
        self.assertTrue(self.control.status()['active'])
        self.assertIn('pause 1', self.mpd_calls)
        self.assertEqual(self.state['mpd'], 'pause')

    def test_auto_refuses_unless_mpd_explicitly_stopped(self):
        for label, status in (('missing state', [['volume', '50']]), ('empty status', []),
                              ('unknown value', [['state', 'buffering']])):
            with self.subTest(status=label):
                self.mpd_calls.clear()
                self.calls.clear()
                def unknown(*commands, status=status):
                    self.mpd_calls.extend(commands)
                    return [status] if commands == ('status',) else [[]]
                self.server.mpd = unknown
                with self.assertRaisesRegex(RuntimeError, 'Music player'):
                    self.control.start_auto(self.generation(), lambda generation: True)
                self.assertTrue({c[0] for c in self.calls} <= {'GetConfigFilePath', 'GetConfigJson', 'GetState'})
                self.assertEqual(self.mpd_calls, ['status'])
                self.spawn.assert_not_called()
                self.assertIsNone(self.control.session)
                self.assertFalse(self.control.route.exists())

    def test_auto_refuses_other_owners_without_touching_them(self):
        self.state['mpd'] = 'stop'
        self.server.AIRPLAY_CONTROL.active = True
        with self.assertRaisesRegex(RuntimeError, 'AirPlay'):
            self.control.start_auto(self.generation(), lambda generation: True)
        self.server.AIRPLAY_CONTROL.active = False
        self.server.TUNING.status = lambda: {'audio_owned': True}
        with self.assertRaisesRegex(RuntimeError, 'Auto Tune'):
            self.control.start_auto(self.generation(), lambda generation: True)
        self.assertNotIn(('SetMute', True), self.calls)
        self.assertEqual(self.mpd_calls, [])
        self.spawn.assert_not_called()
        self.assertFalse(self.control.route.exists())

    def test_auto_refuses_while_manual_session_owns_audio(self):
        self.control.start(MAC)
        calls = len(self.calls)
        with self.assertRaisesRegex(RuntimeError, 'already pending'):
            self.auto()
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.control.session.mode, 'manual')
        self.assertTrue(self.control.status()['active'])

    def test_auto_stale_before_unmute_never_unmutes_and_recovers(self):
        # Valid through every precheck and the clamp; lost right before SetMute(False).
        def current(generation):
            return ('SetVolume', -18.0) not in self.calls
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            self.auto(current=current)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue(self.state['mute'])
        self.assertIsNone(self.control.session)
        self.assertFalse(self.control.route.exists())
        self.assertEqual(self.state['path'], str(self.path))
        self.control._thread.join(5)
        self.child.terminate.assert_called_once()

    def test_auto_matching_generation_uses_guarded_route_and_outlives_manual_cutoff(self):
        self.control = self.controller(lifetime=0.2)
        self.auto()
        self.assertIsNone(self.supervisors[0].lifetime)
        self.assertEqual(self.control.session.mode, 'auto')
        self.assertEqual(self.state['volume'], -18.0)
        self.assertFalse(self.state['mute'])
        self.assertLess(self.index(('SetMute', True)), self.index(('SetVolume', -18.0)))
        self.assertLess(self.index(('SetVolume', -18.0)), self.index(('SetMute', False)))
        self.assertEqual(self.mpd_calls[:3], ['status', 'pause 1', 'status'])
        time.sleep(0.5)
        self.control.tick()
        self.assertTrue(self.control.status()['active'])
        self.assertEqual(self.state['mpd'], 'stop')

    def test_manual_keeps_lifetime_and_mode(self):
        self.control = self.controller(lifetime=123.0)
        self.control.start(MAC)
        self.assertEqual(self.supervisors[0].lifetime, 123.0)
        self.assertEqual(self.control.session.mode, 'manual')
        self.assertEqual(self.control.lifetime, 123.0)

    def test_auto_lost_generation_on_tick_mutes_then_reaps_child(self):
        alive = [True]
        self.auto(current=lambda generation: alive[0])
        order = []
        self.child.terminate.side_effect = lambda: order.append(('terminate', self.state['mute']))
        alive[0] = False
        self.control.tick()
        self.assertIsNone(self.control.session)
        self.control._thread.join(5)
        self.assertEqual(order, [('terminate', True)])
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['path'], str(self.path))
        self.assertIn('generation', self.control.status()['error'])
        self.assertFalse(self.control.route.exists())

    def test_auto_lost_generation_during_preparation_also_fails_on_tick(self):
        alive = [True]
        self.frozen = True
        self.timeout = 3.0
        self.control = self.controller()
        self.state['mpd'] = 'stop'
        errors = []
        def start():
            try:
                self.control.start_auto(self.generation(), lambda generation: alive[0])
            except RuntimeError as error:
                errors.append(error)
        thread = threading.Thread(target=start)
        thread.start()
        self.wait_for(lambda: self.control.session is not None
                      and self.control.session.expected is not None)
        alive[0] = False
        self.control.tick()
        thread.join(5)
        self.assertIsNone(self.control.session)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue(self.state['mute'])

    def test_stale_or_wrong_stop_auto_cannot_stop_newer_or_manual_session(self):
        newer = self.generation(counter=2)
        self.auto(newer)
        self.control.stop_auto(self.generation(counter=1), 'stale invalidate')
        self.control.stop_auto(newer._replace(address='AA:BB:CC:DD:EE:FF'), 'wrong phone')
        self.assertTrue(self.control.status()['active'])
        self.assertFalse(self.state['mute'])
        self.child.terminate.assert_not_called()
        self.control.stop()
        self.control._thread.join(5)
        self.calls.clear()
        self.state['volume'] = -18.0
        self.control.start(MAC)
        self.control.stop_auto(newer, 'stale event against manual')
        self.assertTrue(self.control.status()['active'])
        self.assertFalse(self.state['mute'])
        self.assertEqual(self.control.session.mode, 'manual')

    def test_matching_stop_auto_mutes_reaps_restores_and_never_resumes_mpd(self):
        generation = self.generation()
        self.auto(generation)
        status = self.control.stop_auto(generation, 'stream ended')
        self.assertEqual(status, dict(active=False, preparing=False, recovery_pending=False, error=None))
        self.assertTrue(self.state['mute'])
        self.assertEqual(self.state['path'], str(self.path))
        self.control._thread.join(5)
        self.child.terminate.assert_called_once()
        self.assertEqual(self.state['mpd'], 'stop')
        self.assertEqual(self.control.stop_auto(generation, 'again'), self.control.status())


class SocketTests(unittest.TestCase):
    def connection(self, uid, line):
        connection = Mock()
        connection.getsockopt.return_value = struct.pack('3i', 1, uid, 1)
        connection.makefile.return_value = io.BytesIO(line)
        return connection

    def reply(self, connection):
        raw = connection.sendall.call_args.args[0]
        self.assertTrue(raw.endswith(b'\n'))
        status, _, body = raw[:-1].partition(b' ')
        return status, json.loads(body)

    def test_only_root_peer_and_exact_commands(self):
        control = Mock()
        control.status.return_value = dict(active=False)
        control.start.return_value = dict(active=True)
        for uid, line in ((1000, b'status\n'), (0, b'start *\n'), (0, b'start 12:34:56:78:9a:bc\n'),
                          (0, b'start ' + MAC.encode()), (0, b'pair\n'), (0, b'')):
            with self.subTest(uid=uid, line=line):
                connection = self.connection(uid, line)
                handle_connection(connection, control)
                self.assertEqual(self.reply(connection)[0], b'ERROR')
                connection.close.assert_called_once()
        control.start.assert_not_called()
        control.stop.assert_not_called()
        connection = self.connection(0, b'start ' + MAC.encode() + b'\n')
        handle_connection(connection, control)
        self.assertEqual(self.reply(connection), (b'OK', dict(active=True)))
        control.start.assert_called_once_with(MAC)
        connection = self.connection(0, b'stop\n')
        handle_connection(connection, control)
        control.stop.assert_called_once_with()
        self.assertEqual(self.reply(connection), (b'OK', dict(active=False)))

    def test_controller_errors_are_reported_without_internal_details(self):
        control = Mock()
        control.start.side_effect = KeyError('internal')
        connection = self.connection(0, b'start ' + MAC.encode() + b'\n')
        handle_connection(connection, control)
        self.assertEqual(self.reply(connection), (b'ERROR', 'Bluetooth control failed'))



class ServerIntegrationTests(unittest.TestCase):
    """Real server/AirPlay modules; DSP, MPD and Auto Tune are simulated."""
    def setUp(self):
        import server
        from airplay_control import AirPlayController
        self.server = server
        self.real_bluetooth = server.BLUETOOTH_CONTROL
        self.dsp = Mock()
        self.dsp.__enter__ = Mock(return_value=self.dsp)
        self.dsp.__exit__ = Mock(return_value=False)
        self.dsp.call.return_value = None
        self.order = Mock()
        self.bluetooth = self.order.bluetooth
        self.bluetooth.owns_audio.return_value = True
        for item in (patch.object(server, 'DSP', return_value=self.dsp),
                     patch.object(server, 'mpd', self.order.mpd),
                     patch.object(server, 'BLUETOOTH_CONTROL', self.bluetooth),
                     patch.object(server, 'current_status', return_value={}),
                     patch.object(server.TUNING, 'status', return_value={'audio_owned': False})):
            item.start()
            self.addCleanup(item.stop)
        self.airplay = AirPlayController(server)

    def test_airplay_start_refused_while_bluetooth_owns_without_touching_dsp(self):
        with patch.object(self.server, 'AIRPLAY_CONTROL', self.airplay):
            with self.assertRaisesRegex(RuntimeError, 'Bluetooth owns audio'):
                self.airplay.event('start')
        self.dsp.call.assert_not_called()
        self.order.mpd.assert_not_called()
        self.assertIsNone(self.airplay.pending)
        self.assertFalse(self.airplay.active)
        self.assertIn('Bluetooth', self.airplay.error)
        self.bluetooth.owns_audio.return_value = False
        self.airplay.event('stop')  # stop never consults or disturbs Bluetooth

    def test_manual_action_is_an_override_that_stops_bluetooth_first(self):
        with patch.object(self.server, 'AIRPLAY_CONTROL', self.airplay):
            self.server.action('pause', {})
        names = [call[0] for call in self.order.mock_calls]
        self.assertLess(names.index('bluetooth.stop'), names.index('mpd'))
        self.bluetooth.stop.assert_called_once_with('Manual control override')

    def test_failed_bluetooth_override_blocks_the_manual_action(self):
        self.bluetooth.stop.side_effect = RuntimeError('Bluetooth restore not verified')
        with patch.object(self.server, 'AIRPLAY_CONTROL', self.airplay):
            for name in ('pause', 'auto-start', 'volume'):
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'restore'):
                    self.server.action(name, {'db': -20})
        self.order.mpd.assert_not_called()
        self.dsp.call.assert_not_called()

    def test_idle_bluetooth_is_not_touched_by_manual_actions(self):
        self.bluetooth.owns_audio.return_value = False
        with patch.object(self.server, 'AIRPLAY_CONTROL', self.airplay):
            self.server.action('pause', {})
        self.bluetooth.stop.assert_not_called()

    def test_startup_recovers_bluetooth_even_when_airplay_recovery_fails(self):
        airplay = Mock(needs_mute=False, error=None)
        airplay.recover.side_effect = OSError('AirPlay journal broken')
        self.bluetooth.startup.side_effect = RuntimeError('Bluetooth recovery retained')
        self.dsp.call.side_effect = lambda name, *args: True if name == 'GetMute' else (
            '{}' if name == 'GetConfigJson' else -18 if name == 'GetVolume' else None)
        with patch.object(self.server, 'AIRPLAY_CONTROL', airplay), \
             patch.object(self.server, 'LEVELS_UNLOCKED'), \
             patch.object(self.server, 'ACTIVE_CAPTURE_CHANNELS'):
            self.server.initialize_dsp()
        self.bluetooth.startup.assert_called_once_with()
        calls = [c.args for c in self.dsp.call.call_args_list]
        self.assertEqual(calls[-4:-2], [('SetMute', True), ('GetMute',)])

    def test_bluetooth_socket_served_only_with_installed_runtime_directory(self):
        import bluetooth_control
        for present in (False, True):
            with self.subTest(present=present), \
                 patch.object(self.server, 'initialize_dsp'), \
                 patch.object(self.server.os, 'umask'), \
                 patch.object(self.server.Path, 'exists',
                              lambda path: present and str(path) == '/run/octet-bluetooth'), \
                 patch.object(self.server.threading, 'Thread') as thread, \
                 patch.object(self.server, 'ThreadingHTTPServer'):
                self.server.main()
                targets = [c.kwargs.get('target') for c in thread.call_args_list]
                self.assertEqual(bluetooth_control.serve in targets, present)
                self.assertNotIn(self.server.serve_airplay, targets)

    def test_real_server_controller_lets_any_paired_phone_play(self):
        import bluetooth_control
        from bluetooth_identity import paired_addresses
        control = self.real_bluetooth
        self.assertIsInstance(control, bluetooth_control.BluetoothControl)
        self.assertIs(control.server, self.server)
        self.assertIs(control.policy, paired_addresses)
        self.assertIsNone(control.session)


if __name__ == '__main__':
    unittest.main()
