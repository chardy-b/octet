import copy
import unittest

import bluetooth_contract as bt


class ContractTests(unittest.TestCase):
    def test_cli_version_and_exact_options(self):
        help_text = '--profile-a2dp --pcm=NAME --volume=TYPE [auto|mixer|none|software] --single-audio'
        bt.check_cli('v4.3.1\n', help_text, player=True)
        bt.check_cli('v4.3.1\n', '--profile=NAME a2dp-sink', player=False)
        for version in ('', '4.3.1', '4.3.1\n', 'v4.3.1', ' v4.3.1\n', 'v4.3.1\n\n', 'v4.3.1\r\n', '4.3.10', '4.3.1-dev', '4.2.0', '5.0.0', '4.3.1\n5.0.0'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                bt.check_cli(version, help_text, player=True)
        for token in ('--profile-a2dp', '--pcm=NAME', '--volume=TYPE', '--single-audio', 'software'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                bt.check_cli('v4.3.1\n', help_text.replace(token, token + '-wrong'), player=True)
        with self.assertRaises(ValueError):
            bt.check_cli('v4.3.1\n', '--profile=NAME a2dp-source', player=False)

    def test_explicit_allowlisted_phone_and_no_route_fallback(self):
        phone = 'AA:BB:CC:DD:EE:01'
        self.assertEqual(bt.bridge_arguments(phone.lower(), [phone]), [
            'bluealsa-aplay', '--profile-a2dp', '--pcm=hw:Loopback,0,2',
            '--volume=software', '--single-audio', phone])
        for address in ('', '00:00:00:00:00:00', 'FF:FF:FF:FF:FF:FF', '*',
                        '--pcm=default', phone + ' '):
            with self.subTest(address=address), self.assertRaises(ValueError):
                bt.bridge_arguments(address, [address])
        for allowlist in ([], [phone.replace('01', '02')], phone, [phone, '*']):
            with self.subTest(allowlist=allowlist), self.assertRaises(ValueError):
                bt.bridge_arguments(phone, allowlist)

    def baseline(self):
        # Synthetic 129-filter fixture; not the installed approved Auto Tune file.
        return dict(devices=dict(samplerate=48000, chunksize=1024,
                    capture=dict(type='Alsa', device='hw:Loopback,1,0', channels=2, format='S32_LE'),
                    playback=dict(type='Alsa', device='hw:DAC,0', channels=8, format='S32_LE')),
                    filters={str(i): {'gain': -i} for i in range(129)},
                    pipeline=[{'type': 'Filter', 'names': ['0']}],
                    mixers={'outputs': {'mute': True, 'gain': -12}}, metadata={'approved': 'fixture'})

    def test_only_capture_and_clock_change_all_speaker_data_preserved(self):
        config = self.baseline()
        original = copy.deepcopy(config)
        for rate in (44100, 48000):
            for fmt in ('S16_LE', 'S32_LE'):
                candidate = bt.routed(config, rate=rate, sample_format=fmt, channels=2)
                self.assertEqual(candidate['devices']['capture'], dict(
                    type='Alsa', device='hw:Loopback,1,2', channels=2, format=fmt))
                self.assertEqual(candidate['devices']['capture_samplerate'], rate)
                expected = copy.deepcopy(candidate)
                expected['devices'] = original['devices']
                self.assertEqual(expected, original)
                self.assertEqual(candidate['devices']['playback'], original['devices']['playback'])
                candidate['filters']['0']['gain'] = 99
                self.assertEqual(config, original)

    def test_unsupported_pcm_and_output_routes_rejected(self):
        for rate, fmt, channels in ((96000, 'S32_LE', 2), (44100, 'FLOAT_LE', 2),
                                    (44100, 'S32_LE', 1), (48000.0, 'S16_LE', 2)):
            with self.assertRaises(ValueError):
                bt.routed(self.baseline(), rate=rate, sample_format=fmt, channels=channels)
        for section, key, value in (('playback', 'device', 'default'),
                                    ('playback', 'device', 'plughw:DAC,0'),
                                    ('playback', 'device', 'hw:Loopback,0,0')):
            config = self.baseline()
            config['devices'][section][key] = value
            with self.subTest(section=section, value=value), self.assertRaises(ValueError):
                bt.routed(config, rate=44100, sample_format='S16_LE', channels=2)

    def test_any_profile_input_and_output_count_is_accepted(self):
        # Profiles hold speaker settings; whatever input they were saved with is replaced.
        for section, key, value in (('capture', 'device', 'hw:Loopback,1,1'),
                                    ('capture', 'device', 'hw:CARD=iD14,DEV=0'),
                                    ('playback', 'channels', 2)):
            config = self.baseline()
            config['devices'][section][key] = value
            with self.subTest(section=section, value=value):
                candidate = bt.routed(config, rate=44100, sample_format='S16_LE', channels=2)
                self.assertEqual(candidate['devices']['capture']['device'], 'hw:Loopback,1,2')


if __name__ == '__main__':
    unittest.main()
