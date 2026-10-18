import json
import unittest
from unittest.mock import MagicMock, patch
import server


class OutputControls(unittest.TestCase):
    def dsp(self, volume=-18):
        mock = MagicMock()
        mock.__enter__.return_value = mock
        mock.call.side_effect = lambda name, *args: server.PROFILE if name == 'GetConfigFilePath' else volume if name == 'GetVolume' else None
        return mock

    def test_unlock_does_not_raise_volume(self):
        dsp = self.dsp()
        with patch.object(server, 'LEVELS_UNLOCKED', False), patch.object(server, 'DSP', return_value=dsp), \
             patch.object(server, 'current_status', return_value={}):
            server.action('unlock', {'enabled': True})
            self.assertEqual(server.volume_ceiling(), 0)
            self.assertNotIn('SetVolume', [call.args[0] for call in dsp.call.call_args_list])
            server.action('volume', {'db': -6})
            dsp.call.assert_called_with('SetVolume', -6)
            with self.assertRaises(ValueError):
                server.action('volume', {'db': 1})

    def test_relocking_clamps_an_already_high_level(self):
        dsp = self.dsp(-6)
        with patch.object(server, 'LEVELS_UNLOCKED', True), patch.object(server, 'DSP', return_value=dsp), \
             patch.object(server, 'current_status', return_value={}):
            server.action('unlock', {'enabled': False})
            self.assertEqual(server.volume_ceiling(), -18)
            self.assertIn(('SetVolume', -18), [call.args for call in dsp.call.call_args_list])

    def test_one_output_can_be_bypassed_without_its_partner_or_guards(self):
        filters = {'guard': {'type': 'BiquadCombo'}}
        for side in ('left', 'right'):
            filters['panel_' + side + '_headroom'] = {'type': 'Gain', 'parameters': {'gain': -3}}
            filters['panel_' + side + '_1'] = {'type': 'Biquad', 'parameters': {'type': 'Peaking', 'freq': 1000, 'gain': -3, 'q': 1}}
        base = dict(devices={}, filters=filters, pipeline=[
            dict(type='Filter', channels=[0], names=['panel_left_headroom', 'panel_left_1']),
            dict(type='Filter', channels=[1], names=['panel_right_headroom', 'panel_right_1']),
            dict(type='Mixer', name='pairs'), dict(type='Filter', channels=[0, 1, 2, 3], names=['guard'])])
        before = json.dumps(base)
        config = server.channel_config(base)
        self.assertEqual(json.dumps(base), before)
        self.assertEqual(config['pipeline'][0]['type'], 'Mixer')
        guard_before = dict(config['pipeline'][1])
        server.eq_block(config, 1)['bypassed'] = True
        self.assertTrue(server.eq_block(config, 1)['bypassed'])
        for output in (2, 3, 4):
            self.assertFalse(server.eq_block(config, output)['bypassed'])
        self.assertEqual(config['pipeline'][1], guard_before)
        self.assertEqual(server.eq_block(config, 1)['names'], server.eq_block(config, 3)['names'])
        self.assertEqual(server.eq_block(config, 2)['names'], server.eq_block(config, 4)['names'])
        with self.assertRaises(RuntimeError):
            server.eq_block(config, 5)

    def test_polarity_changes_only_one_sign_with_no_gain_or_eq_changes(self):
        base = dict(filters={'headroom': {'type': 'Gain', 'parameters': {'gain': -12}}},
                    pipeline=[dict(type='Filter', channels=[0, 1, 2, 3], names=['headroom'])])
        config = server.with_polarity_controls(base)
        before = json.dumps(base)
        params = server.polarity_parameters(config, 1)
        params['inverted'] = True
        self.assertEqual(params['gain'], 0)
        self.assertEqual(json.dumps(base), before)
        for output in (2, 3, 4):
            self.assertFalse(server.polarity_parameters(config, output)['inverted'])
        self.assertEqual(config['filters']['headroom'], base['filters']['headroom'])
        twice = server.with_polarity_controls(config)
        self.assertEqual(twice, config)
        with self.assertRaises(RuntimeError):
            server.polarity_parameters(config, 5)

    def test_enable_all_preserves_polarity_and_protection(self):
        base = dict(devices={}, filters={}, pipeline=[dict(type='Mixer', name='pairs')])
        for side in ('left', 'right'):
            base['filters']['panel_' + side + '_headroom'] = dict(type='Gain', parameters=dict(gain=-3))
            base['filters']['panel_' + side + '_1'] = dict(type='Biquad', parameters=dict(type='Peaking', freq=1000, gain=-3, q=1))
        config = server.channel_config(base)
        server.polarity_parameters(config, 2)['inverted'] = True
        for channel in (1, 3):
            server.eq_block(config, channel)['bypassed'] = True
        filters_before = json.dumps(config['filters'])
        server.change_channel_setting(config, None, 'all-eq', True)
        self.assertTrue(all(not server.eq_block(config, channel)['bypassed'] for channel in range(1, 5)))
        self.assertEqual(json.dumps(config['filters']), filters_before)
        self.assertTrue(server.polarity_parameters(config, 2)['inverted'])


if __name__ == '__main__':
    unittest.main()
