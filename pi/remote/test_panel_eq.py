import unittest
from build_panel_eq import build, parse_export


LEFT = 'Preamp: -3.1 dB\nFilter 1: ON HPQ Fc 110 Hz Q 0.71\nFilter 2: ON PK Fc 969 Hz Gain -7.8 dB Q 3\n'
RIGHT = 'Preamp: -2.9 dB\nFilter 1: ON PK Fc 623 Hz Gain +3.0 dB Q 3\nFilter 2: ON LPQ Fc 2850 Hz Q 0.71\n'


class PanelEQ(unittest.TestCase):
    def test_each_panel_is_filtered_once_before_output_split(self):
        base = {'filters': {'guard': {'type': 'Gain'}}, 'pipeline': [{'type': 'Mixer', 'name': 'pairs'}]}
        config = build(base, LEFT, RIGHT)
        self.assertEqual(config['pipeline'][0]['channels'], [0])
        self.assertEqual(config['pipeline'][1]['channels'], [1])
        self.assertEqual(config['pipeline'][2], base['pipeline'][0])
        self.assertEqual(base['filters'], {'guard': {'type': 'Gain'}})
        self.assertEqual(config['filters']['panel_left_2']['parameters'], {'type': 'Peaking', 'freq': 969., 'q': 3., 'gain': -7.8})
        self.assertEqual(config['filters']['panel_right_2']['parameters'], {'type': 'Lowpass', 'freq': 2850., 'q': .71})

    def test_unknown_or_disabled_exports_are_rejected(self):
        for line in ('Filter 1: OFF PK Fc 1000 Hz Gain +3.0 dB Q 1',
                     'Filter 1: ON PK Fc 1000 Hz Gain +30.0 dB Q 1',
                     'Filter 1: ON LPQ Fc 30000 Hz Q 1'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_export('Preamp: -3 dB\n' + line, 'test')


if __name__ == '__main__':
    unittest.main()
