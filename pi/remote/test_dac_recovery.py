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
        self.values = dict(GetConfigJson=json.dumps(self.config), GetMute=False, GetVolume=-24.0,
                           GetConfigFilePath='/profile.yml', GetState='Running')
        def dsp_call(name, *args):
            if name == 'SetMute': self.values['GetMute'] = args[0]
            if name == 'SetVolume': self.values['GetVolume'] = args[0]
            return self.values.get(name)
        self.dsp.call.side_effect = dsp_call
        self.player = dict(state='play', playlist='12', songid='15', song='1', elapsed='126.482')
        self.playlist = [('file', 'track-a'), ('Id', '14'), ('file', 'track-b'), ('Id', '15')]
        def mpd(*commands):
            replies = []
            for command in commands:
                if command == 'pause 1': self.player['state'] = 'pause'
                elif command.startswith('seekid '):
                    _, song, elapsed = command.split()
                    self.player.update(state='play', songid=song, elapsed=elapsed)
                replies.append(self.playlist if command == 'playlistinfo' else list(self.player.items()))
            return replies
        airplay = SimpleNamespace(active=False, idle=False, pending=None, baseline=Mock(), invalidate=Mock())
        airplay.baseline.exists.return_value = False
        self.server = SimpleNamespace(STATE=self.root, LOCK=threading.RLock(), DSP=Mock(return_value=self.dsp),
                    AUDIO_WATCH=Mock(report=dict(fault='dac_output_stalled', source='tidal')),
                    AIRPLAY_CONTROL=airplay, BLUETOOTH_CONTROL=Mock(), recover_source=Mock(),
                    TUNING=Mock(), approved=Mock(), mpd=Mock(side_effect=mpd), source_route=Mock(),
                    CONTROL_GENERATION=0, AUDIO_INTENT=0)
        self.server.BLUETOOTH_CONTROL.owns_audio.return_value = False
        self.server.TUNING.status.return_value = dict(available=True, audio_owned=False)
        self.server.source_route.return_value.exists.return_value = False
        self.pointer = 0
        def pcm():
            self.pointer += 100
            return CLOSED, None, ('RUNNING', '44', self.pointer)
        self.recovery = DacRecovery(self.server, boot_id=self.boot, broker=self.broker, read_pcm=pcm, sleep=Mock())
        self.recovery.installed = Mock(return_value=True)
        self.recovery.lease = Mock()
        self.thread = patch('dac_recovery.threading.Thread').start()
        self.addCleanup(patch.stopall)

    def prepare(self):
        self.recovery.start()
        return json.loads(self.recovery.journal.read_text())

    def test_stream_recovery_keeps_queue_level_and_mute_without_driver_or_reboot(self):
        record = self.prepare()
        self.recovery._run_ladder(record)
        self.assertEqual(self.broker.call_args_list, [call(b'begin'), call(b'stream'), call(b'finish')])
        self.assertEqual(self.recovery.phase, 'restored')
        self.assertEqual(self.player['state'], 'pause')
        self.assertTrue(self.values['GetMute'])
        self.assertEqual(self.values['GetVolume'], -24)
        self.assertFalse(self.recovery.journal.exists())

    def test_all_playback_sources_are_admitted_and_casts_end_through_their_journals(self):
        for source, device in [('airplay','hw:Loopback,1,1'), ('bluetooth','hw:Loopback,1,2'),
                               ('usb:Device:pair-1','hw:Device,0')]:
            with self.subTest(source=source):
                self.recovery.phase = None
                self.config['devices']['capture']['device'] = device
                self.server.AUDIO_WATCH.report['source'] = source
                if source.startswith('usb:'):
                    # Source metadata for USB normally comes from its routing mixer.
                    metadata = patch('dac_recovery.source_metadata', return_value={'id':source})
                else:
                    metadata = patch('dac_recovery.source_metadata', side_effect=lambda cfg: {'id':
                        {'hw:Loopback,1,0':'tidal','hw:Loopback,1,1':'airplay','hw:Loopback,1,2':'bluetooth'}[cfg['devices']['capture']['device']]})
                self.values['GetConfigJson'] = json.dumps(self.config)
                def restore(*args):
                    self.config['devices']['capture']['device'] = TIDAL_DEVICE
                    self.values['GetConfigJson'] = json.dumps(self.config)
                self.server.AIRPLAY_CONTROL.active = source == 'airplay'
                self.server.AIRPLAY_CONTROL.invalidate.side_effect = restore
                self.server.BLUETOOTH_CONTROL.owns_audio.return_value = source == 'bluetooth'
                self.server.BLUETOOTH_CONTROL.stop.side_effect = restore
                with metadata:
                    self.prepare()
                if source == 'airplay': self.server.AIRPLAY_CONTROL.invalidate.assert_called()
                if source == 'bluetooth': self.server.BLUETOOTH_CONTROL.stop.assert_called()
                self.recovery.journal.unlink()
                self.server.AIRPLAY_CONTROL.active = False

    def test_driver_step_only_follows_failed_stream_and_reboot_only_follows_failed_driver(self):
        record = self.prepare()
        self.recovery._verify_local = Mock(side_effect=[RecoveryRefused('stalled'), None, None])
        self.recovery._run_ladder(record)
        self.assertEqual(self.broker.call_args_list, [call(b'begin'),call(b'stream'),call(b'driver'),call(b'finish')])

    def test_both_local_steps_fail_then_reboot_with_durable_record(self):
        record = self.prepare()
        self.recovery._verify_local = Mock(side_effect=RecoveryRefused('stalled'))
        self.recovery._run_ladder(record)
        self.assertEqual(self.broker.call_args_list, [call(b'begin'),call(b'stream'),call(b'driver'),call(b'reboot')])
        self.assertEqual(self.recovery.phase,'rebooting')
        self.assertEqual(json.loads(self.recovery.journal.read_text())['stage'],'reboot')
        self.recovery.lease.release.assert_not_called()

    def test_tuning_or_unconfirmed_mute_never_reaches_root(self):
        self.server.TUNING.status.return_value = dict(available=True,audio_owned=True)
        with self.assertRaisesRegex(RecoveryRefused,'Auto Tune'): self.recovery.start()
        self.broker.assert_not_called()
        self.server.TUNING.status.return_value = dict(available=True,audio_owned=False)
        self.recovery.phase = None
        self.dsp.call.side_effect = lambda n,*a: self.values.get(n)
        with self.assertRaisesRegex(RecoveryRefused,'mute'): self.recovery.start()
        self.broker.assert_not_called()

    def test_empty_queue_and_usb_are_recoverable_without_fabricated_track(self):
        self.player = dict(state='stop',playlist='1')
        self.playlist = []
        record=self.prepare()
        self.assertIsNone(record['position'])
        self.recovery._run_ladder(record)
        self.assertEqual(self.recovery.phase,'restored')
        self.assertEqual(self.player['state'],'stop')

    def test_lost_reboot_reply_retains_busy_latch_and_lease(self):
        record=self.prepare()
        self.recovery._verify_local=Mock(side_effect=RecoveryRefused('stalled'))
        self.broker.side_effect=lambda op: (_ for _ in ()).throw(OSError('lost')) if op==b'reboot' else None
        self.recovery._run_ladder(record)
        self.assertTrue(self.recovery.busy)
        self.assertTrue(self.recovery.journal.exists())
        self.recovery.lease.release.assert_not_called()

    def test_same_boot_process_restart_finishes_a_healthy_local_recovery(self):
        record=self.prepare()
        record['stage']='stream'
        self.recovery._save(record)
        self.recovery.prepare_startup()
        self.recovery.restore()
        self.assertEqual(self.recovery.phase,'restored')
        self.assertFalse(self.recovery.journal.exists())
        self.assertEqual(self.broker.call_args_list[-1],call(b'finish'))

    def test_reboot_restores_current_song_ids_paused_and_muted(self):
        record=self.prepare()
        record['stage']='reboot';self.recovery._save(record)
        self.boot.write_text('newboot')
        self.player.update(state='stop',songid='2',elapsed='0')
        self.playlist=[('file','track-a'),('Id','1'),('file','track-b'),('Id','2')]
        self.recovery.prepare_startup();self.recovery.restore()
        self.assertEqual(self.recovery.phase,'restored')
        self.assertEqual(self.player['state'],'pause')
        self.assertEqual(self.player['songid'],'2')
        self.assertEqual(self.player['elapsed'],'126.482')
        self.assertTrue(self.values['GetMute'])

    def test_changed_queue_keeps_mute_and_recovery_journal(self):
        record=self.prepare();record['stage']='reboot';self.recovery._save(record)
        self.boot.write_text('newboot');self.player['state']='stop'
        self.playlist[0]=('file','changed')
        self.recovery.prepare_startup()
        with self.assertLogs(level='ERROR'):self.recovery.restore()
        self.assertEqual(self.recovery.phase,'failed')
        self.assertTrue(self.recovery.journal.exists())
        self.recovery.lease.release.assert_not_called()


class BrokerTests(unittest.TestCase):
    def setUp(self):
        mute = patch('dac_ladder.muted_dsp')
        mute.start()
        self.addCleanup(mute.stop)

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
