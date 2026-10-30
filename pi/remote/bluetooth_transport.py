"""Source-only observer of fresh BlueZ A2DP-source streams on hci0. Starts nothing.

An observer turns exactly one *fresh* MediaTransport1 transition to 'active' into an
immutable StreamGeneration pinned to the unique bus owners of org.bluez and
org.bluealsa, the exact device and transport object paths, the canonical address and
the codec. Callbacks and signal payloads are hints only: every admission re-reads the
transport and Device1 properties from the pinned BlueZ owner twice and requires paired,
trusted and connected, so a signal alone never admits anything.

Not admitted, by design: paired/connected idle devices, transports that were already
active when the observer (re)synchronised (boot, daemon restart), duplicate active
callbacks, and a second device while the first holds the slot. After release() or
fail() a still-active stream is not re-admitted; only a new idle/pending -> active edge
is. Any bus-owner change, object removal, idle, disconnect, untrust or contradictory
read invalidates the generation. The admission policy (which phones may play) is the
caller's; this module neither reads it nor authorises playback.

Source-only is enforced by shape, not by a source UUID: on this sink-role receiver
BlueZ labels the transport with the local Audio Sink UUID (exact match required, no
other UUID or case), on an hci0 device path with a validated codec and identity.

Read-only Octet evidence: transport path .../dev_X/fd6 (sepN is tolerated, not seen),
Codec 0, UUID 110b. Still unverified: the org.bluealsa well-known name and the codecs
BlueALSA registers. Both are constants below; everything fails closed on mismatch.
"""
import re
from typing import NamedTuple

BLUEZ = 'org.bluez'
BLUEALSA = 'org.bluealsa'
ADAPTER_PATH = '/org/bluez/hci0'
TRANSPORT_IFACE = 'org.bluez.MediaTransport1'
DEVICE_IFACE = 'org.bluez.Device1'
# Live Octet (BlueZ 5.82/BlueALSA 4.3.1): this sink-role receiver's transport carries the
# local Audio Sink UUID even though the phone is the A2DP source. 110a is rejected.
LOCAL_SINK_UUID = '0000110b-0000-1000-8000-00805f9b34fb'
CODECS = (0x00, 0x02)  # A2DP SBC, MPEG-2,4 AAC
TIMEOUT = 2

_UNIQUE = re.compile(r':\d{1,10}\.\d{1,10}')
_TRANSPORT = re.compile(r'(/org/bluez/hci0/dev_((?:[0-9A-F]{2}_){5}[0-9A-F]{2}))(?:/sep\d{1,4})?/fd\d{1,6}')


class TransportUnavailable(RuntimeError):
    """A bus read failed, timed out, or disagreed; callers must fail closed."""


class StreamGeneration(NamedTuple):
    bluez_owner: str
    bluealsa_owner: str
    device_path: str
    transport_path: str
    address: str
    codec: int
    counter: int


class TransportObserver:
    """Deterministic state machine; the bus adapter feeds it signals on one thread.

    bus provides name_owner(name) -> unique owner or None, managed_objects(owner) ->
    {path: {interface: properties}} and get_all(owner, path, interface) -> properties,
    each bounded by its own timeout and raising TransportUnavailable on failure.
    """

    def __init__(self, bus, on_generation, on_invalidate, codecs=CODECS, admit=None):
        self._bus, self._emit, self._report = bus, on_generation, on_invalidate
        self._codecs, self._admit_address = tuple(codecs), admit
        self._pins = None
        self._states = {}
        self._current = None
        self._counter = 0

    def start(self):
        """(Re)synchronise: pin owners and record every present transport's state.

        Anything already active is stale and is never admitted. Unavailable BlueZ or
        BlueALSA leaves the observer closed until a NameOwnerChanged resync succeeds.
        """
        self._pins, self._states = None, {}
        try:
            owners = self._owners()
            objects = self._bus.managed_objects(owners[0])
            states = {path: ifaces[TRANSPORT_IFACE].get('State')
                      for path, ifaces in objects.items() if TRANSPORT_IFACE in ifaces}
            if self._owners() != owners:
                return
        except Exception:
            return
        self._pins, self._states = owners, states

    def valid(self, generation):
        """True only for the current generation whose pins and properties still hold."""
        if self._pins is None or generation != self._current:
            return False
        if self._check_current():
            return True
        return False

    def release(self, generation):
        """Free the slot without a callback. A still-active stream is not re-admitted."""
        if generation == self._current:
            self._current = None

    def fail(self, generation, reason):
        """Invalidate the current generation after a failed route. No retry on this edge."""
        if generation == self._current:
            self._invalidate(reason)

    def on_properties_changed(self, sender, path, interface, changed):
        if self._pins is None or sender != self._pins[0]:
            return
        if interface == TRANSPORT_IFACE:
            if 'State' in changed:
                self._transition(path, changed['State'])
        elif interface == DEVICE_IFACE and self._current and path == self._current.device_path:
            self._check_current()

    def on_interfaces_added(self, sender, path, interfaces):
        if self._pins is None or sender != self._pins[0] or TRANSPORT_IFACE not in interfaces:
            return
        self._transition(path, interfaces[TRANSPORT_IFACE].get('State'))

    def on_interfaces_removed(self, sender, path, interfaces):
        if self._pins is None or sender != self._pins[0]:
            return
        if TRANSPORT_IFACE in interfaces:
            self._states.pop(path, None)
        gen = self._current
        if gen and ((path == gen.transport_path and TRANSPORT_IFACE in interfaces)
                    or (path == gen.device_path and DEVICE_IFACE in interfaces)):
            self._invalidate('object removed')

    def on_name_owner_changed(self, name, old, new):
        if name not in (BLUEZ, BLUEALSA):
            return
        try:
            unchanged = self._pins is not None and self._owners() == self._pins
        except Exception:
            unchanged = False
        if unchanged:
            return
        self._invalidate('bus owner changed')
        self.start()

    def _transition(self, path, hint):
        previous = self._states.get(path)
        self._states[path] = hint if hint == 'active' else (hint or 'idle')
        gen = self._current
        if hint != 'active':
            if gen and path == gen.transport_path:
                self._invalidate('transport left active')
            return
        # The hint never admits: only a fresh edge on a well-formed path is read back.
        if previous == 'active' or gen or not _TRANSPORT.fullmatch(path):
            return
        try:
            candidate = self._read(path)
        except Exception:
            return
        if self._admit_address and not self._admit_address(candidate[1]):
            return
        self._counter += 1
        self._current = StreamGeneration(*self._pins, candidate[0], path, candidate[1],
                                         candidate[2], self._counter)
        self._emit(self._current)

    def _check_current(self):
        gen = self._current
        try:
            ok = self._read(gen.transport_path) == (gen.device_path, gen.address, gen.codec)
        except Exception:
            ok = False
        if not ok:
            self._invalidate('stream identity changed')
        return ok

    def _invalidate(self, reason):
        gen, self._current = self._current, None
        if gen:
            self._report(gen, reason)

    def _owners(self):
        owners = (self._bus.name_owner(BLUEZ), self._bus.name_owner(BLUEALSA))
        if not all(isinstance(o, str) and _UNIQUE.fullmatch(o) for o in owners):
            raise TransportUnavailable('BlueZ and BlueALSA need unique bus owners')
        return owners

    def _read(self, path):
        """Two consistent reads of transport + Device1 -> (device_path, address, codec)."""
        match = _TRANSPORT.fullmatch(path)
        if not match:
            raise TransportUnavailable('unexpected transport path')
        if self._owners() != self._pins:
            # Missed NameOwnerChanged: close, then resync so stale state is never admitted.
            self._invalidate('bus owner changed')
            self.start()
            raise TransportUnavailable('bus owner changed')
        device_path, address = match.group(1), match.group(2).replace('_', ':')
        rounds = []
        for _ in range(2):
            transport = self._bus.get_all(self._pins[0], path, TRANSPORT_IFACE)
            device = self._bus.get_all(self._pins[0], device_path, DEVICE_IFACE)
            codec = transport.get('Codec')
            if (transport.get('State') != 'active' or transport.get('Device') != device_path
                    or transport.get('UUID') != LOCAL_SINK_UUID
                    or type(codec) is not int or codec not in self._codecs
                    or device.get('Address') != address or device.get('Adapter') != ADAPTER_PATH
                    or any(device.get(key) is not True
                           for key in ('Paired', 'Trusted', 'Connected'))):
                raise TransportUnavailable('transport or device identity rejected')
            rounds.append((device_path, address, codec))
        if rounds[0] != rounds[1] or self._owners() != self._pins:
            raise TransportUnavailable('contradictory or racing reads')
        return rounds[0]


class SystemBusAdapter:
    """Bounded system-bus reads and signal wiring on python3-dbus; optional import.

    Construction fails closed with TransportUnavailable when dbus is missing. Signals
    are delivered to the observer with the sender's unique name, which the observer
    checks against its pin. Run a GLib main loop to receive them.
    """

    def __init__(self, bus=None, modules=None):
        if modules is None:
            try:
                import dbus
                import dbus.mainloop.glib
                dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
            except ImportError as error:
                raise TransportUnavailable('python3-dbus is unavailable') from error
            modules = dbus
        self._dbus = modules
        self._bus = bus if bus is not None else modules.SystemBus()

    def _call(self, dest, path, interface, method, signature='', args=()):
        try:
            return self._plain(self._bus.call_blocking(
                dest, path, interface, method, signature, args, timeout=TIMEOUT))
        except Exception as error:
            raise TransportUnavailable(f'{interface}.{method} failed') from error

    def _plain(self, value):
        if isinstance(value, getattr(self._dbus, 'Boolean', ())):
            return bool(value)
        if isinstance(value, str):
            return str(value)
        if isinstance(value, int):
            return int(value)
        if isinstance(value, dict):
            return {self._plain(k): self._plain(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._plain(v) for v in value]
        return value

    def name_owner(self, name):
        try:
            return self._call('org.freedesktop.DBus', '/org/freedesktop/DBus',
                              'org.freedesktop.DBus', 'GetNameOwner', 's', (name,))
        except TransportUnavailable as error:
            cause = getattr(error.__cause__, 'get_dbus_name', lambda: '')()
            if cause == 'org.freedesktop.DBus.Error.NameHasNoOwner':
                return None
            raise

    def managed_objects(self, owner):
        return self._call(owner, '/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects')

    def get_all(self, owner, path, interface):
        return self._call(owner, path, 'org.freedesktop.DBus.Properties', 'GetAll',
                          's', (interface,))

    def connect(self, observer):
        """Subscribe before observer.start() so no edge can fall between snapshot and match."""
        add = self._bus.add_signal_receiver
        def changed(interface, props, _invalid, sender=None, path=None):
            observer.on_properties_changed(sender, str(path), str(interface), self._plain(props))
        def added(path, interfaces, sender=None):
            observer.on_interfaces_added(sender, str(path), self._plain(interfaces))
        def removed(path, interfaces, sender=None):
            observer.on_interfaces_removed(sender, str(path), self._plain(interfaces))
        def owner(name, old, new):
            observer.on_name_owner_changed(str(name), str(old), str(new))
        add(changed, 'PropertiesChanged', 'org.freedesktop.DBus.Properties',
            sender_keyword='sender', path_keyword='path')
        add(added, 'InterfacesAdded', 'org.freedesktop.DBus.ObjectManager', sender_keyword='sender')
        add(removed, 'InterfacesRemoved', 'org.freedesktop.DBus.ObjectManager', sender_keyword='sender')
        add(owner, 'NameOwnerChanged', 'org.freedesktop.DBus', 'org.freedesktop.DBus')
