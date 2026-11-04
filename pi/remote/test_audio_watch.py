import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, call

from audio_watch import AudioWatch, CLOSED, DAC_DEVICE, pcm
from dsp_inputs import TIDAL_DEVICE


def running(pointer, owner='123'):
    return ('RUNNING', owner, pointer)


class AudioWatchTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(devices=dict(capture=dict(device=TIDAL_DEVICE),
                                        playback=dict(device=DAC_DEVICE)))
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        self.values = dict(GetConfigJson=json.dumps(self.config), GetState='Stalled',
                           GetConfigFilePath='/preset.yml', GetVolume=-36, GetMute=True,
                           SetMute=None)
        self.dsp.call.side_effect = lambda name, *args: self.values[name]
        self.player = dict(state='play', playlist='12', songid='3', elapsed='21.4')
        self.samples = Mock()
        airplay = SimpleNamespace(active=False, idle=False, pending=None, needs_mute=False,
                                  restore_pending=None, baseline=Mock(), hook_busy=threading.Lock())
        airplay.baseline.exists.return_value = False
        self.server = SimpleNamespace(DSP=Mock(return_value=self.dsp), mpd=Mock(side_effect=self.mpd),
                                      AIRPLAY_CONTROL=airplay, BLUETOOTH_CONTROL=Mock(),
                                      TUNING=Mock(), LOCK=threading.RLock(), approved=Mock())
        self.server.BLUETOOTH_CONTROL.owns_audio.return_value = False
        self.server.TUNING.status.return_value = dict(available=True, audio_owned=False)
        self.watch = AudioWatch(self.server, auto_recover=True, read_pcm=self.samples, sleep=Mock())
        self.watch.lease = Mock()

    def mpd(self, command):
        if command == 'pause 1':
            self.player['state'] = 'pause'
        return [list(self.player.items())]

    def ticks(self, samples):
        self.samples.side_effect = samples
        for _ in range(4):
            self.watch.tick()

    def stalled_input(self, start=0):
        return [(running(1), running(1, '124'), running(n, '125')) for n in range(start, start + 5)]

    def test_reads_alsa_fields_and_distinguishes_closed_from_unreadable(self):
        with TemporaryDirectory() as temp:
            status = Path(temp) / 'status'
            self.assertIsNone(pcm(status))
            status.write_text('closed\n')
            self.assertEqual(pcm(status), CLOSED)
            status.write_text('state: RUNNING\nowner_pid   : 123\nhw_ptr      : 42\nappl_ptr    : 19\n')
            self.assertEqual(pcm(status), running(42))

    def test_ignores_idle_playback(self):
        self.player['state'] = 'pause'
        self.ticks(self.stalled_input())
        self.samples.assert_not_called()
        self.assertIsNone(self.watch.report['fault'])

    def test_output_stall_takes_priority_over_capture_starvation(self):
        with self.assertLogs(level='WARNING'):
            self.ticks([(running(1), running(1, '124'), ('XRUN', '125', 0))] * 4)
        self.assertEqual(self.watch.report['fault'], 'dac_output_stalled')
        self.assertNotIn(call('SetMute', True), self.dsp.call.call_args_list)
        self.assertNotIn(call('pause 1'), self.server.mpd.call_args_list)

    def test_unreadable_pcm_does_not_trigger_a_fault(self):
        self.ticks([(None, running(1), running(n)) for n in range(4)])
        self.assertIsNone(self.watch.report['fault'])
        self.ticks([(running(1), running(1), None)] * 4)
        self.assertIsNone(self.watch.report['fault'])

    def test_stall_only_reports_when_recovery_is_off(self):
        self.watch.auto_recover = False
        with self.assertLogs(level='WARNING'):
            self.ticks(self.stalled_input())
        self.assertEqual(self.watch.report['fault'], 'tidal_input_stalled')
        self.assertEqual(self.watch.report['recovery'], 'operator_needed')
        self.watch.lease.acquire.assert_not_called()

    def test_rearms_once_muted_and_keeps_the_same_track_queue_and_preset(self):
        with self.assertLogs(level='INFO'):
            self.ticks(self.stalled_input() + [(CLOSED, running(1), running(5))])
        self.assertEqual(self.watch.report['recovery'], 'ready_to_resume')
        self.assertEqual(self.player, dict(state='pause', playlist='12', songid='3', elapsed='21.4'))
        self.assertEqual([entry for entry in self.dsp.call.call_args_list if entry.args[0].startswith('Set')],
                         [call('SetMute', True)])
        self.watch.lease.release.assert_called_once_with()
        self.watch.tick()
        self.player['state'] = 'play'
        with self.assertLogs(level='WARNING'):
            self.ticks(self.stalled_input(5))
        self.assertEqual(self.server.mpd.call_args_list.count(call('pause 1')), 1)
        self.values['GetState'] = 'Running'
        self.ticks([(running(n), running(n, '124'), running(n, '125')) for n in range(10, 14)])
        self.assertIsNone(self.watch.report['fault'])
        self.assertFalse(self.watch.attempted)

    def test_refuses_recovery_when_receiver_owns_audio(self):
        self.server.BLUETOOTH_CONTROL.owns_audio.return_value = True
        with self.assertLogs(level='WARNING'):
            self.ticks(self.stalled_input())
        self.watch.lease.acquire.assert_not_called()
        self.assertNotIn(call('pause 1'), self.server.mpd.call_args_list)

    def test_pause_requires_verified_mute(self):
        self.values['GetMute'] = False
        with self.assertLogs(level='WARNING'):
            self.ticks(self.stalled_input())
        self.assertEqual(self.watch.report['recovery'], 'failed')
        self.assertNotIn(call('pause 1'), self.server.mpd.call_args_list)

    def test_unreadable_device_after_pause_is_not_a_verified_release(self):
        with self.assertLogs(level='WARNING'):
            self.ticks(self.stalled_input() + [(None, running(1), running(5))])
        self.assertEqual(self.watch.report['recovery'], 'failed')
        self.assertNotIn(call('SetMute', False), self.dsp.call.call_args_list)


if __name__ == '__main__':
    unittest.main()
