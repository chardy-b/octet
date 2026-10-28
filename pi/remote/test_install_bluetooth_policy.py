"""Installer policy-default tests: fresh install stays disabled v1 restricted (WIL-249 T4a)."""
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bluetooth_identity
import install_bluetooth

OPEN = dict(version=2, mode='open', enabled=True, addresses=[])


def as_root(info, **changes):
    result = {key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
    result.update(st_uid=0, **changes)
    return SimpleNamespace(**result)


class InstallerPolicyDefaultTests(unittest.TestCase):
    def read(self, raw):
        """admission_policy() of raw bytes, modelling a protected root:octet-bluetooth file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'bluetooth.json'
            path.write_bytes(raw)
            path.chmod(0o640)
            gid = os.stat(path).st_gid
            real_lstat, real_fstat = Path.lstat, os.fstat
            parents = path.parents
            with patch('bluetooth_identity.grp.getgrnam', return_value=SimpleNamespace(gr_gid=gid)), \
                 patch.object(Path, 'lstat', lambda p: as_root(
                     real_lstat(p), **({'st_mode': stat.S_IFDIR | 0o755} if p in parents else {}))), \
                 patch('bluetooth_identity.os.fstat', lambda fd: as_root(real_fstat(fd))):
                return bluetooth_identity.admission_policy(path)

    def test_default_is_exact_disabled_v1_restricted(self):
        self.assertEqual(json.loads(install_bluetooth.DEFAULT),
                         dict(version=1, enabled=False, addresses=[]))
        policy = self.read(install_bluetooth.DEFAULT)
        self.assertEqual((policy.version, policy.mode, policy.enabled, policy.addresses),
                         (1, 'restricted', False, ()))

    def test_plan_names_restricted_default(self):
        self.assertTrue(any('disabled v1 restricted' in step for step in install_bluetooth.plan()))
        self.assertFalse(any('open' in step.split() for step in install_bluetooth.plan()))

    def test_install_source_never_writes_an_open_policy(self):
        self.assertNotIn(b'"open"', install_bluetooth.DEFAULT)
        self.assertEqual(self.read(json.dumps(OPEN).encode()).mode, 'open')


if __name__ == '__main__':
    unittest.main()
