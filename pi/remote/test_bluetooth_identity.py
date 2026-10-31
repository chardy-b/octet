import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bluetooth_pairing
from bluetooth_identity import devices, observe_identity, paired_addresses, verify_identity

MAC = '12:34:56:78:9A:BC'


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.admin = patch('bluetooth_identity.os.geteuid', return_value=0)
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.values = {'Address': f's "{MAC}"\n'.encode(),
                       'Adapter': b'o "/org/bluez/hci0"\n', 'Paired': b'b true\n',
                       'Trusted': b'b true\n', 'Connected': b'b true\n'}
        self.calls = []

    def run_bus(self, argv, **kwargs):
        self.calls.append(argv)
        self.assertEqual(argv[:7], ['/usr/bin/busctl', '--system', '--no-pager',
            '--timeout=2s', 'get-property', 'org.bluez', '/org/bluez/hci0/dev_12_34_56_78_9A_BC'])
        self.assertEqual(argv[7], 'org.bluez.Device1')
        self.assertEqual(kwargs, dict(capture_output=True, timeout=3, shell=False))
        return SimpleNamespace(returncode=0, stderr=b'', stdout=self.values[argv[-1]])

    def test_exact_allowlisted_paired_trusted_connected_identity(self):
        with patch('bluetooth_identity.subprocess.run', side_effect=self.run_bus):
            self.assertEqual(verify_identity(MAC, [MAC]), MAC)
        self.assertEqual(len(self.calls), 10)

    def test_each_identity_property_fails_closed(self):
        for key in self.values:
            with self.subTest(key=key):
                good = self.values[key]
                self.values[key] = b'b false\n'
                with patch('bluetooth_identity.subprocess.run', side_effect=self.run_bus):
                    with self.assertRaisesRegex(RuntimeError, key):
                        verify_identity(MAC, [MAC])
                self.values[key] = good

    def test_bus_failure_stderr_timeout_and_nonexact_output(self):
        for failure in [SimpleNamespace(returncode=1, stderr=b'', stdout=self.values['Address']),
                        SimpleNamespace(returncode=0, stderr=b'warning', stdout=self.values['Address']),
                        SimpleNamespace(returncode=0, stderr=b'', stdout=self.values['Address'].rstrip()),
                        subprocess.TimeoutExpired('busctl', 3), OSError('missing busctl')]:
            with self.subTest(failure=failure):
                options = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch('bluetooth_identity.subprocess.run', **options):
                    with self.assertRaises((RuntimeError, OSError, subprocess.TimeoutExpired)):
                        verify_identity(MAC, [MAC])

    def test_service_observation_is_unprivileged_but_identical(self):
        with patch('bluetooth_identity.os.geteuid', return_value=1000), \
             patch('bluetooth_identity.subprocess.run', side_effect=self.run_bus):
            self.assertEqual(observe_identity(MAC.lower(), [MAC]), MAC)
            with self.assertRaises(ValueError):
                observe_identity(MAC, ['11:22:33:44:55:66'])
        self.assertEqual(len(self.calls), 10)

    def test_disconnect_between_observations(self):
        def disconnect(argv, **kwargs):
            if len(self.calls) == 5:
                self.values['Connected'] = b'b false\n'
            return self.run_bus(argv, **kwargs)
        with patch('bluetooth_identity.subprocess.run', side_effect=disconnect):
            with self.assertRaisesRegex(RuntimeError, 'Connected'):
                verify_identity(MAC, [MAC])

    def test_reject_before_bus_for_nonadmin_unlisted_or_wildcard(self):
        with patch('bluetooth_identity.subprocess.run') as bus:
            for address, allowed in [(MAC, []), (MAC, ['11:22:33:44:55:66']),
                                     ('00:00:00:00:00:00', ['00:00:00:00:00:00']),
                                     ('phone name', [MAC])]:
                with self.assertRaises(ValueError):
                    verify_identity(address, allowed)
            with patch('bluetooth_identity.os.geteuid', return_value=1000):
                with self.assertRaises(RuntimeError):
                    verify_identity(MAC, [MAC])
            bus.assert_not_called()


class PairedDeviceTests(unittest.TestCase):
    """Every device paired with hci0 may play; BlueZ is the only source of that list."""

    def objects(self, *phones):
        data = {'/org/bluez/hci0': {'org.bluez.Adapter1': {}}}
        for address, paired, trusted, adapter in phones:
            data['/org/bluez/hci0/dev_' + address.replace(':', '_')] = {'org.bluez.Device1': {
                'Address': {'type': 's', 'data': address}, 'Alias': {'type': 's', 'data': 'Phone ' + address[:2]},
                'Paired': {'type': 'b', 'data': paired}, 'Trusted': {'type': 'b', 'data': trusted},
                'Connected': {'type': 'b', 'data': False}, 'Adapter': {'type': 'o', 'data': adapter}}}
        body = json.dumps({'type': 'a{oa{sa{sv}}}', 'data': [data]}).encode()
        return SimpleNamespace(returncode=0, stdout=body, stderr=b'')

    def test_paired_devices_on_hci0_are_the_phones_allowed_to_play(self):
        reply = self.objects((MAC, True, True, '/org/bluez/hci0'), ('11:22:33:44:55:66', False, False, '/org/bluez/hci0'),
                             ('AA:BB:CC:DD:EE:FF', True, False, '/org/bluez/hci1'))
        with patch('bluetooth_identity.subprocess.run', return_value=reply):
            self.assertEqual(paired_addresses(), (MAC,))
            self.assertEqual([item['address'] for item in devices()], [MAC, '11:22:33:44:55:66'])

    def test_nothing_paired_or_bluez_down_refuses(self):
        with patch('bluetooth_identity.subprocess.run', return_value=self.objects()):
            with self.assertRaisesRegex(RuntimeError, 'No paired'):
                paired_addresses()
        with patch('bluetooth_identity.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout=b'', stderr=b'x')):
            with self.assertRaisesRegex(RuntimeError, 'BlueZ'):
                paired_addresses()

    def test_newly_paired_phone_is_trusted_without_an_agent(self):
        calls = []
        def bus(argv, **kwargs):
            calls.append(argv[4:])
            if 'GetManagedObjects' in argv:
                return self.objects((MAC, True, False, '/org/bluez/hci0'), ('11:22:33:44:55:66', True, True, '/org/bluez/hci0'))
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        with patch('bluetooth_identity.subprocess.run', side_effect=bus):
            self.assertEqual(bluetooth_pairing.trust_paired(), [MAC])
        self.assertIn(['set-property', 'org.bluez', '/org/bluez/hci0/dev_12_34_56_78_9A_BC',
                       'org.bluez.Device1', 'Trusted', 'b', 'true'], calls)
        self.assertEqual(sum(1 for call in calls if call[0] == 'set-property'), 1)

    def test_discoverable_sets_no_timeout_then_pairable_and_discoverable(self):
        adapter = {}
        def bus(argv, **kwargs):
            if argv[4] == 'set-property':
                adapter[argv[8]] = argv[10]
                return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            value = adapter.get(argv[8], 'false')
            return SimpleNamespace(returncode=0, stdout=f'b {value}\n'.encode(), stderr=b'')
        with patch('bluetooth_identity.subprocess.run', side_effect=bus):
            self.assertTrue(bluetooth_pairing.set_discoverable(True))
            self.assertEqual(adapter, dict(PairableTimeout='0', DiscoverableTimeout='0', Pairable='true', Discoverable='true'))
            self.assertTrue(bluetooth_pairing.discoverable())
            self.assertFalse(bluetooth_pairing.set_discoverable(False))
            self.assertEqual((adapter['Pairable'], adapter['Discoverable']), ('false', 'false'))
            self.assertFalse(bluetooth_pairing.discoverable())


if __name__ == '__main__':
    unittest.main()
