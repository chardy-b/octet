import subprocess
import tempfile
import sys
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from bluetooth_bridge import BridgeSupervisor, PCM, bus_owner, parse_pcm, read_endpoints

MAC = '12:34:56:78:9A:BC'

# Real Pi ALSA status layout, including the separator before mmap pointers.
PI_STATUS = '''state: RUNNING
owner_pid : 41121
trigger_time: 1793059200.123456789
tstamp: 1793059201.123456789
delay: 1024
avail: 1024
avail_max: 4096
-----
hw_ptr: 26738688
appl_ptr: 26737664
'''


def hw(rate=48000, fmt='S16_LE', channels=2):
    return f'access: RW_INTERLEAVED\nformat: {fmt}\nchannels: {channels}\nrate: {rate} ({rate}/1)\n'


def status(pointer=10, owner=123, state='RUNNING'):
    return f'state: {state}\nowner_pid   : {owner}\nhw_ptr: {pointer}\nappl_ptr: {pointer}\n'


class ParsingTests(unittest.TestCase):
    def test_real_pi_status_with_and_without_separator(self):
        for state in (PI_STATUS, PI_STATUS.replace('-----\n', '')):
            self.assertEqual(parse_pcm(hw(), state),
                             PCM(48000, 'S16_LE', 2, 41121, 26738688, 26737664))

    def test_separator_exception_does_not_hide_malformed_fields(self):
        for replacement in ('----', '------', ' -----', '----- ',
                            '----- extra', '-----\n-----', 'garbage'):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                parse_pcm(hw(), PI_STATUS.replace('-----', replacement))
        for state in (
                '-----\n' + PI_STATUS.replace('-----\n', ''),
                PI_STATUS.replace('-----\n', '') + '-----\n',
                PI_STATUS.split('hw_ptr:')[0],
                PI_STATUS.replace('hw_ptr: 26738688', 'broken'),
                PI_STATUS.replace('hw_ptr:', 'wrong:'),
                PI_STATUS.replace('delay: 1024', 'delay 1024'),
                PI_STATUS.replace('delay: 1024', ': 1024'),
                PI_STATUS.replace('delay: 1024', 'delay:'),
                PI_STATUS + 'owner_pid: 41121\n',
                PI_STATUS.replace('appl_ptr: 26737664', 'appl_ptr: 2x'),
                PI_STATUS.replace('owner_pid : 41121', 'owner_pid : -1')):
            with self.subTest(state=state), self.assertRaises((ValueError, KeyError)):
                parse_pcm(hw(), state)
        with self.assertRaises(ValueError):
            parse_pcm(hw() + 'avail_max: 4096\n-----\nhw_ptr: 10\n', PI_STATUS)

    def test_supported_native_formats(self):
        for rate in (44100, 48000):
            for fmt in ('S16_LE', 'S32_LE'):
                self.assertEqual(parse_pcm(hw(rate, fmt), status()).format, (rate, fmt, 2))
        self.assertIsNone(parse_pcm('closed\n', 'closed\n'))

    def test_reject_invalid_unknown_or_incomplete_observations(self):
        cases = [(hw(96000), status()), (hw(fmt='FLOAT_LE'), status()),
                 (hw(channels=1), status()), (hw(), status(state='XRUN')),
                 (hw(), status(state='PREPARED')), (hw(), status(owner=0)),
                 (hw(), status(pointer=-1)), (hw(), status() + 'hw_ptr: 20\n'),
                 (hw().replace('48000/1', '44100/1'), status()),
                 ('closed', status()), (hw(), 'closed'), ('', ''),
                 (hw(), status().replace('owner_pid', 'missing')),
                 (hw() + 'x' * 8193, status())]
        for params, state in cases:
            with self.subTest(params=params[:80], state=state):
                with self.assertRaises((ValueError, KeyError)):
                    parse_pcm(params, state)

    def test_exact_bus_owner_query_and_failures(self):
        good = SimpleNamespace(returncode=0, stderr=b'', stdout=b's ":1.42"\n')
        with patch('bluetooth_bridge.subprocess.run', return_value=good) as run:
            self.assertEqual(bus_owner('org.bluez'), good.stdout)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/busctl', '--system',
                '--no-pager', '--timeout=2s', 'call', 'org.freedesktop.DBus',
                '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'GetNameOwner',
                's', 'org.bluez'])
            self.assertEqual(run.call_args.kwargs, dict(capture_output=True, timeout=3, shell=False))
        for result in [SimpleNamespace(returncode=1, stderr=b'', stdout=good.stdout),
                       SimpleNamespace(returncode=0, stderr=b'warning', stdout=good.stdout),
                       SimpleNamespace(returncode=0, stderr=b'', stdout=b's "org.bluez"\n')]:
            with patch('bluetooth_bridge.subprocess.run', return_value=result):
                with self.assertRaises(RuntimeError):
                    bus_owner('org.bluez')

    def test_reads_only_verified_sub2_and_rejects_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            card = root / 'card2'
            card.mkdir()
            (root / 'Loopback').symlink_to(card)
            (root / 'modules').write_text('2 snd_aloop\n')
            (card / 'id').write_text('Loopback\n')
            for endpoint, device, direction in [('pcm0p', 0, 'PLAYBACK'), ('pcm1c', 1, 'CAPTURE')]:
                sub = card / endpoint / 'sub2'
                sub.mkdir(parents=True)
                (sub / 'info').write_text(f'card: 2\ndevice: {device}\nsubdevice: 2\nstream: {direction}\n')
                (sub / 'hw_params').write_text(hw())
                (sub / 'status').write_text(PI_STATUS)
            with patch('bluetooth_bridge.Path', return_value=root):
                self.assertEqual(len(read_endpoints()), 2)
                info = card / 'pcm0p/sub2/info'
                original_info = info.read_text()
                info.write_text(original_info + 'avail_max: 4096\n-----\nhw_ptr: 10\n')
                with self.assertRaises(ValueError):
                    read_endpoints()
                info.write_text(original_info)
                path = card / 'pcm1c/sub2/hw_params'
                path.unlink()
                path.symlink_to(card / 'pcm0p/sub2/hw_params')
                with self.assertRaisesRegex(ValueError, 'Redirected'):
                    read_endpoints()


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.ticks = 0
        self.child = Mock(pid=123)
        self.child.poll.return_value = None
        self.child.wait.return_value = 0
        self.spawn = Mock(return_value=self.child)
        self.policy = Mock(return_value=(MAC,))
        self.identity = Mock(return_value=MAC)
        self.owner = Mock(return_value=b's ":1.42"\n')
        self.close = Mock()
        self.observe = self.normal
        self.event = SimpleNamespace(is_set=lambda: self.ticks >= 4, wait=self.wait)
        self.ready_seen = []
        self.supervisor = self.make()

    def make(self, **kwargs):
        deadlines = dict(interval=0.1, startup_timeout=1,
                         stall_timeout=0.25, lifetime=2, stop_timeout=0.2)
        deadlines.update(kwargs)
        return BridgeSupervisor(self.close, policy=self.policy, identity=self.identity,
            owner=self.owner, observe=lambda: self.observe(), spawn=self.spawn,
            clock=lambda: self.now, **deadlines)

    def normal(self):
        return (PCM(48000, 'S16_LE', 2, 123, self.ticks * 10, self.ticks * 10),
                PCM(48000, 'S16_LE', 2, 456, self.ticks * 10, self.ticks * 10))

    def wait(self, seconds):
        self.ready_seen.append(self.supervisor.ready)
        self.ticks += 1
        self.now += seconds

    def run_bridge(self):
        self.supervisor.run(MAC, self.event)

    def test_prepare_on_playback_before_capture_then_both_must_progress(self):
        prepared = Mock()
        self.supervisor = self.make(prepare=prepared)
        self.observe = lambda: self.normal() if prepared.called else (self.normal()[0], None)
        self.run_bridge()
        prepared.assert_called_once_with(48000, 'S16_LE')
        self.assertEqual(self.ready_seen, [False, True, True, True])

    def test_changed_playback_after_prepare_rejected_before_ready(self):
        prepared = Mock()
        self.supervisor = self.make(prepare=prepared)
        def observe():
            if not prepared.called:
                return self.normal()[0], None
            return tuple(PCM(44100, p.sample_format, 2, p.owner, p.hw_ptr, p.appl_ptr)
                         for p in self.normal())
        self.observe = observe
        with self.assertRaisesRegex(RuntimeError, 'negotiation changed'):
            self.run_bridge()
        prepared.assert_called_once()
        self.assertFalse(self.supervisor.ready)

    def test_prepare_failure_stops_muted_before_child(self):
        self.supervisor = self.make(prepare=Mock(side_effect=RuntimeError('prepare failed')))
        self.observe = lambda: (self.normal()[0], None)
        with self.assertRaisesRegex(RuntimeError, 'prepare failed'):
            self.run_bridge()
        self.close.assert_called_once()
        self.child.terminate.assert_called_once()
        self.assertFalse(self.supervisor.ready)

    def test_capture_never_opens_after_prepare_times_out(self):
        prepared = Mock()
        self.supervisor = self.make(prepare=prepared)
        self.event.is_set = lambda: False
        self.observe = lambda: (self.normal()[0], None)
        with self.assertRaisesRegex(RuntimeError, 'startup deadline'):
            self.run_bridge()
        prepared.assert_called_once()
        self.assertFalse(any(self.ready_seen))

    def test_prepare_time_counts_against_deadline(self):
        def prepare(rate, fmt):
            self.now += 2
        self.supervisor = self.make(prepare=prepare)
        with self.assertRaisesRegex(RuntimeError, 'deadline during preparation'):
            self.run_bridge()
        self.assertFalse(self.supervisor.ready)

    def test_exact_argv_owned_process_and_repeated_checks(self):
        self.run_bridge()
        self.assertEqual(self.spawn.call_args.args[0], ['bluealsa-aplay', '--profile-a2dp',
            '--pcm=hw:Loopback,0,2', '--volume=software', '--single-audio', MAC])
        self.assertEqual(self.spawn.call_args.kwargs, dict(executable='/usr/bin/bluealsa-aplay',
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            shell=False, close_fds=True))
        self.assertGreaterEqual(self.identity.call_count, 5)
        self.assertGreaterEqual(self.owner.call_count, 22)
        self.assertEqual(self.ready_seen, [False, True, True, True])
        self.assertFalse(self.supervisor.ready)
        self.assertIsNone(self.supervisor.negotiated)
        self.assertIsNone(self.supervisor.pid)
        self.child.terminate.assert_called_once()
        self.child.wait.assert_called_once_with(timeout=0.2)
        self.close.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, 'single-use'):
            self.run_bridge()

    def test_disallowed_mac_never_spawns(self):
        for address in ('*', '00:00:00:00:00:00', 'AA:BB:CC:DD:EE:FF'):
            with self.assertRaises(ValueError):
                self.make().run(address, self.event)
        self.spawn.assert_not_called()

    def test_revoked_policy_and_identity_disconnect_stop(self):
        for name in ('policy', 'identity'):
            with self.subTest(target=name):
                self.setUp()
                target = getattr(self, name)
                target.side_effect = [target.return_value, target.return_value, RuntimeError('revoked')]
                with self.assertRaisesRegex(RuntimeError, 'revoked'):
                    self.run_bridge()
                self.child.terminate.assert_called_once()

    def test_owner_replacement_before_launch_and_during_monitor(self):
        for count in (1, 4, 8):
            self.setUp()
            self.owner.side_effect = [b's ":1.42"\n'] * count + [b's ":1.99"\n'] * 20
            with self.assertRaisesRegex(RuntimeError, 'owner changed'):
                self.run_bridge()
            if count < 6:
                self.spawn.assert_not_called()
            else:
                self.child.terminate.assert_called_once()

    def test_every_pointer_must_advance(self):
        for endpoint in (0, 1):
            for pointer in ('hw_ptr', 'appl_ptr'):
                self.setUp()
                def observe():
                    pair = list(self.normal())
                    values = vars(pair[endpoint]).copy()
                    values[pointer] = 0
                    pair[endpoint] = PCM(**values)
                    return tuple(pair)
                self.observe = observe
                with self.assertRaisesRegex(RuntimeError, 'stalled'):
                    self.run_bridge()
                self.assertFalse(any(self.ready_seen))
                self.child.terminate.assert_called_once()

    def test_mismatch_owner_change_format_change_regression_and_closed(self):
        for failure in ('mismatch', 'owner', 'format', 'regress', 'closed', 'capture_owner'):
            self.setUp()
            def observe():
                pair = list(self.normal())
                if self.ticks == 2:
                    if failure == 'closed':
                        return None, None
                    if failure == 'format':
                        return tuple(PCM(44100, p.sample_format, 2, p.owner, p.hw_ptr, p.appl_ptr) for p in pair)
                    index = 1 if failure == 'capture_owner' else 0
                    values = vars(pair[index]).copy()
                    values.update({'mismatch': {'rate': 44100}, 'owner': {'owner': 999},
                                   'capture_owner': {'owner': 999}, 'regress': {'hw_ptr': 0}}[failure])
                    pair[index] = PCM(**values)
                return tuple(pair)
            self.observe = observe
            with self.assertRaises(RuntimeError):
                self.run_bridge()
            self.child.terminate.assert_called_once()

    def test_startup_and_lifetime_deadlines(self):
        self.event.is_set = lambda: False
        self.observe = lambda: (None, None)
        with self.assertRaisesRegex(RuntimeError, 'startup deadline'):
            self.run_bridge()
        self.setUp()
        self.event.is_set = lambda: False
        with self.assertRaisesRegex(RuntimeError, 'lifetime deadline'):
            self.run_bridge()

    def test_phone_can_start_late_but_must_progress_before_120_seconds(self):
        self.assertEqual(BridgeSupervisor(self.close).startup_timeout, 120)
        self.supervisor = self.make(startup_timeout=120, lifetime=300,
                                    interval=0.5, stall_timeout=2)
        self.event.is_set = lambda: self.now >= 120
        self.observe = lambda: (None, None) if self.now < 119 else self.normal()
        self.run_bridge()
        self.assertFalse(any(self.ready_seen[:-1]))
        self.assertTrue(self.ready_seen[-1])
        self.close.assert_called_once()
        self.child.terminate.assert_called_once()
        self.assertFalse(self.supervisor.ready)

    def test_phone_start_window_is_bounded_even_with_first_pcm_at_deadline(self):
        for first_pcm in (120, 121):
            self.setUp()
            self.supervisor = self.make(startup_timeout=120, lifetime=300, interval=1)
            self.event.is_set = lambda: False
            self.observe = lambda: (None, None) if self.now < first_pcm else self.normal()
            with self.assertRaisesRegex(RuntimeError, 'startup deadline'):
                self.run_bridge()
            self.assertEqual(self.now, 120)
            self.assertFalse(any(self.ready_seen))
            self.close.assert_called_once()
            self.child.terminate.assert_called_once()
        with self.assertRaisesRegex(ValueError, '120 seconds'):
            self.make(startup_timeout=120.001)

    def test_late_observation_is_not_fresh_progress(self):
        normal = self.observe
        def delayed():
            if self.ticks == 2:
                self.now += 0.5
            return normal()
        self.observe = delayed
        with self.assertRaisesRegex(RuntimeError, 'observation late'):
            self.run_bridge()

    def test_exit_spawn_failure_and_read_failure(self):
        for failure in ('exit', 'spawn', 'read'):
            self.setUp()
            if failure == 'exit':
                self.child.poll.return_value = 1
            elif failure == 'spawn':
                self.spawn.side_effect = OSError('spawn')
            else:
                self.observe = Mock(side_effect=OSError('read'))
            with self.assertRaises((RuntimeError, OSError)):
                self.run_bridge()
            self.close.assert_called_once()
            if failure == 'read':
                self.child.terminate.assert_called_once()
            else:
                self.child.terminate.assert_not_called()

    def test_callback_failure_still_stops_and_kill_is_bounded(self):
        order = []
        self.close.side_effect = lambda reason: order.append('mute')
        self.child.terminate.side_effect = lambda: order.append('term')
        self.child.wait.side_effect = [subprocess.TimeoutExpired('child', 0.2), 0]
        self.run_bridge()
        self.assertEqual(order, ['mute', 'term'])
        self.child.kill.assert_called_once()
        self.assertEqual([c.kwargs for c in self.child.wait.call_args_list], [{'timeout': 0.2}] * 2)
        self.setUp()
        self.close.side_effect = RuntimeError('mute failed')
        self.child.wait.side_effect = subprocess.TimeoutExpired('child', 0.2)
        with self.assertRaisesRegex(RuntimeError, 'fail-closed callback.*owned bridge stop'):
            self.run_bridge()
        self.child.kill.assert_called_once()
        self.assertEqual(self.supervisor.pid, 123)  # retain ownership on failed reap
        self.assertFalse(self.supervisor.ready)

    def test_repeated_real_identity_rejects_each_revoked_flag(self):
        from bluetooth_identity import verify_identity
        for flag in ('Paired', 'Trusted', 'Connected'):
            self.setUp()
            self.supervisor.identity = verify_identity
            def query(argv, **kwargs):
                values = {'Address': f's "{MAC}"\n'.encode(),
                          'Adapter': b'o "/org/bluez/hci0"\n',
                          'Paired': b'b true\n', 'Trusted': b'b true\n',
                          'Connected': b'b true\n'}
                if self.ticks >= 1:
                    values[flag] = b'b false\n'
                return SimpleNamespace(returncode=0, stderr=b'', stdout=values[argv[-1]])
            with patch('bluetooth_identity.os.geteuid', return_value=0), patch(
                    'bluetooth_identity.subprocess.run', side_effect=query):
                with self.assertRaisesRegex(RuntimeError, flag):
                    self.run_bridge()
            self.child.terminate.assert_called_once()

    def test_real_owned_child_is_reaped_without_signalling_other_child(self):
        # Only disposable local Python children; never BlueALSA or an audio device.
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        owned = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.supervisor._child = owned
            started = time.monotonic()
            self.supervisor._stop()
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNotNone(owned.poll())
            self.assertIsNone(other.poll())
            self.supervisor._stop()
        finally:
            for child in (owned, other):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)

    def test_cancel_before_spawn_and_invalid_deadlines(self):
        self.event.is_set = lambda: True
        self.run_bridge()
        self.spawn.assert_not_called()
        self.close.assert_called_once()
        for value in (0, -1, float('inf'), float('nan'), True):
            with self.assertRaises(ValueError):
                BridgeSupervisor(self.close, lifetime=value)


if __name__ == '__main__':
    unittest.main()
