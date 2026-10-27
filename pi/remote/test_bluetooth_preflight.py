import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

import audio_sources
import bluetooth_preflight as bt


def fixture(root):
    card = root / 'card10'
    card.mkdir()
    (root / 'Loopback').symlink_to(card, target_is_directory=True)
    (root / 'modules').write_text('10 snd_aloop\n')
    (card / 'id').write_text('Loopback\n')
    for subdevice in range(3):
        for endpoint, device, stream in [('pcm0p', 0, 'PLAYBACK'), ('pcm1c', 1, 'CAPTURE')]:
            sub = card / endpoint / f'sub{subdevice}'
            sub.mkdir(parents=True)
            (sub / 'info').write_text(f'card: 10\ndevice: {device}\nsubdevice: {subdevice}\nstream: {stream}\n')
            (sub / 'status').write_text('closed\n')
    return card


class BluetoothPreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.card = fixture(self.root)
        self.which = patch.object(bt.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name).start()
        self.run = patch.object(bt.subprocess, 'run', side_effect=lambda args, **kwargs: Mock(returncode=0, stderr=b'', stdout=(
            b'v4.3.1\n' if args[-1] == '--version' else
            b'--profile=NAME a2dp-sink --profile-a2dp --pcm=NAME --volume=TYPE software --single-audio'))).start()
        self.addCleanup(patch.stopall)

    def test_success_is_only_prerequisites_and_never_advertises(self):
        before = {p: p.read_bytes() for p in self.card.rglob('*') if p.is_file()}
        result = bt.preflight(self.root)
        self.assertTrue(result['prerequisites_ok'])
        self.assertFalse(result['available'])
        self.assertFalse(result['advertise'])
        self.assertTrue(result['pending'])
        self.assertEqual((result['playback'], result['capture']), ('hw:Loopback,0,2', 'hw:Loopback,1,2'))
        self.assertEqual(before, {p: p.read_bytes() for p in self.card.rglob('*') if p.is_file()})
        for call in self.run.call_args_list:
            self.assertIn(call.args[0][1:], (['--version'], ['--help']))
            self.assertEqual(call.kwargs, dict(capture_output=True, timeout=2))

    def test_either_missing_third_endpoint_blocks_even_with_other_substreams(self):
        for endpoint in ('pcm0p', 'pcm1c'):
            with self.subTest(endpoint=endpoint):
                info = self.card / endpoint / 'sub2/info'
                saved = info.read_bytes()
                info.unlink()
                self.assertFalse(bt.preflight(self.root)['prerequisites_ok'])
                info.write_bytes(saved)

    def test_real_cli_output_missing_sub2_has_only_one_blocker(self):
        self.which.side_effect = lambda name: None if name == 'bluealsad' else '/usr/bin/' + name
        (self.card / 'pcm0p/sub2/info').unlink()
        result = bt.preflight(self.root)
        self.assertEqual(len(result['blockers']), 1)
        self.assertIn('Dedicated snd_aloop', result['blockers'][0])

    def test_stderr_on_either_probe_is_rejected(self):
        original = self.run.side_effect
        for probe in ('--version', '--help'):
            def response(args, **kwargs):
                result = original(args, **kwargs)
                if args[-1] == probe:
                    result.stderr = b'warning'
                return result
            self.run.side_effect = response
            self.assertEqual(len(bt.preflight(self.root)['blockers']), 2)

    def test_exact_identity_and_idle_status_required(self):
        cases = [('id', 'DAC'), ('../modules', '10 snd_usb_audio'),
                 ('../modules', '10 snd_aloop\n10 snd_aloop')]
        for endpoint in ('pcm0p', 'pcm1c'):
            for field, value in [('card', '9'), ('device', '9'), ('subdevice', '0'),
                                 ('subdevice', '1'), ('stream', 'WRONG')]:
                path = self.card / endpoint / 'sub2/info'
                original = path.read_text()
                changed = '\n'.join(f'{field}: {value}' if line.startswith(field + ':') else line
                                    for line in original.splitlines())
                cases.append((str(path.relative_to(self.card)), changed))
            cases.extend([(endpoint + '/sub2/status', value) for value in
                          ('', 'state: RUNNING', 'state: PREPARED', 'disconnected')])
        for name, value in cases:
            with self.subTest(name=name, value=value):
                path = self.card / name
                saved = path.read_bytes()
                path.write_text(value)
                self.assertFalse(bt.preflight(self.root)['prerequisites_ok'])
                path.write_bytes(saved)

    def test_redirected_endpoint_rejected(self):
        sub = self.card / 'pcm0p/sub2'
        sub.rename(sub.with_name('saved'))
        sub.symlink_to(sub.with_name('sub1'), target_is_directory=True)
        self.assertFalse(bt.preflight(self.root)['prerequisites_ok'])

    def test_redirected_metadata_rejected_before_reading(self):
        read_text = Path.read_text
        for endpoint in ('pcm0p', 'pcm1c'):
            for name in ('info', 'status'):
                with self.subTest(endpoint=endpoint, name=name):
                    metadata = self.card / endpoint / 'sub2' / name
                    saved = metadata.read_bytes()
                    target = metadata.with_name(name + '.saved')
                    metadata.rename(target)
                    metadata.symlink_to(target)

                    def checked_read(path, *args, **kwargs):
                        self.assertNotEqual(path, metadata, 'Redirected metadata was read')
                        self.assertNotEqual(path, target, 'Redirect target was read')
                        return read_text(path, *args, **kwargs)

                    try:
                        with patch.object(Path, 'read_text', checked_read):
                            result = bt.preflight(self.root)
                        self.assertFalse(result['prerequisites_ok'])
                        self.assertFalse(result['available'])
                        self.assertFalse(result['advertise'])
                        self.assertIn('Redirected Loopback endpoint metadata', result['blockers'][0])
                        self.assertTrue(metadata.is_symlink())
                        self.assertEqual(target.read_bytes(), saved)
                    finally:
                        metadata.unlink()
                        target.rename(metadata)

    def test_duplicate_endpoint_fields_fail_closed(self):
        for endpoint in ('pcm0p', 'pcm1c'):
            info = self.card / endpoint / 'sub2/info'
            saved = info.read_text()
            for line in saved.splitlines():
                key, value = line.split(':', 1)
                for duplicate in (value.strip(), 'WRONG'):
                    with self.subTest(endpoint=endpoint, field=key, duplicate=duplicate):
                        info.write_text(saved + f' {key} : {duplicate}\n')
                        try:
                            result = bt.preflight(self.root)
                            self.assertFalse(result['prerequisites_ok'])
                            self.assertFalse(result['available'])
                            self.assertFalse(result['advertise'])
                            self.assertIn('Duplicate endpoint field', result['blockers'][0])
                        finally:
                            info.write_text(saved)

    def test_missing_dependencies_all_reported(self):
        self.which.side_effect = lambda name: None
        (self.card / 'pcm1c/sub2/info').unlink()
        result = bt.preflight(self.root)
        self.assertEqual(len(result['blockers']), 3)
        self.assertFalse(result['prerequisites_ok'])
        self.run.assert_not_called()

    def test_legacy_daemon_name(self):
        self.which.side_effect = lambda name: None if name == 'bluealsad' else '/usr/bin/' + name
        self.assertTrue(bt.preflight(self.root)['prerequisites_ok'])
        self.assertEqual(self.run.call_args_list[0].args[0], ['/usr/bin/bluealsa', '--version'])

    def test_failed_binaries_fail_closed(self):
        for error in (OSError('not executable'), subprocess.TimeoutExpired('version', 2)):
            with self.subTest(error=error):
                self.run.side_effect = error
                self.assertFalse(bt.preflight(self.root)['prerequisites_ok'])
        self.run.side_effect = None
        self.run.return_value.returncode = 1
        self.assertFalse(bt.preflight(self.root)['prerequisites_ok'])

    def test_no_bluetooth_source_even_when_prerequisites_pass(self):
        self.assertTrue(bt.preflight(self.root)['prerequisites_ok'])
        with patch.object(audio_sources, 'probe_pcm', return_value=True):
            self.assertEqual([s['id'] for s in audio_sources.discover_sources(self.root)], ['tidal', 'airplay'])

    def test_unsupported_version_or_help_never_passes_prerequisites(self):
        for version, help_text, help_code in (
                ('4.3.10', '--profile=NAME a2dp-sink', 0),
                ('v4.3.1\n', '--profile=A2DP --pcm=NAME', 0),
                ('v4.3.1\n', '--profile=NAME a2dp-sink', 1)):
            with self.subTest(version=version, help_code=help_code):
                self.run.side_effect = lambda args, **kwargs: Mock(
                    stderr=b'', returncode=0 if args[-1] == '--version' else help_code,
                    stdout=version.encode() if args[-1] == '--version' else help_text.encode())
                result = bt.preflight(self.root)
                self.assertFalse(result['prerequisites_ok'])
                self.assertFalse(result['available'])
                self.assertFalse(result['advertise'])

    def test_cli_status(self):
        for ok, code in ((True, 0), (False, 1)):
            with patch.object(bt, 'preflight', return_value=dict(prerequisites_ok=ok)), patch('builtins.print') as output:
                self.assertEqual(bt.main(), code)
                self.assertEqual(json.loads(output.call_args.args[0]), dict(prerequisites_ok=ok))


if __name__ == '__main__':
    unittest.main()
