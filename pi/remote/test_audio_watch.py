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
        self.values = dict(GetConfigJson=json.dumps(self.config), GetState='Stalled')
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

    def mpd(self, *commands):
        replies = []
        for command in commands:
            if command in ('pause 1', 'pause 0'):
                self.player['state'] = 'pause' if command == 'pause 1' else 'play'
            replies.append(list(self.player.items()) if command == 'status' else [])
        return replies

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

    def test_resumes_same_track_and_queue_without_changing_dsp(self):
        with self.assertLogs(level='INFO'):
            self.ticks(self.stalled_input() + [(CLOSED, running(1), running(5))])
        self.assertEqual(self.watch.report['recovery'], 'retrying')
        self.assertEqual(self.watch.report['attempts'], 1)
        self.assertEqual(self.player, dict(state='play', playlist='12', songid='3', elapsed='21.4'))
        self.assertFalse(any(entry.args[0].startswith('Set') for entry in self.dsp.call.call_args_list))
        self.watch.lease.release.assert_called_once_with()
        self.values['GetState'] = 'Running'
        self.ticks([(running(n), running(n, '124'), running(n, '125')) for n in range(10, 14)])
        self.assertIsNone(self.watch.report['fault'])
        self.assertEqual(self.watch.attempts, 0)

    def test_limits_retries_to_three_and_ignores_brief_recovery(self):
        with self.assertLogs(level='INFO'):
            for start in (0, 10, 20):
                self.ticks(self.stalled_input(start) + [(CLOSED, running(1), running(start + 5))])
                self.values['GetState'] = 'Running'
                self.samples.side_effect = [(running(n), running(n, '124'), running(n, '125'))
                                            for n in (start + 6, start + 7)]
                self.watch.tick()
                self.watch.tick()
                self.values['GetState'] = 'Stalled'
            self.ticks(self.stalled_input(30))
            self.ticks(self.stalled_input(40))
        self.assertEqual(self.watch.report['recovery'], 'failed')
        self.assertEqual(self.watch.report['attempts'], 3)
        self.assertEqual(self.server.mpd.call_args_list.count(call('pause 1')), 3)
        self.assertEqual(self.server.mpd.call_args_list.count(call('pause 0', 'status')), 3)

    def test_refuses_recovery_when_receiver_owns_audio(self):
        self.server.BLUETOOTH_CONTROL.owns_audio.return_value = True
        with self.assertLogs(level='WARNING'):
            self.ticks(self.stalled_input())
        self.watch.lease.acquire.assert_not_called()
        self.assertNotIn(call('pause 1'), self.server.mpd.call_args_list)

    def test_does_not_resume_if_the_track_changed_while_paused(self):
        def replace_track(seconds):
            if seconds == .4:
                self.player['songid'] = '4'

        self.watch.sleep.side_effect = replace_track
        with self.assertLogs(level='WARNING'):
            self.ticks(self.stalled_input() + [(CLOSED, running(1), running(5))])
        self.watch.tick()
        self.assertEqual(self.watch.report['recovery'], 'failed')
        self.assertNotIn(call('pause 0', 'status'), self.server.mpd.call_args_list)

    def test_unreadable_device_after_pause_still_resumes_the_same_track(self):
        with self.assertLogs(level='WARNING') as logs:
            self.ticks(self.stalled_input() + [(None, running(1), running(5))])
        self.assertIn('Loopback release could not be verified', logs.output[-1])
        self.assertEqual(self.player['state'], 'play')
        self.assertEqual(self.watch.report['attempts'], 1)


if __name__ == '__main__':
    unittest.main()
