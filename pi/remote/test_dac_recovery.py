import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, call, patch

import dac_reboot
from audio_watch import CLOSED, DAC_DEVICE
from dac_recovery import DacRecovery, RecoveryRefused
from dsp_inputs import TIDAL_DEVICE


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.boot = self.root / 'boot-id'
        self.boot.write_text('original')
        self.broker = Mock()
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        self.config = dict(devices=dict(capture=dict(device=TIDAL_DEVICE), playback=dict(device=DAC_DEVICE)))
        self.values = dict(GetConfigJson=json.dumps(self.config), GetMute=False)
        self.dsp.call.side_effect = self.dsp_call
        self.player = dict(state='play', playlist='12', songid='15', song='1', elapsed='126.482')
        self.playlist = [('file', 'track-a'), ('Id', '14'), ('file', 'track-b'), ('Id', '15')]
        self.server = SimpleNamespace(STATE=self.root, LOCK=threading.RLock(), DSP=Mock(return_value=self.dsp),
                                      AUDIO_WATCH=Mock(report=dict(fault='dac_output_stalled', source='tidal')),
                                      TUNING=Mock(), approved=Mock(), mpd=Mock(side_effect=self.mpd), source_route=Mock(),
                                      CONTROL_GENERATION=0, AUDIO_INTENT=0)
        self.server.TUNING.status.return_value = dict(available=True, audio_owned=False)
        self.server.source_route.return_value.exists.return_value = False
        self.recovery = DacRecovery(self.server, boot_id=self.boot, broker=self.broker,
                                    read_pcm=Mock(return_value=(CLOSED, None, None)), sleep=Mock())
        self.recovery.installed = Mock(return_value=True)
        self.recovery.lease = Mock()

    def dsp_call(self, name, *args):
        if name == 'SetMute':
            self.values['GetMute'] = args[0]
            return None
        return self.values[name]

    def mpd(self, *commands):
        replies = []
        for command in commands:
            if command == 'pause 1':
                self.player['state'] = 'pause'
            if command.startswith('seekid '):
                _, song, elapsed = command.split()
                self.player.update(state='play', songid=song, elapsed=elapsed)
            replies.append(list(self.player.items()) if command == 'status'
                           else self.playlist if command == 'playlistinfo' else [])
        return replies

    def test_reboot_requires_durable_snapshot_mute_and_paused_queue(self):
        def reboot(operation):
            if operation == b'reboot':
                self.assertTrue(self.values['GetMute'])
                self.assertEqual(self.player['state'], 'pause')
                self.assertEqual(json.loads(self.recovery.journal.read_text())['elapsed'], 126.482)

        self.broker.side_effect = reboot
        self.assertEqual(self.recovery.start(), {'phase': 'rebooting'})
        self.assertEqual(self.broker.call_args_list, [call(b'check'), call(b'reboot')])
        self.assertEqual(self.server.AUDIO_INTENT, 1)
        self.recovery.lease.release.assert_not_called()
        with self.assertRaisesRegex(RecoveryRefused, 'pending'):
            self.recovery.start()

    def test_missing_kernel_evidence_never_touches_playback(self):
        self.broker.side_effect = RecoveryRefused('No repeated errors')
        with self.assertLogs(level='WARNING'):
            self.recovery.automatic()
        self.server.DSP.assert_not_called()
        self.server.mpd.assert_not_called()
        self.assertFalse(self.recovery.journal.exists())

    def test_unconfirmed_mute_never_pauses_or_requests_reboot(self):
        self.dsp.call.side_effect = lambda name, *args: self.values.get(name)
        with self.assertRaisesRegex(RecoveryRefused, 'mute could not be verified'):
            self.recovery.start()
        self.broker.assert_called_once_with(b'check')
        self.assertEqual(self.player['state'], 'play')
        self.assertFalse(self.recovery.journal.exists())

    def test_lost_broker_reply_keeps_the_request_latched(self):
        self.broker.side_effect = [None, OSError('lost reply')]
        with self.assertRaises(RecoveryRefused):
            self.recovery.start()
        self.assertTrue(self.recovery.busy)
        self.assertTrue(self.recovery.journal.exists())
        self.recovery.lease.release.assert_not_called()

    def test_restore_uses_current_mpd_ids_and_leaves_the_dsp_muted(self):
        self.recovery.start()
        self.boot.write_text('recovery boot')
        self.player.update(state='stop', songid='2', elapsed='0')
        self.playlist = [('file', 'track-a'), ('Id', '1'), ('file', 'track-b'), ('Id', '2')]
        self.recovery.read_pcm.side_effect = [(CLOSED, None, ('RUNNING', '44', n)) for n in (10, 20)]
        self.recovery.prepare_startup()
        self.recovery.restore()
        self.assertEqual(self.recovery.phase, 'restored')
        self.assertEqual((self.player['state'], self.player['songid'], self.player['elapsed']), ('pause', '2', '126.482'))
        self.assertFalse(self.recovery.journal.exists())
        self.assertTrue(self.values['GetMute'])
        self.assertEqual(self.broker.call_args_list[-1], call(b'ready'))

    def test_changed_queue_stays_stopped_and_retains_recovery_record(self):
        self.recovery.start()
        self.boot.write_text('recovery boot')
        self.player.update(state='stop', elapsed='0')
        self.playlist[-2] = ('file', 'different-track')
        self.recovery.read_pcm.side_effect = [(CLOSED, None, ('RUNNING', '44', n)) for n in (10, 20)]
        self.recovery.prepare_startup()
        with self.assertLogs(level='ERROR'):
            self.recovery.restore()
        self.assertEqual(self.recovery.phase, 'failed')
        self.assertEqual(self.player['state'], 'stop')
        self.assertTrue(self.recovery.journal.exists())

    def test_normal_boot_restores_paused_position_once_without_a_reboot_request(self):
        import dac_recovery

        self.values['GetMute'] = True
        self.player.update(state='stop', songid='15', elapsed='0')
        snapshot = self.root / 'mpd-boot.json'
        digest, _ = dac_recovery.queue_digest(self.playlist)
        snapshot.write_text(json.dumps(dict(boot='original', queue=digest, position=1, elapsed=42.5)))
        self.recovery.read_pcm.side_effect = [(CLOSED, None, ('RUNNING', '44', n)) for n in (10, 20)]
        with patch.object(dac_recovery, 'MPD_BOOT_STATE', snapshot):
            self.recovery.prepare_startup()
            self.recovery.restore()
            self.assertEqual(self.player['state'], 'pause')
            self.assertEqual(self.player['elapsed'], '42.5')
            self.assertEqual(self.recovery.phase, 'restored')
            self.recovery.phase = None  # A web service restart on the same boot.
            self.recovery.prepare_startup()
            self.assertIsNone(self.recovery.phase)
        self.broker.assert_not_called()


class BrokerTests(unittest.TestCase):
    def test_boot_snapshot_uses_mpd_state_position_and_ordered_tidal_uris(self):
        urls = ['http://127.0.0.1:8767/stream/1?entry=abcd', 'http://127.0.0.1:8767/stream/2?entry=ef01']
        state = 'state: pause\ncurrent: 1\ntime: 25.583\nplaylist_begin\n' + ''.join(
            f'song_begin: {url}\nsong_end\n' for url in urls) + 'playlist_end\n'
        record = dac_reboot.paused_mpd_record(state)
        self.assertEqual((record['position'], record['elapsed']), (1, 25.583))
        self.assertIsNone(dac_reboot.paused_mpd_record(state.replace('state: pause', 'state: stop')))
        self.assertIsNone(dac_reboot.paused_mpd_record(state.replace('/stream/2', '/wrong/2')))

    def test_same_boot_and_cross_boot_retries_are_bounded(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            boot = root / 'boot-id'
            boot.write_text('first')
            with (patch.object(dac_reboot, 'STATE', root), patch.object(dac_reboot, 'BOOT_ID', boot),
                  patch.object(dac_reboot, 'fault_evidence', return_value=['kernel evidence']),
                  patch.object(dac_reboot, 'run', return_value='octet-dac-graph.service --mute') as run,
                  patch.object(dac_reboot.time, 'time', return_value=100)):
                loopback = root / 'pcm0p/sub0/status'
                loopback.parent.mkdir(parents=True)
                loopback.write_text('closed\n')
                with patch.object(dac_reboot, 'LOOPBACK', root):
                    dac_reboot.handle(b'reboot')
                with self.assertRaisesRegex(RuntimeError, 'reboot limit'):
                    dac_reboot.handle(b'check')
                boot.write_text('second')
                with self.assertRaisesRegex(RuntimeError, 'reboot limit'):
                    dac_reboot.handle(b'check')
                self.assertEqual(sum(c.args[0] == '/usr/bin/systemd-run' for c in run.call_args_list), 1)

    def test_reboot_requires_the_installed_boot_gate_before_reading_faults(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            boot = root / 'boot-id'
            boot.write_text('first')
            with (patch.object(dac_reboot, 'STATE', root), patch.object(dac_reboot, 'BOOT_ID', boot),
                  patch.object(dac_reboot, 'run', return_value='unrelated.service'),
                  patch.object(dac_reboot, 'fault_evidence') as evidence):
                with self.assertRaisesRegex(RuntimeError, 'graph gate is not enabled'):
                    dac_reboot.handle(b'reboot')
                evidence.assert_not_called()
                self.assertIsNone(dac_reboot.read_attempt())

    def test_fault_evidence_uses_the_mapped_tx_channel_and_requires_repeated_errors(self):
        with (patch.object(dac_reboot, 'DMA_SUMMARY') as summary,
              patch.object(dac_reboot, 'pcm_sample', return_value=('42', 'XRUN', '0')),
              patch.object(dac_reboot.time, 'sleep'), patch.object(dac_reboot, 'run') as run):
            summary.read_text.return_value = ' dma2chan4 | 1f000a0000.i2s:tx\n dma2chan5 | 1f000a0000.i2s:rx\n'
            run.return_value = 'failed to stop\nis non-idle!\n'
            with self.assertRaisesRegex(RuntimeError, 'repeated recent'):
                dac_reboot.fault_evidence()
            self.assertIn('dma2chan4', run.call_args.args[-1])
            self.assertNotIn('dma2chan5', run.call_args.args[-1])
