"""One-step Bluetooth setup. Filesystem in a temp dir; systemctl, dpkg and apt faked.

A fresh Pi gets every step once, in order; a Pi that is set up gets nothing.
"""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import setup_bluetooth

REPO = setup_bluetooth.REPO


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.home = root / 'home/octet'
        self.home.mkdir(parents=True)
        self.systemd = root / 'systemd'
        self.panel = self.home / '.config/wf-panel-pi/wf-panel-pi.ini'
        self.backup = root / 'backup'
        self.packages, self.masked, self.enabled = set(), set(), set()
        self.commands, self.installs, self.chowned = [], [], []
        account = SimpleNamespace(pw_dir=str(self.home), pw_uid=1000, pw_gid=1000)
        for item in (patch.multiple(setup_bluetooth, SYSTEMD=self.systemd, BACKUP=self.backup,
                                    WIREPLUMBER=root / 'wireplumber/90-octet-bluealsa.conf',
                                    AUTO=self.systemd / 'octet-remote.service.d/bluetooth-auto.conf',
                                    JUSTWORKS=self.systemd / 'octet-bluetooth-justworks.service'),
                     patch('setup_bluetooth.pwd.getpwnam', return_value=account),
                     patch('setup_bluetooth.pwd.getpwuid', return_value=account),
                     patch('setup_bluetooth.os.geteuid', return_value=0),
                     patch('setup_bluetooth.os.chown', side_effect=lambda path, *owner: self.chowned.append(path)),
                     patch('install_airplay.os.fchown'),
                     patch('setup_bluetooth.run', side_effect=self.command),
                     patch('setup_bluetooth.apt', side_effect=self.apt),
                     patch('setup_bluetooth.receiver_installed', side_effect=lambda: bool(self.installs)),
                     patch('install_bluetooth.install', side_effect=lambda: self.installs.append(True))):
            item.start()
            self.addCleanup(item.stop)

    def apt(self, *args):
        self.commands.append(['apt-get', *args])
        if args[0] == 'install':
            self.packages.update(args[2:])

    def command(self, *args, check=True):
        self.commands.append(list(args))
        stdout = ''
        if args[0] == 'dpkg-query':
            stdout = 'install ok installed' if args[-1] in self.packages else ''
        elif args[:2] == ('systemctl', 'mask'):
            self.masked.update(args[3:])
        elif args[:2] == ('systemctl', 'enable'):
            self.enabled.add(args[2])
        elif args[:2] == ('systemctl', 'add-wants'):
            wants = self.systemd / 'multi-user.target.wants' / args[3]
            wants.parent.mkdir(parents=True, exist_ok=True)
            wants.touch()
        elif args[:2] == ('systemctl', 'is-enabled'):
            stdout = 'masked' if args[2] in self.masked else 'enabled' if args[2] in self.enabled else 'disabled'
        return SimpleNamespace(returncode=0, stdout=stdout + '\n', stderr='')

    def changes(self):
        return [c for c in self.commands if c[0] == 'apt-get' or (
            c[0] == 'systemctl' and c[1] not in ('is-enabled', 'is-active'))]

    def test_dry_run_lists_every_step_and_changes_nothing(self):
        todo = setup_bluetooth.main([])
        self.assertEqual(len(todo), 9)
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.installs, [])
        self.assertFalse(self.panel.exists())
        self.assertFalse(setup_bluetooth.JUSTWORKS.exists())

    def test_fresh_pi_gets_every_step_in_order_then_nothing_on_rerun(self):
        setup_bluetooth.main(['--apply'])
        self.assertEqual(self.packages, set(setup_bluetooth.PACKAGES))
        self.assertEqual(self.masked, set(setup_bluetooth.DISTRO_UNITS))
        self.assertEqual(self.installs, [True])
        self.assertIn('octet-bluetooth-justworks.service', self.enabled)
        self.assertTrue((self.systemd / 'multi-user.target.wants/octet-bluealsa.service').exists())
        for path, source in ((setup_bluetooth.WIREPLUMBER, 'install/wireplumber-bluealsa.conf'),
                             (setup_bluetooth.AUTO, 'systemd/octet-remote-bluetooth-auto.conf'),
                             (setup_bluetooth.JUSTWORKS, 'systemd/octet-bluetooth-justworks.service'),
                             (self.panel, 'home/wf-panel-pi.ini')):
            self.assertEqual(path.read_bytes(), (REPO / source).read_bytes(), path)
        self.assertNotIn('bluetooth', self.panel.read_text().split('widgets_right')[-1].split('\n')[0])
        # The taskbar config belongs to the desktop user, including new directories.
        self.assertEqual(self.chowned, [self.home / '.config', self.home / '.config/wf-panel-pi'])
        mask = self.commands.index(['systemctl', 'mask', '--now', *setup_bluetooth.DISTRO_UNITS])
        wants = self.commands.index(['systemctl', 'add-wants', 'multi-user.target', 'octet-bluealsa.service'])
        self.assertLess(mask, wants)
        self.commands.clear()
        self.assertEqual(setup_bluetooth.main(['--apply']), [])
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.installs, [True])

    def test_existing_taskbar_config_is_backed_up_before_replacing(self):
        self.panel.parent.mkdir(parents=True)
        self.panel.write_text('[panel]\nwidgets_right=bluetooth\n')
        setup_bluetooth.main(['--apply'])
        backups = list(self.backup.glob('*/wf-panel-pi.ini'))
        self.assertEqual([b.read_text() for b in backups], ['[panel]\nwidgets_right=bluetooth\n'])
        self.assertEqual(self.panel.read_bytes(), (REPO / 'home/wf-panel-pi.ini').read_bytes())

    @unittest.skipUnless(hasattr(os, 'symlink'), 'needs symlinks')
    def test_symlinked_target_is_refused(self):
        target = setup_bluetooth.WIREPLUMBER
        target.parent.mkdir(parents=True)
        os.symlink(self.home, target)
        with self.assertRaises(RuntimeError):
            setup_bluetooth.main(['--apply'])

    def test_requires_root(self):
        with patch('setup_bluetooth.os.geteuid', return_value=1000), self.assertRaises(RuntimeError):
            setup_bluetooth.main([])


if __name__ == '__main__':
    unittest.main()
