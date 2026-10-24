import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
import server
from tuning_proxy import TuningProxy


class AutoMode(unittest.TestCase):
    def test_measurement_proxy_rejects_path_injection(self):
        proxy = TuningProxy()
        with patch('tuning_proxy.urlopen') as opened:
            for identifier in ('../etc/passwd', 'x?query=1', 'a/b', ''):
                with self.assertRaises(ValueError):
                    proxy.measurements(identifier)
            opened.assert_not_called()

    def test_music_and_output_edits_cannot_interrupt_tones(self):
        with patch.object(server.TUNING, 'status', return_value={'audio_owned': True}), patch.object(server, 'DSP') as dsp:
            for action, body in [('play', {'id': '123'}), ('source', {'id': 'tidal'}),
                                 ('volume', {'db': -36}), ('mute', {'muted': False}),
                                 ('channel-always-active', {'output': 7, 'values': {}})]:
                with self.assertRaisesRegex(RuntimeError, 'owns'):
                    server.action(action, body)
            dsp.assert_not_called()

    def test_emergency_mute_stops_direct_test_playback(self):
        with patch.object(server.TUNING, 'status', return_value={'audio_owned': True}), \
             patch.object(server.TUNING, 'request', return_value={}) as request:
            server.action('mute', {'muted': True})
            request.assert_called_once_with('stop', {})

    def test_manual_tweeter_highpass_cannot_be_lowered(self):
        original = yaml.safe_load(Path(__file__).with_name('channel-dsp.yml').read_text())
        for output in (7, 8):
            doc = copy.deepcopy(original)
            controls = server.always_active_controls(doc, output)
            values = {c['name']: c['value'] for c in controls}
            for c in controls:
                if c['label'] == 'High-pass':
                    values[c['name']] = 100
            with self.assertRaisesRegex(ValueError, 'safety'):
                server.change_always_active(doc, output, values)
            self.assertEqual(doc, original)

    def test_offline_service_preserves_ownership_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            proxy = TuningProxy(tmp)
            (Path(tmp)/'current.json').write_text(json.dumps({'audio_owned': True}))
            with patch.object(proxy, 'request', side_effect=RuntimeError('offline')):
                self.assertTrue(proxy.status()['audio_owned'])


if __name__ == '__main__':
    unittest.main()
