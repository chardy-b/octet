"""Fault injection for durable runtime profile rollback; no audio hardware."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import airplay_control
import server
from test_audio_sources import config, usb


class ProfileRollbackTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory) / 'channel-dsp.yml'
        self.path.write_bytes(b'current complete profile')
        self.previous = b'previous complete profile\n' * 1024
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        self.control = airplay_control.AirPlayController(server)
        self.stack.enter_context(patch.object(server, 'CHANNEL_PROFILE', self.path))
        self.stack.enter_context(patch.object(server, 'DSP', return_value=self.dsp))
        self.stack.enter_context(patch.object(server, 'AIRPLAY_CONTROL', self.control))

    def test_restore_order_and_atomic_visibility(self):
        events = []
        original_temp = tempfile.NamedTemporaryFile
        original_sync = os.fsync
        original_replace = Path.replace
        before = self.path.read_bytes()

        def temporary(**kwargs):
            stream = original_temp(**kwargs)
            proxy = MagicMock(wraps=stream)
            proxy.__enter__.return_value = proxy
            proxy.__exit__.side_effect = stream.__exit__
            proxy.name = stream.name
            for name in ('write', 'flush'):
                method = getattr(stream, name)
                def tracked(*args, name=name, method=method):
                    events.append(name)
                    return method(*args)
                getattr(proxy, name).side_effect = tracked
            return proxy

        def sync(fd):
            directory = stat.S_ISDIR(os.fstat(fd).st_mode)
            events.append('directory sync' if directory else 'file sync')
            if not directory:
                self.assertEqual(os.pread(fd, len(self.previous), 0), self.previous)
            original_sync(fd)

        def replace(candidate, target):
            events.append('replace')
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(candidate.read_bytes(), self.previous)
            self.assertEqual(candidate.parent, target.parent)
            self.assertNotEqual(candidate, target)
            return original_replace(candidate, target)

        with patch.object(tempfile, 'NamedTemporaryFile', side_effect=temporary), \
             patch.object(os, 'fsync', side_effect=sync), patch.object(Path, 'replace', replace):
            server.restore_channel_profile(self.previous)
        self.assertEqual(events, ['write', 'flush', 'file sync', 'replace', 'directory sync'])
        self.assertEqual(self.path.read_bytes(), self.previous)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertIsNone(self.control.restore_pending)
        self.assertFalse(self.control.needs_mute)
        self.assertEqual([c.args for c in self.dsp.call.call_args_list],
                         [('SetMute', True), ('Reload',), ('SetMute', True)])

    def test_each_restore_failure_retains_bytes_and_retries_muted(self):
        for fault in ('create', 'write', 'flush', 'chmod', 'file sync', 'replace',
                      'directory sync', 'mute', 'reload', 'mute after reload'):
            with self.subTest(fault=fault):
                self.path.write_bytes(b'current complete profile')
                before = self.path.read_bytes()
                inode = self.path.stat().st_ino
                self.dsp.call.reset_mock()
                original_temp = tempfile.NamedTemporaryFile
                original_sync = os.fsync

                def temporary(**kwargs):
                    stream = original_temp(**kwargs)
                    proxy = MagicMock(wraps=stream)
                    proxy.name = stream.name
                    proxy.__enter__.return_value = proxy
                    proxy.__exit__.side_effect = stream.__exit__
                    def broken_write(data):
                        stream.write(data[:5])
                        stream.flush()
                        raise OSError('partial write')
                    if fault == 'write':
                        proxy.write.side_effect = broken_write
                    else:
                        proxy.flush.side_effect = OSError('flush')
                    return proxy

                def sync(fd):
                    directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                    if directory == (fault == 'directory sync'):
                        raise OSError(fault)
                    original_sync(fd)

                with ExitStack() as failures:
                    if fault == 'create':
                        failures.enter_context(patch.object(tempfile, 'NamedTemporaryFile', side_effect=OSError(fault)))
                    elif fault in ('write', 'flush'):
                        failures.enter_context(patch.object(tempfile, 'NamedTemporaryFile', side_effect=temporary))
                    elif fault == 'chmod':
                        failures.enter_context(patch.object(os, 'fchmod', side_effect=OSError(fault)))
                    elif fault in ('file sync', 'directory sync'):
                        failures.enter_context(patch.object(os, 'fsync', side_effect=sync))
                    elif fault == 'replace':
                        failures.enter_context(patch.object(Path, 'replace', side_effect=OSError(fault)))
                    else:
                        responses = {'mute': [OSError(fault)], 'reload': [None, OSError(fault), None],
                                     'mute after reload': [None, None, OSError(fault)]}
                        failures.enter_context(patch.object(self.dsp, 'call', side_effect=responses[fault]))
                    with self.assertRaises(OSError):
                        server.restore_channel_profile(self.previous)
                self.assertEqual(self.control.restore_pending, self.previous)
                self.assertTrue(self.control.needs_mute)
                after_replace = fault in ('directory sync', 'reload', 'mute after reload')
                self.assertEqual(self.path.read_bytes(), self.previous if after_replace else before)
                if not after_replace:
                    self.assertEqual(self.path.stat().st_ino, inode)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])
                self.control.tick(now=1)
                self.assertEqual(self.path.read_bytes(), self.previous)
                self.assertFalse(self.control.needs_mute)
                self.assertIsNone(self.control.restore_pending)
                self.assertNotIn(('SetMute', False), [c.args for c in self.dsp.call.call_args_list])

    def test_source_and_output_failures_use_retryable_restore(self):
        for operation in ('source', 'output'):
            with self.subTest(operation=operation):
                self.path.write_bytes(self.previous)
                self.dsp.call.reset_mock()
                loaded = config()
                def call(name, *args):
                    if name == 'Reload':
                        raise RuntimeError('reload failed')
                    return {'GetConfigFilePath': str(self.path), 'GetConfigJson': json.dumps(loaded),
                            'GetState': 'Running', 'GetMute': False}.get(name)
                self.dsp.call.side_effect = call
                with patch.object(server, 'discover_sources', return_value=[usb()]), \
                     patch.object(server.subprocess, 'run', return_value=MagicMock(returncode=0)), \
                     patch.object(server, 'mpd'), patch.object(server.INPUT_LEVELS, 'release'), \
                     patch.object(os, 'fsync', side_effect=OSError('disk failure')):
                    with self.assertRaises(OSError):
                        if operation == 'source':
                            server.select_source(usb()['id'])
                        else:
                            server.set_channel_control(1, 'mute', True)
                self.assertEqual(self.control.restore_pending, self.previous)
                self.assertTrue(self.control.needs_mute)
                self.dsp.call.side_effect = None
                self.control.tick(now=1)
                self.assertEqual(self.path.read_bytes(), self.previous)
                self.assertFalse(self.control.needs_mute)
                self.assertNotIn(('SetMute', False), [c.args for c in self.dsp.call.call_args_list])
