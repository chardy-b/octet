"""One audio job at a time (live monitor, level test, recording, noise check, clock check) and a broadcast of its events."""
import asyncio
import threading
import time

from ..session import RecordConfig, Session


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
        self.lock = threading.Lock()
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
        running = self.thread is not None and self.thread.is_alive()
        return dict(running=running, kind=self.kind if running else None, state=self.state if running else "idle",
                    message=self.message if running else "", elapsed=round(time.monotonic() - self.started, 1) if running else 0,
                    last_result=self.last_result, last_error=self.last_error)

    def _emit(self, kind):
        def emit(event):
            if event["type"] == "state":
                self.state, self.message = event["state"], event.get("message", "")
            elif event["type"] == "done":
                self.last_result, self.last_error = event["result"], None
            elif event["type"] == "error":
                self.last_error = event["message"]
            self.bus.publish({**event, "job": kind})
        return emit

    def start(self, kind: str, cfg: RecordConfig, **kw) -> None:
        with self.lock:
            if self.external_busy():
                raise Busy('Auto Tune owns the audio devices. Stop it first.')
            running = self.thread is not None and self.thread.is_alive()
            if running and self.kind == "monitor" and kind != "monitor":
                self.cancel(wait=True)                         # starting a measurement ends the live meter
            elif running:
                raise Busy(f"A {self.kind} is already running. Stop it first.")
            self.kind, self.state, self.message = kind, "starting", ""
            self.started = time.monotonic()
            self.last_result = self.last_error = None
            self.session = Session(self.io, self._emit(kind))
            method = {"monitor": "run_monitor", "record": "run_record", "noise": "run_noise", "clock": "run_clock"}[kind]
            session = self.session

            def work():
                try:
                    getattr(session, method)(cfg, **kw)
                finally:
                    self.bus.publish({"type": "idle", "job": kind})

            self.thread = threading.Thread(target=work, daemon=True, name=f"octet-{kind}")
            self.thread.start()

    def cancel(self, wait: bool = False) -> bool:
        t, s = self.thread, self.session
        if t is None or not t.is_alive() or s is None:
            return False
        s.cancel.set()
        if wait:
            t.join(timeout=10)
        return True
