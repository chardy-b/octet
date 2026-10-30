"""Static deployable-set audit for the Octet remote files. Nothing here installs or touches /opt.

Staging happens only in a temp tree. File-list completeness is NOT deployability: install.py
is a first-time installer and must not be used to update a running Octet (see FirstTimeInstallerTests).
"""
import ast
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import install
import install_bluetooth
import remote_manifest

SOURCE = Path(install.__file__).resolve().parent
SYSTEMD = SOURCE.parent / 'systemd'
# Modules the Bluetooth rollout adds on top of what the older server.py imported.
NEW_MODULES = ('bluetooth_auto.py', 'bluetooth_transport.py')


def first_party_closure(root, entry):
    """Every first-party .py file reachable from entry by import at any depth (static, AST)."""
    found, pending = set(), [entry]
    while pending:
        name = pending.pop()
        if name in found:
            continue
        found.add(name)
        for node in ast.walk(ast.parse((root / name).read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split('.')[0]
                if (root / f'{top}.py').is_file():
                    pending.append(f'{top}.py')
    return found


class ManifestTests(unittest.TestCase):
    def test_every_server_transitive_import_is_installed(self):
        closure = first_party_closure(SOURCE, 'server.py')
        self.assertTrue(set(NEW_MODULES) <= closure, 'server.py no longer imports the new modules?')
        self.assertEqual(sorted(closure - set(remote_manifest.REMOTE_FILES)), [])

    def test_new_bluetooth_modules_are_listed(self):
        for name in NEW_MODULES:
            self.assertIn(name, remote_manifest.REMOTE_FILES)

    def test_list_has_no_duplicates_and_every_file_exists(self):
        self.assertEqual(len(remote_manifest.REMOTE_FILES), len(set(remote_manifest.REMOTE_FILES)))
        self.assertEqual([n for n in remote_manifest.REMOTE_FILES if not (SOURCE / n).is_file()], [])

    def test_bluetooth_installer_requires_every_bluetooth_module_server_imports(self):
        bluetooth = {n for n in first_party_closure(SOURCE, 'server.py')
                     if n.startswith(('bluetooth_', 'airplay_'))}
        self.assertEqual(sorted(bluetooth - set(install_bluetooth.REQUIRED)), [])
        self.assertEqual(sorted(set(install_bluetooth.REQUIRED) - set(remote_manifest.REMOTE_FILES)), [])


class StagedTreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.stage = Path(tmp.name) / 'octet-remote'
        self.stage.mkdir()
        for name in remote_manifest.REMOTE_FILES:
            shutil.copy2(SOURCE / name, self.stage / name)

    def test_staged_tree_is_import_closed_for_server(self):
        closure = first_party_closure(self.stage, 'server.py')
        self.assertEqual(sorted(n for n in closure if not (self.stage / n).is_file()), [])
        for name in NEW_MODULES:
            self.assertTrue((self.stage / name).is_file(), name)

    def test_staged_modules_import_from_the_staged_directory_only(self):
        # -I and an empty env: nothing but stdlib and the staged directory can satisfy imports.
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import bluetooth_auto, bluetooth_transport, '
                'print(bluetooth_auto.__file__, bluetooth_transport.__file__)')
        result = subprocess.run([sys.executable, '-I', '-c', code, str(self.stage)],
                                cwd=self.stage, capture_output=True, text=True, env={})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [str(self.stage / n) for n in
                                                 ('bluetooth_auto.py', 'bluetooth_transport.py')])


class FirstTimeInstallerTests(unittest.TestCase):
    """install.py resets live state, so it is only for a fresh Pi; updates go through deploy.py."""

    def setUp(self):
        self.text = (SOURCE / 'install.py').read_text()

    def test_camilla_websocket_binds_loopback_only(self):
        self.assertIn('camilladsp -p 1234 -a 127.0.0.1 ', self.text)
        self.assertNotIn('-a 0.0.0.0', self.text)

    def test_update_path_is_pointed_out(self):
        self.assertRegex(self.text, r'(?m)^#.*deploy\.py')

    def test_installer_still_stops_services_and_copies_media(self):
        self.assertIn("'systemctl', 'stop', 'mpd.socket', 'mpd', 'camilladsp'", self.text)
        self.assertIn('visualizer-media', self.text)


if __name__ == '__main__':
    unittest.main()
