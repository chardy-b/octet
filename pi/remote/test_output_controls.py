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
        for side in ('ex30', 'ex32'):
            filters['exciter_' + side + '_headroom'] = {'type': 'Gain', 'parameters': {'gain': -3}}
            filters['exciter_' + side + '_1'] = {'type': 'Biquad', 'parameters': {'type': 'Peaking', 'freq': 1000, 'gain': -3, 'q': 1}}
        base = dict(devices={}, filters=filters, pipeline=[
            dict(type='Filter', channels=[0], names=['exciter_ex30_headroom', 'exciter_ex30_1']),
            dict(type='Filter', channels=[1], names=['exciter_ex32_headroom', 'exciter_ex32_1']),
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
        self.assertEqual(server.eq_block(config, 1)['names'], server.eq_block(config, 2)['names'])
        self.assertEqual(server.eq_block(config, 3)['names'], server.eq_block(config, 4)['names'])
        self.assertNotEqual(server.eq_block(config, 1)['names'], server.eq_block(config, 3)['names'])
        self.assertEqual(config['pipeline'][2]['channels'], [0, 1])
        self.assertEqual(config['pipeline'][3]['channels'], [2, 3])
        with self.assertRaises(RuntimeError):
            server.eq_block(config, 5)

    def test_ambiguous_legacy_left_right_filters_are_rejected(self):
        config = dict(devices={}, filters={
            'panel_left_headroom': dict(type='Gain'),
            'panel_right_headroom': dict(type='Gain')}, pipeline=[])
        with self.assertRaises(ValueError):
            server.channel_config(config)

    def test_retiring_wrong_measurements_preserves_guards_and_polarity(self):
        base = dict(devices={}, filters={
            'audition_hp': dict(type='BiquadCombo', parameters=dict(freq=500)),
            'audition_lp': dict(type='BiquadCombo', parameters=dict(freq=5000)),
            'fixed_attenuation': dict(type='Gain', parameters=dict(gain=-12)),
            'panel_left_headroom': dict(type='Gain', parameters=dict(gain=-3.1)),
            'panel_right_headroom': dict(type='Gain', parameters=dict(gain=-2.9)),
            'panel_left_1': dict(type='Biquad'), 'panel_right_1': dict(type='Biquad')},
            pipeline=[dict(type='Mixer', name='pairs'),
                      dict(type='Filter', channels=[0, 1, 2, 3], names=['audition_hp', 'audition_lp', 'fixed_attenuation']),
                      dict(type='Filter', channels=[0, 2], names=['panel_left_headroom', 'panel_left_1']),
                      dict(type='Filter', channels=[1, 3], names=['panel_right_headroom', 'panel_right_1'])])
        base = server.with_polarity_controls(base)
        server.polarity_parameters(base, 2)['inverted'] = True
        config = server.unmeasured_config(base)
        self.assertEqual(config['pipeline'][:2], base['pipeline'][:2])
        self.assertFalse(any(name.startswith('panel_') for name in config['filters']))
        self.assertTrue(server.polarity_parameters(config, 2)['inverted'])
        self.assertEqual(config['filters']['fixed_attenuation']['parameters']['gain'], -12)
        for output in range(1, 5):
            self.assertIsNone(server.eq_block(config, output, required=False))
            with self.assertRaises(RuntimeError):
                server.change_channel_setting(config, output, 'eq', True)
        with self.assertRaises(RuntimeError):
            server.change_channel_setting(config, None, 'all-eq', True)
        before = json.dumps(config)
        dsp = MagicMock()
        dsp.__enter__.return_value = dsp
        dsp.call.side_effect = lambda name: json.dumps(config) if name == 'GetConfigJson' else str(server.CHANNEL_PROFILE) if name == 'GetConfigFilePath' else -6 if name == 'GetVolume' else False
        with patch.object(server, 'DSP', return_value=dsp):
            status = server.dsp_status()
        for channel in status['channels'][:4]:
            self.assertTrue(channel['available'])
            self.assertFalse(channel['eq_available'])
            self.assertIsNone(channel['measurement'])
            self.assertEqual(channel['filters'], [])
        self.assertEqual(json.dumps(config), before)

    def test_default_installer_profile_has_no_measured_correction(self):
        config = server.channel_config(dict(devices={}, filters={'fixed_attenuation': dict(type='Gain', parameters=dict(gain=-12))},
                                            pipeline=[dict(type='Filter', channels=[0, 1, 2, 3], names=['fixed_attenuation'])]))
        for output in range(1, 5):
            self.assertIsNone(server.eq_block(config, output, required=False))
            self.assertFalse(server.polarity_parameters(config, output)['inverted'])
        self.assertNotIn('/opt/camilladsp/configs/exciter-panel-eq.yml', server.PROFILES)

    def test_one_verified_model_leaves_other_model_unmeasured(self):
        config = server.channel_config(dict(devices={}, filters={
            'exciter_ex30_headroom': dict(type='Gain', parameters=dict(gain=-3.1)),
            'exciter_ex30_1': dict(type='Biquad', parameters=dict(type='Highpass', freq=110, q=.71))},
            pipeline=[dict(type='Mixer', name='pairs')]))
        self.assertEqual(server.eq_block(config, 1)['names'], server.eq_block(config, 2)['names'])
        for output in (3, 4):
            self.assertIsNone(server.eq_block(config, output, required=False))
        self.assertEqual(config['pipeline'][1]['channels'], [0, 1])
        server.change_channel_setting(config, None, 'all-eq', False)
        self.assertTrue(server.eq_block(config, 1)['bypassed'])
        self.assertTrue(server.eq_block(config, 2)['bypassed'])

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

    def test_channel_mute_preserves_polarity_gain_eq_and_other_outputs(self):
        from pathlib import Path
        import yaml
        config = yaml.safe_load(Path(__file__).with_name('channel-dsp.yml').read_text(encoding='utf-8'))
        server.polarity_parameters(config, 2)['inverted'] = True
        server.eq_block(config, 2)['bypassed'] = True
        before = json.loads(json.dumps(config))
        server.change_channel_setting(config, 2, 'mute', True)
        expected = json.loads(json.dumps(before))
        expected['filters']['output_2_polarity']['parameters']['mute'] = True
        self.assertEqual(config, expected)
        server.change_channel_setting(config, 2, 'mute', False)
        self.assertEqual(config, before)
        with self.assertRaises(RuntimeError):
            server.change_channel_setting(config, 5, 'mute', True)

    def test_channel_mute_api_uses_output_control_not_master_mute(self):
        with patch.object(server, 'set_channel_control') as change, patch.object(server, 'current_status', return_value={}):
            server.action('channel-mute', dict(output=3, muted=True))
        change.assert_called_once_with(3, 'mute', True)
        for output, muted in ((True, True), (1, 'true'), (0, True), (9, True)):
            with self.subTest(output=output, muted=muted), self.assertRaises(ValueError):
                server.set_channel_control(output, 'mute', muted)

    def test_enable_all_preserves_polarity_and_protection(self):
        base = dict(devices={}, filters={}, pipeline=[dict(type='Mixer', name='pairs')])
        for side in ('ex30', 'ex32'):
            base['filters']['exciter_' + side + '_headroom'] = dict(type='Gain', parameters=dict(gain=-3))
            base['filters']['exciter_' + side + '_1'] = dict(type='Biquad', parameters=dict(type='Peaking', freq=1000, gain=-3, q=1))
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
