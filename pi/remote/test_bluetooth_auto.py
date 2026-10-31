import threading
import unittest
from unittest.mock import patch

import bluetooth_auto
from bluetooth_auto import AutoBluetooth, run_observer, start_auto_bluetooth
from bluetooth_transport import StreamGeneration, TransportObserver, TransportUnavailable
from test_bluetooth_transport import (BLUEALSA, BLUEZ, MAC, MAC2, TP, TP2, FakeBus, dev,
                                      device_props, transport_props)

IFACE = 'org.bluez.MediaTransport1'
WAIT = 5


def generation(counter=1):
    return StreamGeneration(BLUEZ, BLUEALSA, dev(MAC), TP, MAC, 0, counter)


class FakeController:
    """Records calls; start_auto can block so tests can interleave callbacks."""

    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.unmuted = []
        self.error = None
        self.threads = set()

    def start_auto(self, gen, is_current):
        self.threads.add(threading.current_thread())
        self.calls.append(('start', gen))
        self.started.set()
        self.release.wait(WAIT)
        if self.error:
            raise self.error
        if is_current(gen):  # the controller's guarded final check before unmute
            self.unmuted.append(gen)

    def stop_auto(self, gen, reason='Bluetooth stream ended'):
        self.threads.add(threading.current_thread())
        self.calls.append(('stop', gen))
        self.stopped.set()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.runtime = AutoBluetooth(self.controller)
        self.runtime.start_worker()
        self.addCleanup(self.runtime.close)

    def drain(self):
        self.assertTrue(self.runtime.join(WAIT))

    def test_active_generation_queues_one_start_with_cached_validator(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.runtime.on_generation(gen)  # duplicate
        self.drain()
        self.assertEqual(self.controller.calls, [('start', gen)])
        self.assertEqual(self.controller.unmuted, [gen])
        self.assertNotIn(threading.current_thread(), self.controller.threads)

    def test_invalidation_during_blocked_start_is_synchronous_and_prevents_unmute(self):
        gen = generation()
        self.controller.release.clear()
        self.runtime.on_generation(gen)
        self.assertTrue(self.controller.started.wait(WAIT))
        self.assertTrue(self.runtime.is_current(gen))
        self.runtime.on_invalidate(gen, 'transport left active')  # returns while start blocked
        self.assertFalse(self.runtime.is_current(gen))
        self.controller.release.set()
        self.drain()
        self.assertEqual(self.controller.unmuted, [])
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])

    def test_invalidate_stops_exact_generation_only(self):
        old, new = generation(1), generation(2)
        self.runtime.on_generation(new)
        self.drain()
        self.runtime.on_invalidate(old, 'late duplicate')  # stale: not the cached one
        self.drain()
        self.assertTrue(self.runtime.is_current(new))
        self.assertEqual(self.controller.calls, [('start', new)])
        self.runtime.on_invalidate(new, 'transport left active')
        self.drain()
        self.assertEqual(self.controller.calls, [('start', new), ('stop', new)])
        self.runtime.on_invalidate(new, 'duplicate')
        self.drain()
        self.assertEqual(self.controller.calls.count(('stop', new)), 1)

    def test_failed_generation_is_not_retried(self):
        gen = generation()
        self.controller.error = RuntimeError('Music player is not stopped')
        self.runtime.on_generation(gen)
        self.drain()
        self.runtime.on_generation(gen)  # replay of the same failed edge
        self.drain()
        self.assertEqual(self.controller.calls, [('start', gen)])
        self.controller.error = None
        newer = generation(2)
        self.runtime.on_generation(newer)
        self.drain()
        self.assertEqual(self.controller.calls[-1], ('start', newer))

    def test_stale_queued_start_is_skipped(self):
        first, second = generation(1), generation(2)
        self.controller.release.clear()
        self.runtime.on_generation(first)
        self.assertTrue(self.controller.started.wait(WAIT))
        self.runtime.on_invalidate(first, 'idle')
        self.runtime.on_generation(second)
        self.controller.release.set()
        self.drain()
        self.assertEqual([c for c in self.controller.calls if c[0] == 'start'],
                         [('start', first), ('start', second)])
        self.assertEqual(self.controller.unmuted, [second])

    def test_worker_survives_controller_errors_in_start_and_stop(self):
        class Boom(FakeController):
            def stop_auto(self, gen, reason=''):
                raise OSError('dsp gone')
        self.runtime.close()
        controller = Boom()
        controller.error = RuntimeError('refused')
        runtime = AutoBluetooth(controller)
        runtime.start_worker()
        runtime.on_generation(generation(1))
        runtime.on_invalidate(generation(1), 'idle')
        controller.error = None
        runtime.on_generation(generation(2))
        self.assertTrue(runtime.join(WAIT))
        self.assertIn(('start', generation(2)), controller.calls)
        with self.assertRaises(RuntimeError):  # stop still failing: close must not claim success
            runtime.close()
        controller.stop_auto = FakeController.stop_auto.__get__(controller)
        runtime.close()

    def test_fail_invalidates_cache_and_stops_running_session(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.drain()
        self.runtime.fail('GLib loop ended')
        self.assertFalse(self.runtime.is_current(gen))
        self.drain()
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])

    def test_fail_stops_session_even_when_cache_was_already_cleared(self):
        gen = generation()
        self.controller.release.clear()
        self.runtime.on_generation(gen)
        self.assertTrue(self.controller.started.wait(WAIT))
        self.runtime.fail('adapter lost')
        self.controller.release.set()
        self.drain()
        self.assertEqual(self.controller.unmuted, [])
        self.assertEqual(self.controller.calls[-1], ('stop', gen))

    def test_is_current_never_raises_and_rejects_non_generations(self):
        self.assertFalse(self.runtime.is_current(None))
        self.assertFalse(self.runtime.is_current(object()))

    def die_on_next_record(self, runtime):
        """Make the worker exit fatally on the next record it dequeues."""
        return patch.object(runtime, '_handle', side_effect=KeyboardInterrupt)

    def test_fatal_worker_exit_stops_active_session_directly_without_queue(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.drain()
        self.assertEqual(self.controller.calls, [('start', gen)])
        with self.die_on_next_record(self.runtime):
            self.runtime.on_invalidate(gen, 'transport left active')
            self.assertFalse(self.runtime.is_current(gen))  # synchronous
            self.runtime._thread.join(WAIT)
        self.assertFalse(self.runtime._thread.is_alive())
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])
        self.assertEqual(self.runtime._queue.qsize(), 0)

    def test_fatal_exit_during_start_stops_inflight_generation(self):
        gen = generation()
        self.controller.release.clear()
        self.runtime.on_generation(gen)
        self.assertTrue(self.controller.started.wait(WAIT))
        self.controller.error = KeyboardInterrupt()
        self.controller.release.set()
        self.runtime._thread.join(WAIT)
        self.assertFalse(self.runtime.is_current(gen))
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])

    def test_fatal_exit_with_nothing_active_calls_no_stop(self):
        with self.die_on_next_record(self.runtime):
            self.runtime.on_generation(generation())
            self.runtime._thread.join(WAIT)
        self.assertEqual(self.controller.calls, [])
        self.assertFalse(self.runtime.is_current(generation()))

    def test_fatal_exit_after_successful_stop_does_not_stop_again(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.runtime.on_invalidate(gen, 'idle')
        self.drain()
        with self.die_on_next_record(self.runtime):
            self.runtime.on_generation(generation(2))
            self.runtime._thread.join(WAIT)
        self.assertEqual(self.controller.calls.count(('stop', gen)), 1)
        self.assertNotIn(('stop', generation(2)), self.controller.calls)

    def test_fatal_exit_stop_failure_is_contained(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.drain()
        with patch.object(self.controller, 'stop_auto', side_effect=OSError('dsp gone')) as stop, \
                self.die_on_next_record(self.runtime):
            self.runtime.on_invalidate(gen, 'idle')
            self.runtime._thread.join(WAIT)
        stop.assert_called_once()
        self.assertFalse(self.runtime._thread.is_alive())


    def test_close_with_active_session_invalidates_and_stops_exact_generation_once(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.drain()
        self.runtime.close()
        self.assertFalse(self.runtime.is_current(gen))
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])
        self.assertFalse(self.runtime._thread.is_alive())

    def test_close_stop_failure_raises_keeps_generation_and_retries_exact_generation(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.drain()
        real_stop, failures = self.controller.stop_auto, [OSError('dsp socket gone')]
        def stop_auto(stopped, reason=''):
            if failures:
                self.controller.calls.append(('stop-failed', stopped))
                raise failures.pop()
            real_stop(stopped, reason)
        with patch.object(self.controller, 'stop_auto', stop_auto):
            with self.assertRaises(RuntimeError) as caught:
                self.runtime.close()
            self.assertNotIsInstance(caught.exception, OSError)
            self.assertLess(len(str(caught.exception)), 200)
            self.assertEqual(self.runtime._active, gen)
            self.assertFalse(self.runtime.is_current(gen))
            self.runtime.on_generation(generation(2))  # closed: no new stream can enter
            self.assertFalse(self.runtime.is_current(generation(2)))
            self.assertEqual(self.runtime._queue.qsize(), 0)
            self.runtime.close()  # retry of the same generation succeeds
            self.assertIsNone(self.runtime._active)
            self.runtime.close()  # idempotent
        self.assertEqual(self.controller.calls,
                         [('start', gen), ('stop-failed', gen), ('stop', gen)])

    def test_close_holds_no_lock_across_controller_stop(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.drain()
        held = []
        def stop_auto(stopped, reason=''):
            held.append(self.runtime._lock.locked())
            self.runtime.is_current(stopped)  # would deadlock if the lock were held
        with patch.object(self.controller, 'stop_auto', stop_auto):
            self.runtime.close()
        self.assertEqual(held, [False])

    def test_close_during_blocked_start_prevents_unmute_and_stops_inflight(self):
        gen = generation()
        self.controller.release.clear()
        self.runtime.on_generation(gen)
        self.assertTrue(self.controller.started.wait(WAIT))
        self.runtime.close(timeout=0.05)  # worker is stuck inside start_auto
        self.assertFalse(self.runtime.is_current(gen))
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])
        self.controller.release.set()
        self.runtime._thread.join(WAIT)
        self.assertFalse(self.runtime._thread.is_alive())
        self.assertEqual(self.controller.unmuted, [])

    def test_close_with_invalidation_already_queued_stops_once(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.drain()
        self.runtime.on_invalidate(gen, 'idle')
        self.runtime.close()
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])

    def test_close_is_repeatable_and_idle_close_stops_nothing(self):
        self.runtime.close()
        self.runtime.close()
        self.assertEqual(self.controller.calls, [])
        gen = generation()
        other = AutoBluetooth(self.controller)
        other.start_worker()
        other.on_generation(gen)
        self.assertTrue(other.join(WAIT))
        other.close()
        other.close()
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])

    def test_generation_after_close_cannot_enqueue_a_start(self):
        self.runtime.close()
        self.runtime.on_generation(generation())
        self.assertFalse(self.runtime.is_current(generation()))
        self.assertEqual(self.runtime._queue.qsize(), 0)
        self.assertEqual(self.controller.calls, [])

    def test_close_before_queued_start_runs_never_starts(self):
        runtime = AutoBluetooth(self.controller)  # worker not started yet
        runtime.on_generation(generation())
        runtime.close()
        runtime.start_worker()
        self.assertTrue(runtime.join(WAIT))
        self.assertEqual(self.controller.calls, [])


class ObserverIntegrationTests(unittest.TestCase):
    """Real TransportObserver on a fake bus: callbacks must not touch the worker."""

    def setUp(self):
        self.bus = FakeBus()
        for mac in (MAC, MAC2):
            self.bus.add(dev(mac), device_props(mac))
        self.bus.add(TP, transport_props(MAC))
        self.bus.add(TP2, transport_props(MAC2))
        self.controller = FakeController()
        self.runtime = AutoBluetooth(self.controller)
        self.runtime.start_worker()
        self.addCleanup(self.runtime.close)
        self.obs = TransportObserver(self.bus, self.runtime.on_generation,
                                     self.runtime.on_invalidate)

    def change(self, state, path=TP):
        self.bus.set(path, IFACE, State=state)
        self.obs.on_properties_changed(BLUEZ, path, IFACE, {'State': state})

    def test_boot_active_starts_once_and_other_idle_does_nothing(self):
        self.bus.set(TP, IFACE, State='active')
        self.obs.start()
        self.change('active')
        self.change('idle', TP2)
        self.assertTrue(self.runtime.join(WAIT))
        self.assertEqual([(action, gen.counter) for action, gen in self.controller.calls], [('start', 1)])

    def test_fresh_active_then_idle_starts_then_stops_exact_generation(self):
        self.obs.start()
        self.change('active')
        self.assertTrue(self.runtime.join(WAIT))
        gen = self.controller.calls[0][1]
        self.assertEqual((gen.address, gen.transport_path, gen.counter), (MAC, TP, 1))
        self.assertEqual(self.controller.unmuted, [gen])
        reads = len(self.bus.reads)
        self.change('idle')
        self.assertTrue(self.runtime.join(WAIT))
        self.assertEqual(self.controller.calls, [('start', gen), ('stop', gen)])
        # The worker and cache never query the bus: only the observer's own reads.
        self.assertEqual(len(self.bus.reads), reads)

    def test_validator_is_cache_only_no_bus_reads(self):
        self.obs.start()
        self.change('active')
        self.assertTrue(self.runtime.join(WAIT))
        reads = len(self.bus.reads)
        self.assertTrue(self.runtime.is_current(self.controller.calls[0][1]))
        self.assertEqual(len(self.bus.reads), reads)

    def test_owner_loss_stops_generation(self):
        self.obs.start()
        self.change('active')
        self.assertTrue(self.runtime.join(WAIT))
        gen = self.controller.calls[0][1]
        self.bus.owners['org.bluealsa'] = ':1.77'
        self.obs.on_name_owner_changed('org.bluealsa', BLUEALSA, ':1.77')
        self.assertTrue(self.runtime.join(WAIT))
        self.assertFalse(self.runtime.is_current(gen))
        # The old session stops before the still-active stream is re-admitted.
        actions = [(action, call.bluealsa_owner) for action, call in self.controller.calls]
        self.assertEqual(actions[1:], [('stop', gen.bluealsa_owner), ('start', ':1.77')])

    def test_blocked_start_then_idle_edge_never_unmutes(self):
        self.obs.start()
        self.controller.release.clear()
        self.change('active')
        self.assertTrue(self.controller.started.wait(WAIT))
        self.change('idle')
        self.controller.release.set()
        self.assertTrue(self.runtime.join(WAIT))
        self.assertEqual(self.controller.unmuted, [])


class FakeAdapter:
    def __init__(self, fail=None):
        self.fail, self.connected = fail, None

    def connect(self, observer):
        if self.fail == 'connect':
            raise TransportUnavailable('python3-dbus is unavailable')
        self.connected = observer


class FakeLoop:
    def __init__(self, error=None):
        self.error, self.ran, self.quit_called = error, threading.Event(), False

    def run(self):
        self.ran.set()
        if self.error:
            raise self.error

    def quit(self):
        self.quit_called = True


class RunObserverTests(unittest.TestCase):
    def setUp(self):
        self.controller = FakeController()
        self.runtime = AutoBluetooth(self.controller)
        self.runtime.start_worker()
        self.addCleanup(self.runtime.close)
        self.events = []

    def observer_factory(self, started=None):
        outer = self

        class Obs:
            def __init__(self, bus, on_generation, on_invalidate):
                outer.events.append('observer')
                self.bus, self.on_generation = bus, on_generation

            def start(self):
                outer.events.append('start')
        return Obs

    def test_subscribes_before_snapshot_then_runs_loop(self):
        adapter = FakeAdapter()
        loop = FakeLoop()
        orig = adapter.connect
        adapter.connect = lambda obs: (self.events.append('connect'), orig(obs))
        run_observer(self.runtime, lambda: adapter, self.observer_factory(), lambda: loop)
        self.assertEqual(self.events, ['observer', 'connect', 'start'])
        self.assertTrue(loop.ran.is_set())

    def test_loop_returning_or_raising_fails_runtime_and_stops_session(self):
        for error in (None, RuntimeError('glib died')):
            with self.subTest(error=error):
                gen = generation()
                self.runtime.on_generation(gen)
                self.assertTrue(self.runtime.join(WAIT))
                run_observer(self.runtime, FakeAdapter, self.observer_factory(),
                             lambda: FakeLoop(error))
                self.assertFalse(self.runtime.is_current(gen))
                self.assertTrue(self.runtime.join(WAIT))
                self.assertEqual(self.controller.calls[-1], ('stop', gen))

    def test_adapter_setup_failure_is_contained(self):
        gen = generation()
        self.runtime.on_generation(gen)
        self.assertTrue(self.runtime.join(WAIT))
        run_observer(self.runtime, lambda: FakeAdapter('connect'), self.observer_factory(),
                     FakeLoop)
        self.assertFalse(self.runtime.is_current(gen))
        run_observer(self.runtime, lambda: (_ for _ in ()).throw(TransportUnavailable('x')),
                     self.observer_factory(), FakeLoop)

    def test_missing_gi_is_contained(self):
        with patch.dict('sys.modules', {'gi': None, 'gi.repository': None}):
            run_observer(self.runtime, FakeAdapter, self.observer_factory())
        self.assertEqual(self.events, [])


class StartAutoBluetoothTests(unittest.TestCase):
    def test_returns_without_raising_and_runs_in_daemon_threads(self):
        created = []

        class FakeThread:
            def __init__(self, target=None, args=(), name=None, daemon=None):
                created.append((target, args, daemon))

            def start(self):
                pass
        with patch.object(bluetooth_auto.threading, 'Thread', FakeThread):
            runtime = start_auto_bluetooth(FakeController())
        self.assertIsInstance(runtime, AutoBluetooth)
        self.assertTrue(created)
        self.assertTrue(all(daemon for _, _, daemon in created))
        self.assertIn(run_observer, [target for target, _, _ in created])

    def test_thread_creation_failure_is_contained(self):
        with patch.object(bluetooth_auto.threading, 'Thread', side_effect=RuntimeError('no threads')):
            self.assertIsNone(start_auto_bluetooth(FakeController()))


if __name__ == '__main__':
    unittest.main()
