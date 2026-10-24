import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml
import server


class ActiveSettings(unittest.TestCase):
    def config(self):
        return yaml.safe_load(Path(__file__).with_name('channel-dsp.yml').read_text(encoding='utf-8'))

    def values(self, config, output):
        return {item['name']: item['value'] for item in server.always_active_controls(config, output)}

    def effective(self, config, output):
        return [(copy.deepcopy(config['filters'][name]), bool(stage.get('bypassed')))
                for stage in config['pipeline'] if stage.get('type') == 'Filter'
                and output - 1 in stage.get('channels', []) for name in stage['names']]

    def test_shared_filters_become_independent_without_changing_siblings_or_eq(self):
        for output in range(1, 9):
            with self.subTest(output=output):
                config = self.config()
                server.polarity_parameters(config, output)['inverted'] = True
                before = copy.deepcopy(config)
                values = self.values(config, output)
                for item in server.always_active_controls(config, output):
                    values[item['name']] = -18 if item['parameter'] == 'gain' else (4000 if output >= 7 else 80) if item['label'] == 'High-pass' else 8000
                server.change_always_active(config, output, values)
                actual = server.always_active_controls(config, output)
                self.assertEqual([item['value'] for item in actual], list(values.values()))
                for other in range(1, 9):
                    if other != output:
                        self.assertEqual(self.effective(config, other), self.effective(before, other))
                self.assertEqual(server.polarity_parameters(config, output), server.polarity_parameters(before, output))
                self.assertEqual(server.eq_block(config, output, required=False), server.eq_block(before, output, required=False))
                # A second save reuses private stages; it does not accumulate gain/filter stages.
                stage_count, filter_count = len(config['pipeline']), len(config['filters'])
                again = self.values(config, output)
                gain = next(item for item in actual if item['parameter'] == 'gain')
                again[gain['name']] = -21
                server.change_always_active(config, output, again)
                self.assertEqual(len(config['pipeline']), stage_count)
                self.assertEqual(len(config['filters']), filter_count)

    def test_output_without_attenuation_gets_independent_gain_stage(self):
        config = server.unmeasured_config(self.config())
        values = self.values(config, 1)
        self.assertEqual(values['__attenuation'], 0)
        before = self.effective(config, 2)
        values['__attenuation'] = -9
        server.change_always_active(config, 1, values)
        self.assertEqual(self.effective(config, 2), before)
        self.assertEqual([item['value'] for item in server.always_active_controls(config, 1) if item['parameter'] == 'gain'], [-9])

    def test_invalid_values_are_rejected_before_config_is_modified(self):
        config = self.config()
        original = copy.deepcopy(config)
        for name, value in [('sub_hp', 0), ('sub_hp', 500), ('sub_lp', 24000),
                            ('sub_lp', float('nan')), ('sub_hp', True), ('sub_hp', '50'),
                            ('sub_attenuation', 1), ('sub_attenuation', -61),
                            ('sub_attenuation', float('inf'))]:
            with self.subTest(name=name, value=value):
                values = self.values(config, 5)
                values[name] = value
                with self.assertRaises(ValueError):
                    server.change_always_active(config, 5, values)
                self.assertEqual(config, original)
        for values in ({}, {'output_5_polarity': 0}, {'exciter_ex30_1': 100}, None):
            with self.assertRaises(ValueError):
                server.change_always_active(config, 5, values)
            self.assertEqual(config, original)

    def test_limits_follow_sample_rate(self):
        config = self.config()
        config['devices']['samplerate'] = 32000
        values = self.values(config, 7)
        values['tweeter_lp'] = 18000
        with self.assertRaises(ValueError):
            server.change_always_active(config, 7, values)

    def test_editor_does_not_include_measured_filters_or_polarity(self):
        config = self.config()
        controls = self.values(config, 1)
        self.assertEqual(set(controls), {'audition_hp', 'audition_lp', 'exciter_ex30_headroom'})
        server.eq_block(config, 1)['bypassed'] = True
        self.assertEqual(self.values(config, 1), controls)

    def test_save_validation_reload_and_failure_rollback(self):
        for outcome in ('success', 'reject', 'timeout'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                profile = Path(tmp) / 'channel-dsp.yml'
                config = self.config()
                profile.write_text(yaml.safe_dump(config), encoding='utf-8')
                previous = profile.read_bytes()
                loaded = copy.deepcopy(config)
                dsp = MagicMock()
                dsp.__enter__.return_value = dsp

                def call(command, *args):
                    nonlocal loaded
                    if command == 'GetConfigFilePath':
                        return str(profile)
                    if command == 'GetConfigJson':
                        return json.dumps(loaded)
                    if command == 'GetMute':
                        return False
                    if command == 'Reload' and outcome == 'success':
                        loaded = yaml.safe_load(profile.read_text(encoding='utf-8'))
                    if command == 'GetState':
                        return 'Running'

                dsp.call.side_effect = call
                values = self.values(config, 5)
                values['sub_lp'] = 450
                with patch.object(server, 'CHANNEL_PROFILE', profile), patch.object(server, 'DSP', return_value=dsp), \
                     patch.object(server.subprocess, 'run', return_value=MagicMock(returncode=1 if outcome == 'reject' else 0)) as validate, \
                     patch.object(server.time, 'sleep'):
                    if outcome == 'success':
                        server.set_channel_control(5, 'always-active', values)
                        dsp.call.assert_called_with('SetMute', False)
                        self.assertIn(450, self.values(loaded, 5).values())
                        self.assertEqual(self.values(loaded, 6), self.values(config, 6))
                    else:
                        with self.assertRaises(RuntimeError):
                            server.set_channel_control(5, 'always-active', values)
                        self.assertEqual(profile.read_bytes(), previous)
                        if outcome == 'reject':
                            self.assertNotIn(('SetMute', True), [item.args for item in dsp.call.call_args_list])
                        else:
                            self.assertIn(('SetMute', True), [item.args for item in dsp.call.call_args_list])
                            self.assertNotIn(('SetMute', False), [item.args for item in dsp.call.call_args_list])
                    self.assertIn('--check', validate.call_args.args[0])

    def test_api_routes_to_persistent_output_control(self):
        with patch.object(server, 'set_channel_control') as change, patch.object(server, 'current_status', return_value={}):
            server.action('channel-always-active', dict(output=5, values={'sub_lp': 450}))
        change.assert_called_once_with(5, 'always-active', {'sub_lp': 450})


if __name__ == '__main__':
    unittest.main()
