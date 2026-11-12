import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from audio_diagnostics import AudioDiagnostics, HISTORY, KEEP, pcm_details, camilla_file_tail


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recorder = AudioDiagnostics(self.root)
        self.dsp = MagicMock()
        self.dsp.return_value.__enter__.return_value.call.return_value = 1

    def observe(self, rate=48000):
        self.recorder.observe('tidal', 'Running', True,
                              {'devices': {'samplerate': rate}}, [('RUNNING', '42', 100)])

    def test_bounded_history_retains_route_changes_without_mutating_config(self):
        self.observe()
        self.observe(44100)
        self.assertEqual(self.recorder.history[-1]['devices']['samplerate'], 44100)
        self.assertNotEqual(self.recorder.history[0]['devices_sha256'],
                            self.recorder.history[1]['devices_sha256'])
        for _ in range(HISTORY + 10):
            self.observe()
        self.assertEqual(len(self.recorder.history), HISTORY)
        self.assertNotIn('devices', self.recorder.history[-1])
        self.assertFalse(self.recorder.root.exists())  # Healthy checks never write to disk.

    @patch('audio_diagnostics.subprocess.run')
    @patch('audio_diagnostics.pcm_details', return_value={'dac/status': 'XRUN'})
    def test_incidents_survive_recreation_and_are_retained_with_bounded_count(self, details, run):
        run.return_value.stdout = 'PB: Prepare playback after buffer underrun'
        run.return_value.returncode = 0
        run.return_value.stderr = ''
        self.observe()
        for _ in range(KEEP + 2):
            self.recorder.incident('dac_output_stalled', 'tidal', self.dsp)
        paths = list(self.recorder.root.glob('incident-*.json'))
        self.assertEqual(len(paths), KEEP)
        record = json.loads(paths[-1].read_text())
        self.assertEqual(record['pcm']['dac/status'], 'XRUN')
        self.assertEqual(record['history'][0]['source'], 'tidal')
        self.assertIn('GetRateAdjust', record['dsp'])
        commands = self.dsp.return_value.__enter__.return_value.call.call_args_list
        self.assertTrue(all(args.args[0].startswith('Get') for args in commands))
        AudioDiagnostics(self.root)  # Restart never clears previous incidents.
        self.assertTrue(paths[-1].exists())

    def test_write_failure_is_nonfatal_for_recovery(self):
        with patch.object(self.recorder, '_incident', side_effect=OSError('disk full')):
            with self.assertLogs(level='ERROR'):
                self.recorder.incident('dac_output_stalled', 'tidal', self.dsp)
        self.assertTrue(self.recorder.warned)

    def test_clock_metrics_are_throttled_and_unsupported_command_is_recorded(self):
        clock = MagicMock(return_value=10)
        recorder = AudioDiagnostics(self.root, monotonic=clock)
        dsp = MagicMock()
        dsp.call.side_effect = RuntimeError('unsupported')
        recorder.sample_metrics(dsp)
        recorder.sample_metrics(dsp)
        dsp.call.assert_called_once_with('GetCaptureRate')
        self.assertEqual(recorder.metrics['GetCaptureRate']['unavailable'], 'RuntimeError')
        clock.return_value = 21
        recorder.sample_metrics(dsp)
        self.assertEqual(dsp.call.call_count, 2)

    def test_raw_pcm_collects_all_sources_and_parameters(self):
        for sub in ('pcm0p/sub0', 'pcm1c/sub1', 'pcm0p/sub2'):
            directory = self.root / 'card10' / sub
            directory.mkdir(parents=True)
            (directory / 'status').write_text('closed')
            (directory / 'hw_params').write_text('rate: 48000')
            (directory / 'unrelated').write_text('ignore')
        result = pcm_details(self.root)
        self.assertEqual(len(result), 6)
        self.assertEqual(result['card10/pcm1c/sub1/hw_params'], 'rate: 48000')

    def test_actual_camilla_file_retains_underrun_evidence_without_disconnect_noise(self):
        log = self.root / 'camilla.log'
        log.write_text('PB: Prepare playback after buffer underrun\n'
                       'WARN [socketserver] Lost connection\nCapture: read overrun\n')
        text = camilla_file_tail(log)
        self.assertIn('buffer underrun', text)
        self.assertIn('read overrun', text)
        self.assertNotIn('socketserver', text)


if __name__ == '__main__':
    unittest.main()
