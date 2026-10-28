"""Guarded Bluetooth installer. Filesystem in a temp dir; systemctl/groupadd mocked.

The fake systemctl models unit enable/active state so tests can prove the
installer and rollback only read it, never change it.
"""
import ast
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import install
import install_bluetooth

SOURCE = Path(install_bluetooth.__file__).resolve().parent


def local_imports(name):
    """Modules from this directory that name.py imports at any depth."""
    found, pending = set(), [name]
    while pending:
        tree = ast.parse((SOURCE / f'{pending.pop()}.py').read_text())
        for node in ast.walk(tree):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for module in names:
                if (SOURCE / f'{module}.py').is_file() and module not in found:
                    found.add(module)
                    pending.append(module)
    return found


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.policy = root / 'etc/octet/bluetooth.json'
        self.dropin = root / 'systemd/octet-remote.service.d/bluetooth.conf'
        self.unit = root / 'systemd/octet-bluealsa.service'
        self.backup = root / 'backup'
        self.remote = root / 'opt'
        self.remote.mkdir()
        for name in install_bluetooth.REQUIRED:
            (self.remote / name).write_text('installed')
        self.commands = []
        self.blockers = []
        self.groups = {'octet-bluetooth': 4242}
        self.units = {'octet-remote.service': ['enabled', 'active'],
                      'bluealsa.service': ['disabled', 'inactive'],
                      'bluetooth.service': ['enabled', 'active'],
                      'octet-bluealsa.service': ['', 'inactive']}
        self.on_reload = None
        for item in (patch.multiple(install_bluetooth, POLICY=self.policy, DROPIN=self.dropin,
                                    UNIT=self.unit, BACKUP=self.backup, REMOTE=self.remote,
                                    ADMIN_UID=os.getuid()),
                     patch('install_bluetooth.shutil.which',
                           side_effect=lambda name: '/usr/bin/bluealsad' if name == 'bluealsad' else None),
                     patch('install_bluetooth.Path.resolve', lambda path, strict=False: path),
                     patch('install_bluetooth.os.geteuid', return_value=0),
                     patch('install_bluetooth.os.fchown'),
                     patch('install_airplay.os.fchown'),
                     patch('install_bluetooth.preflight', side_effect=lambda: dict(
                         prerequisites_ok=not self.blockers, blockers=self.blockers)),
                     patch('install_bluetooth.grp.getgrnam', side_effect=self.group),
                     patch('install_bluetooth.run', side_effect=self.command)):
            item.start()
            self.addCleanup(item.stop)

    def group(self, name):
        if name not in self.groups:
            raise KeyError(name)
        return SimpleNamespace(gr_gid=self.groups[name])

    def command(self, *args, check=True):
        self.commands.append(list(args))
        stdout = ''
        if args[0] == 'groupadd':
            self.groups[args[-1]] = 4242
        elif args[:2] == ('systemctl', 'daemon-reload'):
            # A unit file without [Install] is reported as static once loaded.
            self.units['octet-bluealsa.service'][0] = 'static' if self.unit.exists() else ''
            if self.on_reload:
                self.on_reload()
        elif args[:2] in (('systemctl', 'is-enabled'), ('systemctl', 'is-active')):
            stdout = self.units[args[2]][args[1] == 'is-active'] + '\n'
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

    def assert_only_reads_unit_state(self):
        for command in self.commands:
            if command[0] == 'systemctl':
                self.assertIn(command[1], ('is-enabled', 'is-active', 'daemon-reload'), command)

    def test_dry_run_changes_nothing(self):
        plan = install_bluetooth.main([])
        self.assertTrue(plan)
        self.assertFalse(self.policy.exists())
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.commands, [])

    def test_apply_installs_disabled_policy_and_dropin_without_starting_anything(self):
        del self.groups['octet-bluetooth']
        install_bluetooth.main(['--apply'])
        self.assertEqual(json.loads(self.policy.read_bytes()),
                         dict(version=1, enabled=False, addresses=[]))
        self.assertEqual(self.policy.stat().st_mode & 0o777, 0o640)
        dropin = self.dropin.read_text()
        self.assertIn('RuntimeDirectory=octet-bluetooth\n', dropin)
        self.assertIn('SupplementaryGroups=octet-bluetooth\n', dropin)
        self.assertIn('ExecStopPost=/usr/bin/python3 /opt/octet-remote/airplay_control.py mute-on-exit', dropin)
        mutations = [c for c in self.commands if c[1] not in ('is-enabled', 'is-active')]
        self.assertEqual(mutations, [['groupadd', '--system', 'octet-bluetooth'],
                                     ['systemctl', 'daemon-reload']])
        self.assert_only_reads_unit_state()
        recorded = json.loads((self.backup / 'units.json').read_text())
        self.assertEqual(recorded['octet-remote.service'], dict(enabled='enabled', active='active'))
        self.assertEqual(recorded['octet-bluealsa.service'], dict(enabled='not-found', active='inactive'))

    def test_bluealsa_unit_is_sink_only_cannot_be_enabled_and_never_pulls_in_bluez(self):
        install_bluetooth.main(['--apply'])
        text = self.unit.read_text()
        self.assertIn('ExecStart=/usr/bin/bluealsad --profile=a2dp-sink\n', text)
        self.assertEqual(text.count('--profile'), 1)
        self.assertNotIn('[Install]', text)
        self.assertNotIn('WantedBy', text)
        self.assertIn('Requisite=bluetooth.service\n', text)
        self.assertNotIn('Requires=', text)
        self.assertNotIn('Conflicts=', text)  # would stop the packaged unit implicitly
        self.assertIn('BusName=org.bluealsa\n', text)
        self.assertIn('Restart=no\n', text)
        self.assertEqual(self.unit.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.units['octet-bluealsa.service'], ['static', 'inactive'])

    def test_unit_state_change_is_reported_never_repaired(self):
        def drift():
            self.units['bluealsa.service'][1] = 'active'
        self.on_reload = drift
        with self.assertRaisesRegex(RuntimeError, r'NOT altered.*bluealsa.service: .*inactive.*-> .*active'):
            install_bluetooth.main(['--apply'])
        self.assert_only_reads_unit_state()
        self.assertTrue((self.backup / 'octet-bluealsa.service.absent').exists())

    def test_noncanonical_daemon_or_symlinked_unit_refused_before_changes(self):
        with patch('install_bluetooth.shutil.which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'daemon'):
                install_bluetooth.main(['--apply'])
        self.unit.parent.mkdir(parents=True)
        self.unit.symlink_to(self.remote / 'server.py')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            install_bluetooth.main(['--apply'])
        self.assertEqual(self.commands, [])
        self.assertFalse(self.policy.exists())
        self.assertFalse(self.backup.exists())

    def test_running_daemon_blocks_install_and_rollback_without_stopping_it(self):
        install_bluetooth.main(['--apply'])
        self.units['octet-bluealsa.service'][1] = 'active'
        for argv in (['--apply'], ['--rollback']):
            with self.subTest(argv=argv), self.assertRaisesRegex(RuntimeError, 'stop it deliberately'):
                install_bluetooth.main(argv)
        self.assertTrue(self.unit.exists())
        self.assertTrue(self.dropin.exists())
        self.assert_only_reads_unit_state()

    def test_bluetooth_group_added_only_when_present(self):
        self.groups['bluetooth'] = 110
        install_bluetooth.main(['--apply'])
        self.assertIn('SupplementaryGroups=octet-bluetooth bluetooth\n', self.dropin.read_text())

    def test_existing_policy_is_never_overwritten(self):
        self.policy.parent.mkdir(parents=True)
        self.policy.write_bytes(b'{"admin": "owned"}')
        with patch('sys.stderr', new_callable=io.StringIO):
            install_bluetooth.main(['--apply'])
        self.assertEqual(self.policy.read_bytes(), b'{"admin": "owned"}')

    def test_existing_root_only_policy_is_reported_not_repaired(self):
        # A root-only 0600 policy would let pairing/trust look fine while
        # octet-remote can never read it; the installer says so and changes nothing.
        self.policy.parent.mkdir(parents=True)
        self.policy.write_bytes(b'{}')
        self.policy.chmod(0o600)
        self.groups['octet-bluetooth'] = self.policy.stat().st_gid
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            install_bluetooth.main(['--apply'])
        self.assertIn('chmod 0640', err.getvalue())
        self.assertEqual(self.policy.stat().st_mode & 0o777, 0o600)
        self.policy.chmod(0o640)
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            install_bluetooth.main(['--apply'])
        self.assertNotIn('chmod 0640', err.getvalue())

    def test_blockers_missing_files_or_non_root_refuse_before_any_change(self):
        self.blockers = ['Dedicated snd_aloop playback 0,2 and capture 1,2 required']
        with self.assertRaisesRegex(RuntimeError, 'snd_aloop'):
            install_bluetooth.main(['--apply'])
        self.blockers = []
        (self.remote / 'bluetooth_control.py').unlink()
        with self.assertRaisesRegex(RuntimeError, 'Octet remote'):
            install_bluetooth.main(['--apply'])
        (self.remote / 'bluetooth_control.py').write_text('installed')
        with patch('install_bluetooth.os.geteuid', return_value=1000):
            with self.assertRaisesRegex(RuntimeError, 'root'):
                install_bluetooth.main(['--apply'])
        self.assertFalse(self.policy.exists())
        self.assertFalse(self.dropin.exists())
        self.assertEqual(self.commands, [])
        self.assertFalse(self.backup.exists())

    def test_unprotected_policy_directory_refused(self):
        self.policy.parent.mkdir(parents=True)
        for mode in (0o777, 0o700, 0o750):  # writable, or unreachable by octet-remote
            with self.subTest(mode=oct(mode)):
                os.chmod(self.policy.parent, mode)
                with self.assertRaisesRegex(RuntimeError, 'protected and searchable'):
                    install_bluetooth.main(['--apply'])
        self.assertFalse(self.dropin.exists())

    def test_rollback_removes_dropin_unit_and_untouched_default_policy_only(self):
        install_bluetooth.main(['--apply'])
        install_bluetooth.main(['--rollback'])
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.policy.exists())
        self.assertIn(['systemctl', 'daemon-reload'], self.commands)
        self.assert_only_reads_unit_state()
        self.assertEqual(self.units['octet-remote.service'], ['enabled', 'active'])
        # A verified rollback consumes its backup; the next install snapshots afresh.
        self.assertEqual(sorted(p.name for p in self.backup.iterdir()), [])
        install_bluetooth.main(['--apply'])
        self.policy.write_text(json.dumps(dict(version=1, enabled=True, addresses=['12:34:56:78:9A:BC'])))
        install_bluetooth.main(['--rollback'])
        self.assertTrue(self.policy.exists())
        self.assertFalse(self.dropin.exists())

    def test_existing_dropin_and_unit_are_backed_up_and_restored(self):
        self.dropin.parent.mkdir(parents=True)
        self.dropin.write_text('[Service]\n# administrator\n')
        self.unit.write_text('# earlier administrator unit\n')
        install_bluetooth.main(['--apply'])
        self.assertIn('octet-bluetooth', self.dropin.read_text())
        self.assertIn('a2dp-sink', self.unit.read_text())
        install_bluetooth.main(['--rollback'])
        self.assertEqual(self.dropin.read_text(), '[Service]\n# administrator\n')
        self.assertEqual(self.unit.read_text(), '# earlier administrator unit\n')

    def test_edits_after_a_rollback_survive_reinstall_and_second_rollback(self):
        install_bluetooth.main(['--apply'])
        install_bluetooth.main(['--rollback'])
        self.dropin.parent.mkdir(parents=True, exist_ok=True)
        self.dropin.write_text('[Service]\n# edited after rollback\n')
        install_bluetooth.main(['--apply'])
        install_bluetooth.main(['--rollback'])
        self.assertEqual(self.dropin.read_text(), '[Service]\n# edited after rollback\n')

    def test_failed_restore_retains_backup_and_reports(self):
        install_bluetooth.main(['--apply'])
        with patch('install_bluetooth.restore_file', side_effect=RuntimeError('verification failed')):
            with self.assertRaisesRegex(RuntimeError, 'backups retained'):
                install_bluetooth.main(['--rollback'])
        self.assertTrue((self.backup / 'units.json').exists())
        self.assertTrue((self.backup / 'octet-remote-bluetooth.conf.absent').exists())
        install_bluetooth.main(['--rollback'])  # retry with the retained backup
        self.assertFalse(self.dropin.exists())
        self.assert_only_reads_unit_state()


class DeployedPackagingTests(unittest.TestCase):
    """A /opt/octet-remote built only from install.py's file list must be self-contained."""

    def deploy(self, skip=()):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        target = Path(tmp.name) / 'octet-remote'
        target.mkdir()
        for name in install.REMOTE_FILES:
            if name not in skip:
                shutil.copy2(SOURCE / name, target / name)
        return target

    def import_from(self, target):
        # -I (Python 3.4+, unlike 3.11's -P): no PYTHONPATH, user site or script/cwd path,
        # so only the deployed directory inserted below (plus stdlib and system
        # site-packages for yaml) can satisfy imports.
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import install_bluetooth, install_airplay; '
                'print(install_bluetooth.__file__); print(install_airplay.__file__)')
        return subprocess.run([sys.executable, '-I', '-c', code, str(target)],
                              cwd=target, capture_output=True, text=True, env={})

    def test_installer_packages_every_local_import_of_install_bluetooth(self):
        needed = local_imports('install_bluetooth')
        self.assertIn('install_airplay', needed)
        self.assertIn('install_airplay.py', install.REMOTE_FILES)
        missing = sorted(f'{name}.py' for name in needed if f'{name}.py' not in install.REMOTE_FILES)
        self.assertEqual(missing, [])

    def test_deployed_directory_imports_install_bluetooth_from_itself(self):
        target = self.deploy()
        result = self.import_from(target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [str(target / 'install_bluetooth.py'),
                                                 str(target / 'install_airplay.py')])

    def test_deployed_directory_imports_the_server_and_its_manual_source_route(self):
        # server.py imports manual_source_route.py; a deployment without it cannot start.
        for name in ('manual_source_route',):
            self.assertIn(name + '.py', install.REMOTE_FILES)
        target = self.deploy()
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import server; '
                'print(server.MANUAL_ROUTE.__class__.__module__); print(server.__file__)')
        result = subprocess.run([sys.executable, '-I', '-c', code, str(target)],
                                cwd=target, capture_output=True, text=True, env={})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ['manual_source_route', str(target / 'server.py')])

    def test_missing_helper_fails_closed_with_clear_message(self):
        target = self.deploy(skip=('install_airplay.py',))
        result = self.import_from(target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires install_airplay.py alongside it in ' + str(target.resolve()), result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
