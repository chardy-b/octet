"""Local administrator CLI. All bus and socket I/O is simulated."""
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bluetooth_admin
import bluetooth_identity
from install_bluetooth import DEFAULT

MAC = '12:34:56:78:9A:BC'
ADAPTER = ['/usr/bin/busctl', '--system', '--no-pager', '--timeout=2s']


class BusTestCase(unittest.TestCase):
    """Simulated BlueZ adapter/device properties; no tests of its own."""
    def setUp(self):
        self.properties = {'Discoverable': b'b false\n', 'Pairable': b'b false\n',
                           'DiscoverableTimeout': b'u 180\n', 'PairableTimeout': b'u 0\n',
                           'Address': f's "{MAC}"\n'.encode(), 'Adapter': b'o "/org/bluez/hci0"\n',
                           'Paired': b'b true\n', 'Trusted': b'b false\n',
                           'Powered': b'b true\n'}
        self.calls = []
        self.broken = set()
        for item in (patch('bluetooth_admin.os.geteuid', return_value=0),
                     patch('bluetooth_admin.local_policy', return_value=(MAC,)),
                     patch('bluetooth_admin.pairing_policy', return_value=(True, (MAC,))),
                     patch('bluetooth_admin.subprocess.run', side_effect=self.bus)):
            item.start()
            self.addCleanup(item.stop)

    def bus(self, argv, **kwargs):
        self.assertEqual(kwargs, dict(capture_output=True, timeout=3, shell=False))
        self.assertEqual(argv[:4], ADAPTER)
        self.calls.append(argv[4:])
        verb, service, path, interface, name = argv[4:9]
        self.assertEqual(service, 'org.bluez')
        expected = ('org.bluez.Adapter1', '/org/bluez/hci0') if name in (
            'Discoverable', 'Pairable', 'DiscoverableTimeout', 'PairableTimeout', 'Powered') else (
            'org.bluez.Device1', '/org/bluez/hci0/dev_12_34_56_78_9A_BC')
        self.assertEqual((interface, path), expected)
        if name in self.broken:
            return SimpleNamespace(returncode=1, stdout=b'', stderr=b'Failed')
        if verb == 'set-property':
            signature, value = argv[9:]
            self.properties[name] = f'{signature} {value}\n'.encode()
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        self.assertEqual(verb, 'get-property')
        return SimpleNamespace(returncode=0, stdout=self.properties[name], stderr=b'')

    def sets(self):
        return [c[4:] for c in self.calls if c[0] == 'set-property']


class AdminTests(BusTestCase):
    def test_discoverable_window_is_bounded_by_bluez_timeouts_first(self):
        bluetooth_admin.discoverable(90)
        self.assertEqual(self.sets(), [['PairableTimeout', 'u', '90'], ['DiscoverableTimeout', 'u', '90'],
                                       ['Pairable', 'b', 'true'], ['Discoverable', 'b', 'true']])
        self.assertEqual(self.properties['Discoverable'], b'b true\n')

    def test_unpowered_adapter_refuses_window_without_any_write(self):
        for value in (b'b false\n', b''):
            with self.subTest(value=value):
                self.properties['Powered'] = value
                with self.assertRaisesRegex(RuntimeError, 'not powered'):
                    bluetooth_admin.discoverable(60)
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.properties['Discoverable'], b'b false\n')

    def test_window_length_and_policy_are_required_before_any_bus_write(self):
        for seconds in (0, -1, 181, 3600, True, 1.5, '60'):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                bluetooth_admin.discoverable(seconds)
        with patch('bluetooth_admin.pairing_policy', side_effect=RuntimeError('0640')):
            with self.assertRaisesRegex(RuntimeError, '0640'):
                bluetooth_admin.discoverable(60)
        with patch('bluetooth_admin.os.geteuid', return_value=1000):
            for action in (lambda: bluetooth_admin.discoverable(60), bluetooth_admin.close,
                           lambda: bluetooth_admin.trust(MAC), lambda: bluetooth_admin.untrust(MAC)):
                with self.assertRaisesRegex(RuntimeError, 'administrator'):
                    action()
        self.assertEqual(self.calls, [])

    def test_window_opens_with_disabled_empty_policy_but_not_the_start_gate(self):
        # The pairing window must not depend on the enabled allowlist (MAC unknown).
        with patch('bluetooth_admin.pairing_policy', return_value=(False, ())), \
             patch('bluetooth_admin.local_policy',
                   side_effect=AssertionError('start gate consulted')):
            bluetooth_admin.discoverable(60)
        self.assertEqual(self.properties['Discoverable'], b'b true\n')
        self.assertEqual(self.properties['Pairable'], b'b true\n')
        # bluetoothd's own timeouts bound it; the tool registers no agent.
        self.assertEqual(self.properties['PairableTimeout'], b'u 60\n')
        self.assertEqual(self.properties['DiscoverableTimeout'], b'u 60\n')
        self.assertFalse([c for c in self.calls if 'AgentManager1' in c or 'RegisterAgent' in c])

    def test_failed_or_unverified_window_is_closed(self):
        self.broken.add('Discoverable')
        with self.assertRaises(RuntimeError):
            bluetooth_admin.discoverable(60)
        self.assertEqual(self.properties['Pairable'], b'b false\n')
        self.assertIn(['Pairable', 'b', 'false'], self.sets())

    def test_close_attempts_both_and_verifies(self):
        self.properties.update(Discoverable=b'b true\n', Pairable=b'b true\n')
        bluetooth_admin.close()
        self.assertEqual(self.properties['Discoverable'], b'b false\n')
        self.assertEqual(self.properties['Pairable'], b'b false\n')
        self.properties['Pairable'] = b'b true\n'
        self.broken.add('Discoverable')
        with self.assertRaises(RuntimeError):
            bluetooth_admin.close()
        self.assertEqual(self.properties['Pairable'], b'b false\n')

    def test_trust_only_allowlisted_paired_exact_device_then_close_window(self):
        self.properties.update(Discoverable=b'b true\n', Pairable=b'b true\n')
        self.assertEqual(bluetooth_admin.trust(MAC.lower()), MAC)
        self.assertEqual(self.properties['Trusted'], b'b true\n')
        self.assertEqual(self.properties['Discoverable'], b'b false\n')
        self.assertEqual(self.sets()[0], ['Trusted', 'b', 'true'])

    def test_trust_refuses_unlisted_unpaired_or_mismatched_device(self):
        with self.assertRaises(ValueError):
            bluetooth_admin.trust('11:22:33:44:55:66')
        with self.assertRaises(ValueError):
            bluetooth_admin.trust('phone name')
        self.assertEqual(self.calls, [])
        for name, value in (('Paired', b'b false\n'), ('Address', b's "11:22:33:44:55:66"\n'),
                            ('Adapter', b'o "/org/bluez/hci1"\n')):
            with self.subTest(name=name):
                saved = self.properties[name]
                self.properties[name] = value
                with self.assertRaisesRegex(RuntimeError, name):
                    bluetooth_admin.trust(MAC)
                self.properties[name] = saved
        self.assertNotIn(['Trusted', 'b', 'true'], self.sets())

    def test_untrust_revokes_any_explicit_device(self):
        self.properties['Trusted'] = b'b true\n'
        with patch('bluetooth_admin.local_policy', side_effect=RuntimeError('disabled')):
            bluetooth_admin.untrust(MAC)
        self.assertEqual(self.properties['Trusted'], b'b false\n')
        with self.assertRaises(ValueError):
            bluetooth_admin.untrust('FF:FF:FF:FF:FF:FF')


class FreshPhoneFlowTests(BusTestCase):
    """Real policy files and readers; only the bus and root-owned /etc are simulated."""
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name).resolve() / 'bluetooth.json'
        self.path.write_bytes(DEFAULT)  # exactly what install_bluetooth.py writes
        self.path.chmod(0o640)
        gid = os.stat(self.path).st_gid
        self.properties.update(Paired=b'b false\n', Trusted=b'b false\n')
        real_lstat, real_fstat = Path.lstat, os.fstat
        def metadata(info, **changes):
            result = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
            result.update(changes)
            return SimpleNamespace(**result)
        def lstat(path):
            info = real_lstat(path)
            if path in self.path.parents:
                return metadata(info, st_uid=0, st_mode=stat.S_IFDIR | 0o755)
            return metadata(info, st_uid=0)
        for item in (patch.object(Path, 'lstat', lstat),
                     patch('bluetooth_identity.os.fstat',
                           side_effect=lambda fd: metadata(real_fstat(fd), st_uid=0)),
                     patch('bluetooth_identity.grp.getgrnam',
                           return_value=SimpleNamespace(gr_gid=gid)),
                     patch('bluetooth_admin.pairing_policy',
                           lambda: bluetooth_identity.pairing_policy(self.path)),
                     patch('bluetooth_admin.local_policy',
                           lambda: bluetooth_identity.local_policy(self.path))):
            item.start()
            self.addCleanup(item.stop)

    def service_can_start(self):
        # octet-remote's own gate (unprivileged reader, same file).
        with patch('bluetooth_identity.os.geteuid', return_value=1000):
            return bluetooth_identity.service_policy(self.path)

    def test_fresh_phone_without_known_mac_pairs_then_enables_then_trusts(self):
        # 1. Fresh install: disabled, empty allowlist. Nothing can be trusted yet.
        with self.assertRaises((RuntimeError, ValueError)):
            bluetooth_admin.trust(MAC)
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            self.service_can_start()
        # 2. The bounded window opens anyway; it still grants no playback.
        bluetooth_admin.discoverable(120)
        self.assertEqual(self.properties['Discoverable'], b'b true\n')
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            self.service_can_start()
        # 3. Attended pairing (operator confirms the passkey in bluetoothctl).
        self.properties['Paired'] = b'b true\n'
        # 4. A paired phone is still not trusted while the policy is disabled.
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            bluetooth_admin.trust(MAC)
        self.assertEqual(self.properties['Trusted'], b'b false\n')
        bluetooth_admin.close()
        self.assertEqual(self.properties['Discoverable'], b'b false\n')
        # 5. Administrator edits the policy in place (inode, owner, group, mode kept).
        self.path.write_text(json.dumps(dict(version=1, enabled=True, addresses=[MAC])))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        # 6. Trust succeeds, and the service can read the very same authority.
        self.assertEqual(bluetooth_admin.trust(MAC), MAC)
        self.assertEqual(self.properties['Trusted'], b'b true\n')
        self.assertEqual(self.service_can_start(), (MAC,))

    def test_root_only_policy_refuses_window_and_trust_before_any_grant(self):
        self.path.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, 'chmod 0640'):
            bluetooth_admin.discoverable(120)
        self.assertEqual(self.calls, [])
        self.properties['Paired'] = b'b true\n'
        self.path.write_text(json.dumps(dict(version=1, enabled=True, addresses=[MAC])))
        with self.assertRaisesRegex(RuntimeError, 'chmod 0640'):
            bluetooth_admin.trust(MAC)
        self.assertEqual(self.properties['Trusted'], b'b false\n')
        self.assertEqual(self.sets(), [])


class ClientTests(unittest.TestCase):
    def test_request_round_trip_and_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 's')
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(path)
            listener.listen(2)
            received = []
            def serve():
                for reply in (b'OK {"active": true}\n', b'ERROR "AirPlay owns audio"\n'):
                    connection, _ = listener.accept()
                    with connection:
                        received.append(connection.makefile('rb').readline())
                        connection.sendall(reply)
            thread = threading.Thread(target=serve)
            thread.start()
            with patch('bluetooth_admin.os.geteuid', return_value=0):
                self.assertEqual(bluetooth_admin.request('start ' + MAC, path), {'active': True})
                with self.assertRaisesRegex(RuntimeError, 'AirPlay owns audio'):
                    bluetooth_admin.request('stop', path)
            thread.join(5)
            self.assertEqual(received, [b'start ' + MAC.encode() + b'\n', b'stop\n'])

    def test_cli_canonicalizes_start_and_prints_json(self):
        with patch('bluetooth_admin.os.geteuid', return_value=0), \
             patch('bluetooth_admin.request', return_value={'active': True}) as request, \
             patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(bluetooth_admin.main(['start', MAC.lower()]), 0)
        request.assert_called_once_with('start ' + MAC)
        self.assertEqual(json.loads(out.getvalue()), {'active': True})
        with patch('bluetooth_admin.os.geteuid', return_value=0), \
             patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(bluetooth_admin.main(['start', 'not-a-mac']), 1)


if __name__ == '__main__':
    unittest.main()
