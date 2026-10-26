from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install_airplay
from test_audio_sources import loopback_fixture


class InstallTests(unittest.TestCase):
    def test_missing_subdevice_and_capabilities_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                install_airplay.check_loopback(root)
            loopback_fixture(root)
            install_airplay.check_loopback(root)
        for version in ('4.3-OpenSSL-Avahi', '4.3-OpenSSL-ALSA', '4.3-AirPlay2-Avahi-ALSA'):
            with self.assertRaises(RuntimeError):
                install_airplay.check_capabilities(version)
        install_airplay.check_capabilities('4.3-OpenSSL-Avahi-ALSA')

    def test_repeat_install_keeps_original_backup_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, backup = root / 'config', root / 'backup'
            target.write_text('original')
            install_airplay.save_file(target, backup)
            target.write_text('new')
            install_airplay.save_file(target, backup)
            self.assertEqual(backup.read_text(), 'original')
            install_airplay.restore_file(target, backup)
            self.assertEqual(target.read_text(), 'original')

    def test_installer_repeat_and_failure_restore_files_and_service_state(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {str(root / str(i)): str(i) for i in range(3)}
            for name in files:
                Path(name).write_text('original')
            calls = []
            enabled = {name: 'disabled' for name in install_airplay.SERVICES}

            def run(*args, **kwargs):
                calls.append(args)
                if args[1] in ('enable', 'disable'):
                    enabled[args[2]] = 'enabled' if args[1] == 'enable' else 'disabled'
                return SimpleNamespace(stdout=enabled[args[-1]] if 'is-enabled' in args else 'inactive\n')

            with patch.object(install_airplay, 'BACKUP', root / 'backup'), \
                 patch.object(install_airplay, 'FILES', files), \
                 patch.object(install_airplay, 'preflight'), \
                 patch.object(install_airplay, 'check_loopback'), \
                 patch.object(install_airplay, 'run', side_effect=run), \
                 patch.object(install_airplay.grp, 'getgrnam'), \
                 patch.object(install_airplay, 'verify_services') as verify:
                install_airplay.install()
                first = [Path(name).read_text() for name in files]
                install_airplay.install()
                self.assertEqual(first, [Path(name).read_text() for name in files])
                verify.side_effect = RuntimeError('receiver rejected config')
                with self.assertRaises(RuntimeError):
                    install_airplay.install()
                self.assertEqual([Path(name).read_text() for name in files], ['original'] * 3)
                self.assertIn(('systemctl', 'disable', 'shairport-sync'), calls)
                self.assertIn(('systemctl', 'stop', 'shairport-sync'), calls)
                self.assertFalse(any('mpd' in command or 'camilladsp' in command for command in calls))

    def test_atomic_write_failure_preserves_old_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'config'
            target.write_text('old')
            with patch.object(install_airplay.os, 'replace', side_effect=OSError('disk')):
                with self.assertRaises(OSError):
                    install_airplay.atomic_write(target, b'new')
            self.assertEqual(target.read_text(), 'old')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_backup_and_repeated_restore_preserve_mode_and_owner(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            target, backup = Path(directory) / 'config', Path(directory) / 'saved'
            target.write_text('old')
            target.chmod(0o640)
            if os.geteuid() == 0:
                os.chown(target, 1234, 1235)
            expected = target.stat()
            install_airplay.save_file(target, backup)
            for _ in range(2):
                install_airplay.atomic_write(target, b'new', 0o600)
                install_airplay.restore_file(target, backup)
                actual = target.stat()
                self.assertEqual((actual.st_uid, actual.st_gid, actual.st_mode),
                                 (expected.st_uid, expected.st_gid, expected.st_mode))

    def test_rollback_continues_after_file_failure_and_can_be_retried(self):
        import json
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / 'backup'
            backup.mkdir()
            files = {str(root / str(i)): str(i) for i in range(3)}
            for name, saved in files.items():
                Path(name).write_text('new')
                (backup / saved).write_text('old')
            (backup / 'services.json').write_text(json.dumps({
                name: dict(enabled='static', active=False) for name in install_airplay.SERVICES}))
            restore = install_airplay.restore_file
            def fail_one(target, saved):
                if target.name == '1':
                    raise OSError('disk failure')
                restore(target, saved)
            with patch.object(install_airplay, 'BACKUP', backup), \
                 patch.object(install_airplay, 'FILES', files), \
                 patch.object(install_airplay, 'run', side_effect=lambda *a, **k:
                              SimpleNamespace(stdout='static' if 'is-enabled' in a else 'inactive')):
                with patch.object(install_airplay, 'restore_file', side_effect=fail_one):
                    with self.assertRaisesRegex(RuntimeError, 'backups retained'):
                        install_airplay.rollback()
                self.assertEqual([Path(name).read_text() for name in files], ['old', 'new', 'old'])
                install_airplay.rollback()
                install_airplay.rollback()
                self.assertEqual([Path(name).read_text() for name in files], ['old'] * 3)

    def test_each_service_must_be_active(self):
        import subprocess
        for failed in install_airplay.SERVICES:
            with self.subTest(failed=failed):
                def run(*args):
                    if args[-1] == failed:
                        raise subprocess.CalledProcessError(3, args)
                with patch.object(install_airplay.time, 'sleep'), \
                     patch.object(install_airplay, 'run', side_effect=run) as command:
                    with self.assertRaises(subprocess.CalledProcessError):
                        install_airplay.verify_services()
                    self.assertTrue(all(len(c.args) == 4 for c in command.call_args_list))

    def test_special_enabled_states_are_started_without_enable_mutations(self):
        for state in ('static', 'indirect', 'alias', 'linked', 'linked-runtime', 'enabled-runtime'):
            with self.subTest(state=state), \
                 patch.object(install_airplay, 'enabled_state', return_value=state), \
                 patch.object(install_airplay, 'run') as command:
                install_airplay.start_service('avahi-daemon')
                command.assert_called_once_with('systemctl', 'start', 'avahi-daemon')

    def test_preflight_uses_documented_capability_flag(self):
        from types import SimpleNamespace
        with patch.object(install_airplay.shutil, 'which', return_value='/usr/bin/tool'), \
             patch.object(install_airplay.pwd, 'getpwnam'), \
             patch.object(install_airplay, 'run', return_value=SimpleNamespace(stdout='ALSA-Avahi', stderr='')) as run, \
             patch.object(install_airplay, 'check_loopback', side_effect=RuntimeError('end preflight')):
            with self.assertRaisesRegex(RuntimeError, 'end preflight'):
                install_airplay.preflight()
            run.assert_called_once_with('shairport-sync', '-V')

    def test_config_has_no_hardware_mixer_or_shared_sink(self):
        text = (Path(__file__).parents[1] / 'etc/shairport-sync.conf').read_text()
        self.assertIn('hw:Loopback,0,1', text)
        self.assertNotIn('hw:Loopback,0,0', text)
        self.assertNotIn('mixer_control_name =', text)
        self.assertIn('wait_for_completion = "yes"', text)
        self.assertIn('output_rate = 44100', text)


if __name__ == '__main__':
    unittest.main()
