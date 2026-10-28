"""Effective-property tests for the CamillaDSP private-namespace unit and its proxy unit.

Properties are read from the composed unit (base fragment plus optional drop-ins, with systemd's
empty-assignment list reset), never from comments. Source-only: these units must not be deployed
without the reviewed atomic client/service migration.
"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SYSTEMD = Path(__file__).resolve().parents[1] / 'systemd'
CAMILLA = SYSTEMD / 'camilladsp.service'
PROXY = SYSTEMD / 'octet-dsp-proxy.service'
LIST_KEYS = {'ExecStart', 'ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecStopPost'}


def compose(*paths):
    """Return {section: {key: [values]}}; an empty assignment resets list-type keys."""
    unit = {}
    for path in paths:
        section = None
        for raw in Path(path).read_text().splitlines():
            line = raw.strip()
            if not line or line[0] in '#;':
                continue
            if line.startswith('[') and line.endswith(']'):
                section = unit.setdefault(line[1:-1], {})
                continue
            key, _, value = line.partition('=')
            key, value = key.strip(), value.strip()
            values = section.setdefault(key, [])
            if value == '' and key in LIST_KEYS:
                values.clear()
            elif key in LIST_KEYS or key in {'After', 'Before', 'Wants', 'Requires', 'BindsTo',
                                             'WantedBy', 'PartOf', 'JoinsNamespaceOf', 'RestrictAddressFamilies',
                                             'IPAddressAllow', 'IPAddressDeny'}:
                values.append(value)
            else:
                values[:] = [value]
    return unit


def words(unit, section, key):
    return [w for v in unit[section].get(key, []) for w in v.split()]


def one(unit, section, key):
    values = unit[section].get(key, [])
    assert len(values) == 1, (section, key, values)
    return values[0]


class CamillaUnitTest(unittest.TestCase):
    def setUp(self):
        self.unit = compose(CAMILLA)
        self.exec = one(self.unit, 'Service', 'ExecStart').split()

    def option(self, flag):
        return self.exec[self.exec.index(flag) + 1]

    def test_private_network_namespace(self):
        self.assertEqual(one(self.unit, 'Service', 'PrivateNetwork'), 'yes')

    def test_listener_is_private_loopback_only(self):
        self.assertEqual(self.option('-a'), '127.0.0.1')
        self.assertEqual(self.option('-p'), '1234')
        self.assertEqual(self.exec.count('-a'), 1)
        self.assertNotIn('0.0.0.0', ' '.join(self.exec))

    def test_starts_muted_with_safe_gain(self):
        self.assertIn('--mute', self.exec)
        self.assertEqual(self.exec[self.exec.index('--gain') + 1] if '--gain' in self.exec
                         else [a for a in self.exec if a.startswith('--gain=')][0].split('=')[1], '-36')

    def test_keeps_state_log_and_wait_flag(self):
        self.assertEqual(self.exec[0], '/opt/camilladsp/camilladsp')
        self.assertEqual(self.option('-s'), '/opt/camilladsp/statefile.yml')
        self.assertEqual(self.option('-o'), '/opt/camilladsp/camilladsp.log')
        self.assertIn('-w', self.exec)

    def test_audio_identity_and_realtime_priority_retained(self):
        s = 'Service'
        self.assertEqual(one(self.unit, s, 'User'), 'octet')
        self.assertEqual(one(self.unit, s, 'Group'), 'audio')
        self.assertEqual(one(self.unit, s, 'LimitRTPRIO'), '99')
        self.assertEqual(one(self.unit, s, 'LimitMEMLOCK'), 'infinity')
        self.assertEqual(one(self.unit, s, 'CPUSchedulingPolicy'), 'fifo')
        self.assertEqual(one(self.unit, s, 'CPUSchedulingPriority'), '50')
        self.assertEqual(one(self.unit, s, 'Restart'), 'always')
        self.assertIn('sound.target', words(self.unit, 'Unit', 'After'))

    def test_does_not_disable_device_access(self):
        self.assertNotIn('PrivateDevices', self.unit['Service'])

    def test_legacy_exec_drop_in_would_override_base(self):
        """Documents the migration hazard: an old octet-startup.conf resets ExecStart to 0.0.0.0."""
        with tempfile.TemporaryDirectory() as d:
            drop = Path(d, 'octet-startup.conf')
            drop.write_text('[Service]\nExecStart=\nExecStart=/x -a 0.0.0.0\n')
            composed = compose(CAMILLA, drop)
        self.assertEqual(composed['Service']['ExecStart'], ['/x -a 0.0.0.0'])


class ProxyUnitTest(unittest.TestCase):
    def setUp(self):
        self.unit = compose(PROXY)
        self.camilla = compose(CAMILLA)

    def test_distinct_identity(self):
        s = 'Service'
        self.assertEqual(one(self.unit, s, 'User'), 'octet-dsp-proxy')
        self.assertEqual(one(self.unit, s, 'Group'), 'octet-dsp-control')
        self.assertNotEqual(one(self.unit, s, 'User'), one(self.camilla, s, 'User'))
        self.assertNotIn('SupplementaryGroups', self.unit[s])
        self.assertNotIn('audio', one(self.unit, s, 'Group'))

    def test_joins_camilla_namespace(self):
        self.assertEqual(words(self.unit, 'Unit', 'JoinsNamespaceOf'), ['camilladsp.service'])
        self.assertEqual(one(self.unit, 'Service', 'PrivateNetwork'), 'yes')
        self.assertEqual(one(self.camilla, 'Service', 'PrivateNetwork'), 'yes')

    def test_fail_closed_without_camilla(self):
        for key in ('Requires', 'BindsTo', 'After'):
            self.assertIn('camilladsp.service', words(self.unit, 'Unit', key), key)
        self.assertNotIn('camilladsp.service', words(self.unit, 'Unit', 'Wants'))
        self.assertEqual(one(self.unit, 'Service', 'Restart'), 'on-failure')

    def test_runtime_directory(self):
        s = 'Service'
        self.assertEqual(words(self.unit, s, 'RuntimeDirectory'), ['octet-dsp'])
        self.assertEqual(one(self.unit, s, 'RuntimeDirectoryMode'), '0750')
        self.assertEqual(one(self.unit, s, 'RuntimeDirectoryPreserve') if 'RuntimeDirectoryPreserve' in self.unit[s] else 'no', 'no')

    def test_exec_start_exact(self):
        self.assertEqual(self.unit['Service']['ExecStart'],
                         ['/usr/bin/python3 /opt/octet-remote/dsp_proxy.py'])

    def test_no_generic_tcp_exposure(self):
        s = self.unit['Service']
        self.assertEqual(sorted(words(self.unit, 'Service', 'RestrictAddressFamilies')), ['AF_INET', 'AF_UNIX'])
        self.assertEqual(words(self.unit, 'Service', 'IPAddressDeny'), ['any'])
        self.assertEqual(sorted(words(self.unit, 'Service', 'IPAddressAllow')), ['127.0.0.0/8'])
        for key in ('ExecStart', 'ListenStream', 'Sockets'):
            self.assertNotIn('0.0.0.0', ' '.join(s.get(key, [])))

    def test_hardening(self):
        s = 'Service'
        for key, value in {'NoNewPrivileges': 'yes', 'ProtectSystem': 'strict', 'ProtectHome': 'yes',
                           'PrivateTmp': 'yes', 'PrivateDevices': 'yes', 'ProtectKernelTunables': 'yes',
                           'ProtectKernelModules': 'yes', 'ProtectControlGroups': 'yes',
                           'RestrictSUIDSGID': 'yes', 'LockPersonality': 'yes',
                           'MemoryDenyWriteExecute': 'yes', 'RestrictRealtime': 'yes',
                           'RestrictNamespaces': 'yes', 'UMask': '0007'}.items():
            self.assertEqual(one(self.unit, s, key), value, key)
        self.assertEqual(self.unit[s]['CapabilityBoundingSet'], [''])
        self.assertEqual(self.unit[s]['AmbientCapabilities'], [''])
        self.assertNotIn('ReadWritePaths', self.unit[s])

    def test_restart_with_camilla(self):
        """BindsTo stops the proxy with Camilla; PartOf plus a camilladsp.service.wants link restart it."""
        self.assertEqual(words(self.unit, 'Unit', 'PartOf'), ['camilladsp.service'])
        self.assertEqual(words(self.unit, 'Install', 'WantedBy'), ['camilladsp.service'])
        self.assertIn('camilladsp.service', words(self.unit, 'Unit', 'BindsTo'))

    def test_camilla_independent_of_proxy(self):
        for key in ('Requires', 'Requisite', 'BindsTo', 'PartOf', 'After', 'Before', 'Wants', 'Upholds'):
            self.assertNotIn('octet-dsp-proxy.service', words(self.camilla, 'Unit', key), key)
            self.assertNotIn('octet-dsp-proxy.service', words(self.camilla, 'Service', key), key)
        self.assertEqual(words(self.camilla, 'Install', 'WantedBy'), ['multi-user.target'])


@unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable')
class AnalyzeVerifyTest(unittest.TestCase):
    def test_verify_in_scratch_root(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'etc/systemd/system').mkdir(parents=True)
            for name, mode in (('opt/camilladsp/camilladsp', 0o755), ('usr/bin/python3', 0o755),
                               ('opt/octet-remote/dsp_proxy.py', 0o644)):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#!/bin/sh\nexit 0\n')
                path.chmod(mode)
            (root / 'etc/passwd').write_text('root:x:0:0::/root:/bin/sh\noctet:x:1000:29::/:/bin/sh\n'
                                             'octet-dsp-proxy:x:998:997::/:/bin/sh\n')
            (root / 'etc/group').write_text('root:x:0:\naudio:x:29:\noctet-dsp-control:x:996:\n')
            units = []
            for src in (CAMILLA, PROXY):
                dst = root / 'etc/systemd/system' / src.name
                shutil.copy(src, dst)
                units.append(str(dst))
            for target in ('sound', 'sysinit', 'basic', 'shutdown', 'multi-user'):
                (root / f'etc/systemd/system/{target}.target').write_text('[Unit]\nDescription=stub\n')
            wants = root / 'etc/systemd/system/camilladsp.service.wants'
            wants.mkdir()
            (wants / PROXY.name).symlink_to('../' + PROXY.name)
            result = subprocess.run(['systemd-analyze', 'verify', f'--root={root}', *units],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Unknown key', result.stderr)
            self.assertNotIn('Unknown lvalue', result.stderr)


if __name__ == '__main__':
    unittest.main()
