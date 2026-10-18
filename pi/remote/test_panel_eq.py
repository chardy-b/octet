from pathlib import Path
import yaml
import unittest
from build_panel_eq import build, parse_export


LEFT = 'Preamp: -3.1 dB\nFilter 1: ON HPQ Fc 110 Hz Q 0.71\nFilter 2: ON PK Fc 969 Hz Gain -7.8 dB Q 3\n'
RIGHT = 'Preamp: -2.9 dB\nFilter 1: ON PK Fc 623 Hz Gain +3.0 dB Q 3\nFilter 2: ON LPQ Fc 2850 Hz Q 0.71\n'


class PanelEQ(unittest.TestCase):
    def test_each_model_is_filtered_after_split_on_its_stereo_pair(self):
        base = {'filters': {'guard': {'type': 'Gain'}}, 'pipeline': [{'type': 'Mixer', 'name': 'pairs'}]}
        config = build(base, LEFT, RIGHT, 'old_measurement_A', 'old_measurement_B')
        self.assertEqual(config['pipeline'][0], base['pipeline'][0])
        self.assertEqual(config['pipeline'][1]['channels'], [0, 1])
        self.assertEqual(config['pipeline'][2]['channels'], [2, 3])
        self.assertEqual(base['filters'], {'guard': {'type': 'Gain'}})
        self.assertEqual(config['filters']['exciter_ex30_headroom']['parameters']['gain'], -3.1)
        self.assertEqual(config['filters']['exciter_ex32_headroom']['parameters']['gain'], -2.9)
        self.assertEqual(config['filters']['exciter_ex30_headroom']['description'], 'old_measurement_A')
        self.assertEqual(config['filters']['exciter_ex30_2']['parameters'], {'type': 'Peaking', 'freq': 969., 'q': 3., 'gain': -7.8})
        self.assertEqual(config['filters']['exciter_ex32_2']['parameters'], {'type': 'Lowpass', 'freq': 2850., 'q': .71})

    def test_unidentified_model_has_no_fabricated_filters(self):
        config = build(dict(filters={}, pipeline=[]), ex30_text=LEFT)
        self.assertEqual(len(config['pipeline']), 1)
        self.assertEqual(config['pipeline'][0]['channels'], [0, 1])
        self.assertFalse(any(name.startswith('exciter_ex32_') for name in config['filters']))
        with self.assertRaises(ValueError):
            build(dict(filters={}, pipeline=[]))

    def test_saved_model_pairs_have_matching_eq_and_requested_gain(self):
        from server import eq_block
        config = yaml.safe_load(Path(__file__).with_name('channel-dsp.yml').read_text(encoding='utf-8'))
        for first, second, highpass, preamp, extra in ((1, 2, 110, -3.1, False), (3, 4, 120, -2.9, False)):
            self.assertEqual(eq_block(config, first)['names'], eq_block(config, second)['names'])
            for output in (first, second):
                eq = [config['filters'][name] for name in eq_block(config, output)['names']]
                self.assertEqual(eq[0]['parameters']['freq'], highpass)
                active = [name for stage in config['pipeline'] if stage.get('type') == 'Filter'
                          and output - 1 in stage.get('channels', []) for name in stage['names']]
                self.assertEqual('fixed_attenuation' in active, extra)
                headroom = [config['filters'][name]['parameters']['gain'] for name in active if name.endswith('_headroom')]
                self.assertEqual(headroom, [preamp])
                self.assertIn('audition_hp', active)
                self.assertIn('audition_lp', active)

    def test_unknown_or_disabled_exports_are_rejected(self):
        for line in ('Filter 1: OFF PK Fc 1000 Hz Gain +3.0 dB Q 1',
                     'Filter 1: ON PK Fc 1000 Hz Gain +30.0 dB Q 1',
                     'Filter 1: ON LPQ Fc 30000 Hz Q 1'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_export('Preamp: -3 dB\n' + line, 'test')


if __name__ == '__main__':
    unittest.main()
