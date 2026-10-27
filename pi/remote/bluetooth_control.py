"""Fail-closed recovery/watchdog foundation. Playback intentionally disabled.

Not registered in server.py or a service. No bridge, pairing, volume or unmute.
"""
from bluetooth_route import BluetoothRoute


class BluetoothControl:
    def __init__(self, server, stop_bridge):
        self.server = server
        self.route = BluetoothRoute(server)
        # Stop only a supervisor-owned bridge, never kill by name.
        self.stop_bridge = stop_bridge
        self.error = None
        self.errors = {}
        self.mute_verified = False

    def fail(self, reason):
        # Also serialize direct fail() calls. Emergency mute precedes disk/lock IO.
        with self.server.LOCK:
            self.error = reason
            self.errors = {}
            self.mute_verified = False
            try:
                self.route.mute()
                self.mute_verified = True
            except Exception as error:
                self.errors['mute'] = error
            try:
                self.stop_bridge()
            except Exception as error:
                self.errors['stop'] = error
            try:
                if self.route.exists():
                    # A failed stop must not skip restore, but leaves evidence and
                    # the flock held: bridge shutdown has not been established.
                    self.route.recover(cleanup=not self.errors)
            except Exception as error:
                self.errors['recovery'] = error
                self.mute_verified = False
            if self.errors:
                self.error = reason + '; ' + '; '.join(
                    f'{stage}: {error}' for stage, error in self.errors.items())
                raise RuntimeError(self.error) from next(iter(self.errors.values()))

    def startup(self):
        self.fail('Playback disabled; startup recovery required')

    def start(self, generation=None):
        with self.server.LOCK:
            self.fail('Bluetooth playback is disabled: runtime activation gates incomplete')
            raise RuntimeError(self.error)

    def stop(self, generation=None):
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
