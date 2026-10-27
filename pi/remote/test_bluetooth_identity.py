import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bluetooth_identity import local_policy, verify_identity

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
                with self.assertRaises(RuntimeError):
                    local_policy()
            bus.assert_not_called()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / 'policy.json'
        self.path.write_text(json.dumps(dict(version=1, enabled=True, addresses=[MAC])))
        self.path.chmod(0o600)
        self.file_uid = 0
        self.parent_mode = 0o755
        real_stat, real_fstat = Path.lstat, os.fstat
        def metadata(info, **changes):
            result = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
            result.update(changes)
            return SimpleNamespace(**result)
        def lstat(path):
            info = real_stat(path)
            if path in self.path.parents:
                return metadata(info, st_uid=0, st_mode=stat.S_IFDIR | self.parent_mode)
            return metadata(info, st_uid=self.file_uid)
        def fstat(fd):
            return metadata(real_fstat(fd), st_uid=self.file_uid)
        # Model a protected /etc directory without writing to it or requiring root.
        for patcher in [patch('bluetooth_identity.os.geteuid', return_value=0),
                        patch.object(Path, 'lstat', lstat),
                        patch('bluetooth_identity.os.fstat', side_effect=fstat)]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_explicit_enabled_policy(self):
        self.assertEqual(local_policy(self.path), (MAC,))

    def test_missing_disabled_invalid_duplicate_or_oversized_policy(self):
        cases = [dict(version=1, enabled=False, addresses=[MAC]),
                 dict(version=1, enabled=1, addresses=[MAC]),
                 dict(version=True, enabled=True, addresses=[MAC]),
                 dict(version=1, enabled=True, addresses=[]),
                 dict(version=1, enabled=True, addresses=[MAC.lower()]),
                 dict(version=1, enabled=True, addresses=[MAC, MAC]),
                 dict(version=1, enabled=True, addresses=['FF:FF:FF:FF:FF:FF']),
                 dict(version=1, enabled=True, addresses=[MAC], unknown=True)]
        for policy in cases:
            with self.subTest(policy=policy):
                self.path.write_text(json.dumps(policy))
                with self.assertRaises((RuntimeError, ValueError)):
                    local_policy(self.path)
        for raw in ['{"enabled":true,"enabled":false}', ' ' * 4097, 'null', '[]']:
            self.path.write_text(raw)
            with self.assertRaises((RuntimeError, ValueError)):
                local_policy(self.path)
        self.path.unlink()
        with self.assertRaises(OSError):
            local_policy(self.path)

    def test_unprotected_policy_or_parent(self):
        self.file_uid = 1000
        with self.assertRaisesRegex(RuntimeError, 'root-owned'):
            local_policy(self.path)
        self.file_uid = 0
        self.path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, '0600'):
            local_policy(self.path)
        self.path.chmod(0o600)
        self.parent_mode = 0o777
        with self.assertRaisesRegex(RuntimeError, 'directory'):
            local_policy(self.path)

    def test_relative_and_symlink_policy(self):
        with self.assertRaisesRegex(RuntimeError, 'canonical'):
            local_policy(Path('policy.json'))
        target = self.path.with_name('target.json')
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'canonical'):
            local_policy(self.path)
