"""Fail-closed recovery/watchdog foundation. Playback intentionally disabled.

Not registered in server.py or a service. Future integration must call startup
before accepting requests and tick under the same process control lock. No BlueZ,
bridge spawn, LAN endpoint, pairing, volume change or unmute is implemented.
"""
from bluetooth_route import BluetoothRoute


class BluetoothControl:
    def __init__(self, server, stop_bridge):
        self.server = server
        self.route = BluetoothRoute(server)
        # Future supervisor must stop only its own bridge, never kill by name.
        self.stop_bridge = stop_bridge
        self.error = None
        self.mute_verified = False

    def fail(self, reason):
        self.error = reason
        self.mute_verified = False
        try:
            self.route.mute()
            self.mute_verified = True
        finally:
            self.stop_bridge()
        if self.route.exists():
            try:
                self.route.recover()
            except Exception:
                self.mute_verified = False
                raise

    def startup(self):
        with self.server.LOCK:
            self.fail('Playback disabled; startup recovery required')

    def start(self, generation=None):
        with self.server.LOCK:
            self.fail('Bluetooth playback is disabled in this slice')
            raise RuntimeError(self.error)

    def stop(self, generation=None):
        with self.server.LOCK:
            # Even stale stop events fail closed; they never revive a session.
            self.fail('Bluetooth stopped or stale generation')

    def tick(self, generation=None):
        with self.server.LOCK:
            if not self.route.exists():
                return
            try:
                self.route.verify()
                if generation != self.route.record['generation']:
                    raise RuntimeError('Stale Bluetooth generation')
            except Exception as error:
                self.fail(str(error))
