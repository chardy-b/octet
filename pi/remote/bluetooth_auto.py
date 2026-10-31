"""Source-only wiring from fresh A2DP stream generations to the guarded controller.

A TransportObserver runs on one D-Bus/GLib thread. Its callbacks only update a tiny
lock-protected cache of the current immutable StreamGeneration and enqueue a record;
they never block and never touch the controller, DSP or MPD. A single worker thread
consumes the records and calls BluetoothControl.start_auto / stop_auto, handing the
controller the cache-only is_current validator, so the worker and the controller's
threads never perform D-Bus reads or mutate observer state.

An invalidation clears the cache synchronously, so a start already in flight refuses
its final unmute, and the controller's watchdog fails the lost generation. A start that
fails is not retried; only a new active generation is. Any failure of the worker, the
adapter or the GLib loop invalidates the cache and stops the automatic session, and is
contained: manual control, Tidal and AirPlay are never affected. Idle connections never
reach this module; a stream already active at (re)synchronisation arrives once, as a
new generation.
"""
import logging
import queue
import threading
import time

from bluetooth_transport import StreamGeneration, SystemBusAdapter, TransportObserver

START, STOP = 'start', 'stop'


class AutoBluetooth:
    def __init__(self, controller):
        self.controller = controller
        self._lock = threading.Lock()  # guards _current only; never held across a call
        self._current = None
        self._closed = False
        self._failed = None
        self._active = None  # worker-only: last generation start_auto was attempted for
        self._queue = queue.Queue()
        self._thread = None

    # Observer callbacks (D-Bus thread): nonblocking, no I/O.
    def on_generation(self, generation):
        with self._lock:
            if self._closed:
                return
            previous, self._current = self._current, generation
        if previous == generation:
            return
        if previous is not None:
            self._queue.put((STOP, previous, 'Bluetooth stream replaced'))
        self._queue.put((START, generation, None))

    def on_invalidate(self, generation, reason):
        with self._lock:
            matches = self._current == generation
            if matches:
                self._current = None
        if matches:
            self._queue.put((STOP, generation, str(reason)))

    def is_current(self, generation):
        """Cache-only validator for the controller: never calls D-Bus."""
        with self._lock:
            return isinstance(generation, StreamGeneration) and self._current == generation

    def fail(self, reason):
        """Invalidate the cache and stop the current automatic session; never raises."""
        with self._lock:
            generation, self._current = self._current, None
            closed = self._closed
        if generation is not None and not closed:
            self._queue.put((STOP, generation, str(reason)))

    # Worker thread.
    def start_worker(self):
        self._thread = threading.Thread(target=self._run, name='octet-bluetooth-auto',
                                        daemon=True)
        self._thread.start()

    def close(self, timeout=5):
        """Fail closed: invalidate the cache, drain the worker, stop the session directly.

        Queued STOP records ahead of the sentinel are handled by the worker first. If the
        worker is stuck inside start_auto, the stop is issued from here after the timeout.
        The controller scopes stop_auto to the generation, so a manual session is safe.
        If that stop fails, close raises RuntimeError and keeps the generation: output is
        not known to be muted. Calling close again retries exactly that generation; once
        it succeeds, further calls do nothing.
        """
        with self._lock:
            first = not self._closed
            self._closed = True
            self._current = None
        if first:
            self._queue.put(None)
            if self._thread is not None:
                self._thread.join(timeout)
        generation = self._active
        if generation is None:
            return
        try:
            self.controller.stop_auto(generation, 'Automatic Bluetooth closed')
        except Exception as error:
            logging.exception('Automatic Bluetooth stop on close failed')
            raise RuntimeError('Automatic Bluetooth stop on close failed: '
                               f'{type(error).__name__}') from error
        if self._active == generation:
            self._active = None

    def join(self, timeout):
        """Wait until every queued record has been handled (for tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def _run(self):
        try:
            while True:
                record = self._queue.get()
                try:
                    if record is None:
                        return
                    self._handle(*record)
                except Exception:
                    logging.exception('Automatic Bluetooth action failed')
                finally:
                    self._queue.task_done()
        except BaseException:
            # Fatal exit: nothing will drain the queue, so stop the session directly.
            logging.exception('Automatic Bluetooth worker died')
            with self._lock:
                self._current = None
            generation, self._active = self._active, None
            if generation is not None:
                try:
                    self.controller.stop_auto(generation, 'Automatic Bluetooth worker died')
                except BaseException:
                    logging.exception('Automatic Bluetooth stop after worker death failed')

    def _handle(self, action, generation, reason):
        if action == START:
            if generation == self._failed or not self.is_current(generation):
                return
            self._active = generation
            try:
                self.controller.start_auto(generation, self.is_current)
            except Exception:
                self._failed = generation  # latched: no retry on this edge
                raise
        else:
            self.controller.stop_auto(generation, reason)
            if self._active == generation:
                self._active = None


def _glib_loop():
    from gi.repository import GLib
    return GLib.MainLoop()


def run_observer(runtime, adapter_factory=SystemBusAdapter, observer_factory=TransportObserver,
                 loop_factory=_glib_loop):
    """D-Bus/GLib thread body. Subscribe, then snapshot, then run; contain all failures."""
    try:
        loop = loop_factory()
        adapter = adapter_factory()
        observer = observer_factory(adapter, runtime.on_generation, runtime.on_invalidate)
        adapter.connect(observer)
        observer.start()
        loop.run()
        runtime.fail('Bluetooth observer loop ended')
    except BaseException as error:
        logging.warning('Automatic Bluetooth observer unavailable: %s', error)
        runtime.fail(f'Bluetooth observer failed: {error}')


def start_auto_bluetooth(controller):
    """Start the optional runtime; returns it, or None. Never raises into the server."""
    try:
        runtime = AutoBluetooth(controller)
        runtime.start_worker()
        threading.Thread(target=run_observer, args=(runtime,), name='octet-bluetooth-glib',
                         daemon=True).start()
        return runtime
    except Exception:
        logging.exception('Automatic Bluetooth could not start; manual control remains')
        return None
