"""Pure-logic tests for deploy.py. Nothing here touches a Pi, systemd or /opt."""
from pathlib import Path
import tarfile
import tempfile
import unittest

import deploy
import remote_manifest

ROOT = Path(__file__).resolve().parent


class DropinRepair(unittest.TestCase):
    def test_0700_is_raised_to_0750(self):
        text, changed = deploy.repaired_dropin('[Service]\nRuntimeDirectory=octet-bluetooth\nRuntimeDirectoryMode=0700\n')
        self.assertTrue(changed)
        self.assertIn('RuntimeDirectoryMode=0750\n', text)
        self.assertNotIn('0700', text)

    def test_correct_and_unrelated_files_are_left_alone(self):
        for text in ('[Service]\nRuntimeDirectoryMode=0750\n', '[Service]\nEnvironment=OCTET_BIND=0.0.0.0\n'):
            self.assertEqual(deploy.repaired_dropin(text), (text, False))

    def test_repair_is_idempotent(self):
        once, _ = deploy.repaired_dropin('RuntimeDirectoryMode=0700\n')
        self.assertEqual(deploy.repaired_dropin(once), (once, False))


class Package(unittest.TestCase):
    def test_every_manifest_file_plus_revision_is_shipped(self):
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / 'p.tgz'
            deploy.build_package(dict(commit='abc', branch='main', dirty=False, built='now'), package)
            with tarfile.open(package) as archive:
                names = set(archive.getnames())
        self.assertEqual(names, {'deploy.py', 'remote_manifest.py', 'REVISION', *remote_manifest.REMOTE_FILES})

    def test_server_import_closure_is_covered_by_the_manifest(self):
        self.assertEqual(sorted(deploy.import_closure(ROOT, 'server.py') - set(remote_manifest.REMOTE_FILES)), [])

    def test_proxy_files_are_not_deployed(self):
        self.assertFalse([n for n in remote_manifest.REMOTE_FILES if 'dsp_proxy' in n])


if __name__ == '__main__':
    unittest.main()
