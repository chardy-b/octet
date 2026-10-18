import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import yaml
import server


class DriverPairs(unittest.TestCase):
    def original(self):
        return yaml.safe_load(Path(__file__).with_name('channel-dsp.yml').read_text(encoding='utf-8'))

    def effective(self, config, output):
        return [(name, config['filters'][name], bool(stage.get('bypassed')))
                for stage in config['pipeline'] if stage.get('type') == 'Filter'
                and output - 1 in stage.get('channels', []) for name in stage['names']]

    def assign(self, config):
        return server.assign_subs_and_tweeters(config, 25, 500, -30, 5000, 20000, -36)

    def test_new_pairs_are_stereo_filtered_attenuated_and_muted(self):
        old = self.original()
        before = json.dumps(old)
        config = self.assign(old)
        self.assertEqual(json.dumps(old), before)
        for output in range(1, 5):
            self.assertEqual(self.effective(config, output), self.effective(old, output))
        mappings = {entry['dest']: entry['sources'] for entry in config['mixers']['exciter_pairs']['mapping']}
        for output in range(5, 9):
            self.assertEqual(mappings[output - 1], [dict(channel=(output - 1) % 2, gain=0)])
            self.assertTrue(server.polarity_parameters(config, output)['mute'])
            self.assertIsNone(server.eq_block(config, output, required=False))
            role = 'sub' if output <= 6 else 'tweeter'
            names = [name for name, _, _ in self.effective(config, output)]
            self.assertIn(role + '_hp', names)
            self.assertIn(role + '_lp', names)
            self.assertIn(role + '_attenuation', names)
            self.assertNotIn('audition_hp', names)
            for suffix in ('hp', 'lp'):
                self.assertEqual(config['filters'][role + '_' + suffix]['parameters']['order'], 4)
        self.assertEqual(config['filters']['sub_hp']['parameters']['freq'], 25)
        self.assertEqual(config['filters']['sub_lp']['parameters']['freq'], 500)
        self.assertEqual(config['filters']['tweeter_hp']['parameters']['freq'], 5000)
        self.assertEqual(config['filters']['tweeter_lp']['parameters']['freq'], 20000)
        self.assertEqual(config['filters']['sub_attenuation']['parameters']['gain'], -30)
        self.assertEqual(config['filters']['tweeter_attenuation']['parameters']['gain'], -36)

    def test_new_output_controls_preserve_protection_and_siblings(self):
        config = self.assign(self.original())
        before = json.loads(json.dumps(config))
        server.change_channel_setting(config, 7, 'mute', False)
        expected = json.loads(json.dumps(before))
        expected['filters']['output_7_polarity']['parameters']['mute'] = False
        self.assertEqual(config, expected)
        server.change_channel_setting(config, 8, 'polarity', True)
        self.assertTrue(server.polarity_parameters(config, 8)['mute'])
        filters = json.dumps(config['filters'])
        server.change_channel_setting(config, None, 'all-eq', False)
        self.assertEqual(json.dumps(config['filters']), filters)
        again = self.assign(config)
        self.assertFalse(server.polarity_parameters(again, 7)['mute'])
        for output in range(5, 9):
            self.assertEqual(len([stage for stage in again['pipeline']
                                 if stage.get('description') == f'Output {output} polarity']), 1)

    def test_status_exposes_all_eight_roles_and_mute_states(self):
        config = self.assign(self.original())
        dsp = MagicMock()
        dsp.__enter__.return_value = dsp
        dsp.call.side_effect = lambda name: json.dumps(config) if name == 'GetConfigJson' else str(server.CHANNEL_PROFILE) if name == 'GetConfigFilePath' else -18 if name == 'GetVolume' else False
        with patch.object(server, 'DSP', return_value=dsp):
            status = server.dsp_status()
        for output in range(5, 9):
            channel = status['channels'][output - 1]
            self.assertTrue(channel['available'])
            self.assertTrue(channel['muted'])
            self.assertFalse(channel['eq_available'])
            self.assertEqual(channel['input'], 'Left' if output % 2 else 'Right')
            self.assertEqual(channel['role'], 'subwoofer' if output <= 6 else 'tweeter')


if __name__ == '__main__':
    unittest.main()
