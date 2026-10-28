import sys
import unittest
from unittest.mock import patch

from bluetooth_transport import (LOCAL_SINK_UUID, StreamGeneration, SystemBusAdapter,
                                 TransportObserver, TransportUnavailable)

MAC = '12:34:56:78:9A:BC'
MAC2 = 'AA:BB:CC:DD:EE:FF'
BLUEZ, BLUEALSA = ':1.10', ':1.20'
ADAPTER = '/org/bluez/hci0'


def dev(mac):
    return ADAPTER + '/dev_' + mac.replace(':', '_')


def transport_props(mac, state='idle', codec=0, uuid=LOCAL_SINK_UUID):
    return {'org.bluez.MediaTransport1': {'Device': dev(mac), 'UUID': uuid,
                                          'Codec': codec, 'State': state}}


def device_props(mac, paired=True, trusted=True, connected=True, adapter=ADAPTER):
    return {'org.bluez.Device1': {'Address': mac, 'Adapter': adapter, 'Paired': paired,
                                  'Trusted': trusted, 'Connected': connected}}


class FakeBus:
    def __init__(self):
        self.owners = {'org.bluez': BLUEZ, 'org.bluealsa': BLUEALSA}
        self.objects = {}
        self.reads = []
        self.hook = None

    def add(self, path, props):
        self.objects.setdefault(path, {}).update(props)

    def set(self, path, iface, **values):
        self.objects[path][iface].update(values)

    def name_owner(self, name):
        return self.owners.get(name)

    def managed_objects(self, owner):
        if owner != self.owners['org.bluez']:
            raise TransportUnavailable('wrong owner')
        return {p: {i: dict(v) for i, v in ifs.items()} for p, ifs in self.objects.items()}

    def get_all(self, owner, path, interface):
        self.reads.append((owner, path, interface))
        if self.hook:
            self.hook(len(self.reads))
        if owner != self.owners['org.bluez'] or path not in self.objects \
                or interface not in self.objects[path]:
            raise TransportUnavailable('gone')
        return dict(self.objects[path][interface])


TP = dev(MAC) + '/sep1/fd2'
TP2 = dev(MAC2) + '/sep1/fd0'


class Base(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.gens, self.invalid = [], []
        for mac in (MAC, MAC2):
            self.bus.add(dev(mac), device_props(mac))
        self.bus.add(TP, transport_props(MAC))
        self.bus.add(TP2, transport_props(MAC2))
        self.obs = TransportObserver(self.bus, self.gens.append,
                                     lambda g, why: self.invalid.append((g, why)))

    def go_active(self, path=TP, sender=BLUEZ):
        iface = 'org.bluez.MediaTransport1'
        self.bus.set(path, iface, State='active')
        self.obs.on_properties_changed(sender, path, iface, {'State': 'active'})

    def go_idle(self, path=TP, sender=BLUEZ):
        iface = 'org.bluez.MediaTransport1'
        self.bus.set(path, iface, State='idle')
        self.obs.on_properties_changed(sender, path, iface, {'State': 'idle'})


class AdmissionTests(Base):
    def test_fresh_active_edge_emits_pinned_generation(self):
        self.obs.start()
        self.go_active()
        self.assertEqual(self.gens, [StreamGeneration(BLUEZ, BLUEALSA, dev(MAC), TP, MAC, 0, 1)])
        self.assertTrue(self.obs.valid(self.gens[0]))

    def test_paired_connected_idle_does_nothing(self):
        self.obs.start()
        self.go_idle()
        self.assertEqual(self.gens, [])

    def test_boot_active_snapshot_does_nothing_even_when_repeated(self):
        self.bus.set(TP, 'org.bluez.MediaTransport1', State='active')
        self.obs.start()
        self.go_active()  # duplicate callback for already-active state
        self.assertEqual(self.gens, [])

    def test_boot_active_then_idle_then_active_admits(self):
        self.bus.set(TP, 'org.bluez.MediaTransport1', State='active')
        self.obs.start()
        self.go_idle()
        self.go_active()
        self.assertEqual(len(self.gens), 1)

    def test_pending_to_active_is_fresh(self):
        self.obs.start()
        self.bus.set(TP, 'org.bluez.MediaTransport1', State='pending')
        self.obs.on_properties_changed(BLUEZ, TP, 'org.bluez.MediaTransport1', {'State': 'pending'})
        self.go_active()
        self.assertEqual(len(self.gens), 1)

    def test_new_transport_object_already_active_is_fresh(self):
        self.obs.start()
        path = dev(MAC) + '/sep1/fd7'
        self.bus.add(path, transport_props(MAC, 'active'))
        self.obs.on_interfaces_added(BLUEZ, path, transport_props(MAC, 'active'))
        self.assertEqual(self.gens[0].transport_path, path)

    def test_no_start_no_admission(self):
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_duplicate_active_callback_is_ignored(self):
        self.obs.start()
        self.go_active()
        self.go_active()
        self.assertEqual(len(self.gens), 1)
        self.assertEqual(self.invalid, [])


class RejectionTests(Base):
    def test_stale_owner_event_rejected(self):
        self.obs.start()
        self.go_active(sender=':1.99')
        self.assertEqual(self.gens, [])

    def test_owner_restart_between_event_and_read_rejected(self):
        self.obs.start()
        self.bus.owners['org.bluez'] = ':1.11'
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_bluealsa_owner_changed_before_read_rejected(self):
        self.obs.start()
        self.bus.owners['org.bluealsa'] = ':1.21'
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_bluealsa_absent_at_start_fails_closed(self):
        del self.bus.owners['org.bluealsa']
        self.obs.start()
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_non_unique_owner_rejected(self):
        self.bus.owners['org.bluez'] = 'org.bluez'
        self.obs.start()
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_codec_mismatch(self):
        self.obs.start()
        self.bus.set(TP, 'org.bluez.MediaTransport1', Codec=0xff)
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_other_uuids_rejected_including_source_110a(self):
        for uuid in ('0000110a-0000-1000-8000-00805f9b34fb', '0000110d-0000-1000-8000-00805f9b34fb',
                     '0000110B-0000-1000-8000-00805f9b34fb', '', None):
            with self.subTest(uuid=uuid):
                self.setUp()
                self.obs.start()
                self.bus.set(TP, 'org.bluez.MediaTransport1', UUID=uuid)
                self.go_active()
                self.assertEqual(self.gens, [])

    def test_missing_uuid_rejected(self):
        self.obs.start()
        del self.bus.objects[TP]['org.bluez.MediaTransport1']['UUID']
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_live_octet_shape_admits(self):
        # busctl on Octet (BlueZ 5.82/BlueALSA 4.3.1): .../dev_X/fd6, Codec y 0, UUID 110b.
        mac, device = 'B0:9C:B2:D3:EE:44', ADAPTER + '/dev_B0_9C_B2_D3_EE_44'
        path = device + '/fd6'
        self.bus.add(device, device_props(mac))
        self.bus.add(path, {'org.bluez.MediaTransport1': {
            'Device': device, 'State': 'idle', 'Codec': 0,
            'UUID': '0000110b-0000-1000-8000-00805f9b34fb'}})
        self.obs.start()
        self.go_active(path)
        self.assertEqual(self.gens, [StreamGeneration(BLUEZ, BLUEALSA, device, path, mac, 0, 1)])

    def test_wrong_adapter_path_rejected(self):
        self.obs.start()
        path = '/org/bluez/hci1/dev_12_34_56_78_9A_BC/sep1/fd2'
        self.bus.add(path, transport_props(MAC))
        self.bus.objects[path]['org.bluez.MediaTransport1']['Device'] = '/org/bluez/hci1/dev_12_34_56_78_9A_BC'
        self.bus.add('/org/bluez/hci1/dev_12_34_56_78_9A_BC', device_props(MAC, adapter='/org/bluez/hci1'))
        self.obs.on_interfaces_added(BLUEZ, path, transport_props(MAC, 'active'))
        self.bus.set(path, 'org.bluez.MediaTransport1', State='active')
        self.obs.on_properties_changed(BLUEZ, path, 'org.bluez.MediaTransport1', {'State': 'active'})
        self.assertEqual(self.gens, [])

    def test_transport_device_mismatch_rejected(self):
        self.obs.start()
        self.bus.set(TP, 'org.bluez.MediaTransport1', Device=dev(MAC2))
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_unexpected_path_shape_rejected(self):
        self.obs.start()
        path = dev(MAC) + '/../fd2'
        self.bus.add(path, transport_props(MAC, 'active'))
        self.obs.on_interfaces_added(BLUEZ, path, transport_props(MAC, 'active'))
        self.assertEqual(self.gens, [])

    def test_identity_must_be_paired_trusted_connected(self):
        for field in ('paired', 'trusted', 'connected'):
            with self.subTest(field=field):
                self.setUp()
                self.bus.add(dev(MAC), device_props(MAC, **{field: False}))
                self.obs.start()
                self.go_active()
                self.assertEqual(self.gens, [])

    def test_identity_truthy_non_bool_rejected(self):
        self.bus.add(dev(MAC), device_props(MAC, trusted=1))
        self.obs.start()
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_address_must_match_path(self):
        self.bus.add(dev(MAC), device_props(MAC2))
        self.obs.start()
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_contradictory_second_read_rejected(self):
        self.obs.start()
        def flip(count):
            if count == 3:
                self.bus.set(dev(MAC), 'org.bluez.Device1', Connected=False)
        self.bus.hook = flip
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_two_reads_each_of_transport_and_device(self):
        self.obs.start()
        self.bus.reads.clear()
        self.go_active()
        self.assertEqual(len(self.bus.reads), 4)
        self.assertEqual({r[0] for r in self.bus.reads}, {BLUEZ})

    def test_admit_predicate_can_refuse(self):
        obs = TransportObserver(self.bus, self.gens.append, lambda *a: None,
                                admit=lambda address: address == MAC2)
        obs.start()
        self.go_active()
        self.assertEqual(self.gens, [])

    def test_read_failure_latches_transport_until_it_leaves_active(self):
        self.obs.start()
        calls = []
        def boom(count):
            calls.append(count)
            raise TransportUnavailable('timeout')
        self.bus.hook = boom
        self.go_active()
        self.bus.hook = None
        self.go_active()  # same failed stream: no retry
        self.assertEqual(self.gens, [])
        self.go_idle()
        self.go_active()
        self.assertEqual(len(self.gens), 1)


class SlotTests(Base):
    def test_first_valid_active_wins_and_competitor_ignored(self):
        self.obs.start()
        self.go_active(TP)
        self.go_active(TP2)
        self.assertEqual([g.address for g in self.gens], [MAC])

    def test_competitor_not_admitted_on_release_without_fresh_edge(self):
        self.obs.start()
        self.go_active(TP)
        self.go_active(TP2)
        self.obs.release(self.gens[0])
        self.assertEqual(len(self.gens), 1)
        self.go_idle(TP2)
        self.go_active(TP2)
        self.assertEqual([g.address for g in self.gens], [MAC, MAC2])
        self.assertEqual(self.gens[1].counter, 2)

    def test_release_then_fresh_edge_readmits_same_device(self):
        self.obs.start()
        self.go_active()
        self.obs.release(self.gens[0])
        self.go_idle()
        self.go_active()
        self.assertEqual([g.counter for g in self.gens], [1, 2])

    def test_released_still_active_stream_not_readmitted_without_edge(self):
        self.obs.start()
        self.go_active()
        self.obs.release(self.gens[0])
        self.go_active()
        self.assertEqual(len(self.gens), 1)

    def test_stale_release_does_not_free_slot(self):
        self.obs.start()
        self.go_active(TP)
        stale = self.gens[0]._replace(counter=99)
        self.obs.release(stale)
        self.go_active(TP2)
        self.assertEqual(len(self.gens), 1)

    def test_fail_latches_stream_generation(self):
        self.obs.start()
        self.go_active()
        self.obs.fail(self.gens[0], 'route failed')
        self.assertEqual(self.invalid[-1][1], 'route failed')
        self.go_active()
        self.assertEqual(len(self.gens), 1)
        self.go_idle()
        self.go_active()
        self.assertEqual(len(self.gens), 2)

    def test_generation_counter_is_monotonic_across_owner_restart(self):
        self.obs.start()
        self.go_active()
        self.bus.owners['org.bluez'] = ':1.11'
        self.obs.on_name_owner_changed('org.bluez', BLUEZ, ':1.11')
        self.bus.set(TP, 'org.bluez.MediaTransport1', State='idle')
        self.obs.on_properties_changed(':1.11', TP, 'org.bluez.MediaTransport1', {'State': 'idle'})
        self.go_active(sender=':1.11')
        self.assertEqual([g.counter for g in self.gens], [1, 2])
        self.assertEqual(self.gens[1].bluez_owner, ':1.11')


class InvalidationTests(Base):
    def admitted(self):
        self.obs.start()
        self.go_active()
        return self.gens[0]

    def test_idle_invalidates(self):
        gen = self.admitted()
        self.go_idle()
        self.assertEqual(self.invalid, [(gen, 'transport left active')])
        self.assertFalse(self.obs.valid(gen))

    def test_transport_removed_invalidates(self):
        gen = self.admitted()
        self.obs.on_interfaces_removed(BLUEZ, TP, ['org.bluez.MediaTransport1'])
        self.assertEqual(self.invalid[0][0], gen)

    def test_device_removed_invalidates(self):
        gen = self.admitted()
        self.obs.on_interfaces_removed(BLUEZ, dev(MAC), ['org.bluez.Device1'])
        self.assertEqual(self.invalid[0][0], gen)

    def test_disconnect_invalidates(self):
        gen = self.admitted()
        self.bus.set(dev(MAC), 'org.bluez.Device1', Connected=False)
        self.obs.on_properties_changed(BLUEZ, dev(MAC), 'org.bluez.Device1', {'Connected': False})
        self.assertEqual(self.invalid[0][0], gen)

    def test_untrust_invalidates(self):
        gen = self.admitted()
        self.bus.set(dev(MAC), 'org.bluez.Device1', Trusted=False)
        self.obs.on_properties_changed(BLUEZ, dev(MAC), 'org.bluez.Device1', {'Trusted': False})
        self.assertEqual(self.invalid[0][0], gen)

    def test_other_device_events_do_not_invalidate(self):
        self.admitted()
        self.bus.set(dev(MAC2), 'org.bluez.Device1', Connected=False)
        self.obs.on_properties_changed(BLUEZ, dev(MAC2), 'org.bluez.Device1', {'Connected': False})
        self.assertEqual(self.invalid, [])

    def test_owner_change_invalidates_and_blocks_until_resync(self):
        gen = self.admitted()
        self.bus.owners['org.bluealsa'] = ':1.21'
        self.obs.on_name_owner_changed('org.bluealsa', BLUEALSA, ':1.21')
        self.assertEqual(self.invalid[0][0], gen)
        self.assertFalse(self.obs.valid(gen))

    def test_owner_vanishing_fails_closed_and_recovery_ignores_active_snapshot(self):
        gen = self.admitted()
        del self.bus.owners['org.bluez']
        self.obs.on_name_owner_changed('org.bluez', BLUEZ, '')
        self.assertEqual(self.invalid[0][0], gen)
        self.bus.owners['org.bluez'] = ':1.11'
        self.obs.on_name_owner_changed('org.bluez', '', ':1.11')
        self.assertEqual(len(self.gens), 1)  # stream still active at resync: stale
        self.assertFalse(self.obs.valid(gen))

    def test_unchanged_owner_notification_is_harmless(self):
        gen = self.admitted()
        self.obs.on_name_owner_changed('org.bluez', BLUEZ, BLUEZ)
        self.assertEqual(self.invalid, [])
        self.assertTrue(self.obs.valid(gen))

    def test_valid_detects_silent_owner_change(self):
        gen = self.admitted()
        self.bus.owners['org.bluez'] = ':1.11'
        self.assertFalse(self.obs.valid(gen))

    def test_valid_detects_contradictory_read(self):
        gen = self.admitted()
        self.bus.set(dev(MAC), 'org.bluez.Device1', Paired=False)
        self.assertFalse(self.obs.valid(gen))

    def test_valid_rejects_forged_generation(self):
        gen = self.admitted()
        self.assertFalse(self.obs.valid(gen._replace(address=MAC2)))

    def test_invalidation_is_reported_once(self):
        self.admitted()
        self.go_idle()
        self.obs.on_interfaces_removed(BLUEZ, TP, ['org.bluez.MediaTransport1'])
        self.assertEqual(len(self.invalid), 1)

    def test_stale_owner_events_do_not_invalidate(self):
        self.admitted()
        self.bus.set(TP, 'org.bluez.MediaTransport1', State='idle')
        self.obs.on_properties_changed(':1.99', TP, 'org.bluez.MediaTransport1', {'State': 'idle'})
        self.assertEqual(self.invalid, [])


class AdapterTests(unittest.TestCase):
    def test_import_is_optional_and_construction_fails_closed(self):
        with patch.dict(sys.modules, {'dbus': None, 'dbus.mainloop.glib': None}):
            with self.assertRaises(TransportUnavailable):
                SystemBusAdapter()

    def test_adapter_normalizes_and_pins_calls(self):
        class Boolean(int):
            pass
        class Str(str):
            pass
        class Bus:
            def __init__(self):
                self.calls = []
            def call_blocking(self, dest, path, iface, method, sig, args, timeout=None):
                self.calls.append((dest, path, iface, method, args, timeout))
                if method == 'GetNameOwner':
                    return Str(':1.10')
                return {Str('Connected'): Boolean(1), Str('Codec'): 2, Str('Address'): Str(MAC)}
        Dbus = type('Dbus', (), {'Boolean': Boolean, 'String': Str, 'ObjectPath': Str})
        bus = Bus()
        adapter = SystemBusAdapter(bus=bus, modules=Dbus)
        self.assertEqual(adapter.name_owner('org.bluez'), ':1.10')
        props = adapter.get_all(':1.10', TP, 'org.bluez.MediaTransport1')
        self.assertIs(props['Connected'], True)
        self.assertIs(type(props['Address']), str)
        self.assertEqual(bus.calls[1][0], ':1.10')
        self.assertTrue(all(call[5] and call[5] <= 3 for call in bus.calls))


if __name__ == '__main__':
    unittest.main()
