"""One audio job at a time (live monitor, level test, recording, noise check, clock check) and a broadcast of its events."""
import asyncio
import threading
import time

from ..session import RecordConfig, Session

STOP_TIMEOUT_SECONDS = 10


class Busy(Exception):
    pass


class Bus:
    """Fans events out to every browser connected to /api/events. Publishing is safe from any thread."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subs: set[asyncio.Queue] = set()
        self.lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=400)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def publish(self, event: dict) -> None:
        if self.loop is None:
            return
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            self.loop.call_soon_threadsafe(self._put, q, event)

    @staticmethod
    def _put(q: asyncio.Queue, event: dict) -> None:
        if q.full():
            try:
                q.get_nowait()                  # a slow browser loses the oldest reading, never blocks the audio
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(event)


class JobManager:
    def __init__(self, io, bus: Bus):
        self.io, self.bus = io, bus
        self.lock = threading.Lock()  # Admission is shared with Auto Tune.
        self._state_lock = threading.Lock()  # The worker can finish while admission waits.
        self.thread: threading.Thread | None = None
        self.session: Session | None = None
        self.kind: str | None = None
        self.state = "idle"
        self.message = ""
        self.started = 0.0
        self.last_result: dict | None = None
        self.last_error: str | None = None
        self.external_busy = lambda: False

    def status(self) -> dict:
        with self._state_lock:
            return self._status()

    def _status(self) -> dict:
        phase = self._phase()
        running = phase != "idle"
        return dict(phase=phase, running=running, kind=self.kind if running else None, state=self.state if running else "idle",
                    message=self.message if running else "", elapsed=round(time.monotonic() - self.started, 1) if running else 0,
                    last_result=self.last_result, last_error=self.last_error)

    def _phase(self) -> str:
        if self.session is None:
            return "idle"
        return "stopping" if self.session.cancel.is_set() else "running"

    def _emit(self, session, kind, event):
        with self._state_lock:
            if self.session is not session:
                return
            if event["type"] == "state":
                self.state, self.message = event["state"], event.get("message", "")
                event = {**event, "phase": self._phase()}
            elif event["type"] == "done":
                self.last_result, self.last_error = event["result"], None
            elif event["type"] == "error":
                self.last_error = event["message"]
            self.bus.publish({**event, "job": kind})

    def start(self, kind: str, cfg: RecordConfig, **kw) -> None:
        method = {"monitor": "run_monitor", "record": "run_record", "noise": "run_noise", "clock": "run_clock"}[kind]
        with self.lock:
            if self.external_busy():
                raise Busy('Auto Tune owns the audio devices. Stop it first.')
            current = self.status()
            if current["running"]:
                if current["kind"] != "monitor" or kind == "monitor":
                    raise Busy(f"A {current['kind']} is already running. Stop it first.")
                self.cancel(wait=True)
                if self.status()["running"]:
                    raise Busy("The live meter is still releasing the microphone. Try again when it stops.")

            def emit(event):
                self._emit(session, kind, event)

            session = Session(self.io, emit)

            def work():
                try:
                    getattr(session, method)(cfg, **kw)
                finally:
                    with self._state_lock:
                        self.session = None
                        self.bus.publish({"type": "idle", "job": kind, "phase": "idle"})

            with self._state_lock:
                thread = threading.Thread(target=work, daemon=True, name=f"octet-{kind}")
                thread.start()
                self.thread, self.session = thread, session
                self.kind, self.state, self.message = kind, "starting", ""
                self.started = time.monotonic()
                self.last_result = self.last_error = None
                self.bus.publish({"type": "status", **self._status()})

    def cancel(self, wait: bool = False) -> bool:
        with self._state_lock:
            thread, session = self.thread, self.session
            if session is None:
                return False
            if not session.cancel.is_set():
                session.cancel.set()
                self.bus.publish({"type": "status", **self._status()})
        if wait:
            thread.join(timeout=STOP_TIMEOUT_SECONDS)
        return True
