import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml
import audio_sources
import server


def config():
    return yaml.safe_load(Path(__file__).with_name('channel-dsp.yml').read_text(encoding='utf-8'))


def usb(mapping=(0, 1), key='pair-1'):
    return dict(id='usb:iD14:' + key, label='Audient iD14', device='hw:CARD=iD14,DEV=0',
                channels=10, mapping=list(mapping), rates=[44100, 48000], available=True)


class AudioSources(unittest.TestCase):
    def test_discovers_capture_only_and_distinguishes_analogue_from_optical(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, name, stream in [
                (0, 'hdmi', 'Playback:\n Altset 1\n Format: S32_LE\n Channels: 8\n Rates: 48000\n'),
                (3, 'iD14', 'Audient Audient iD14 at usb-xhci-hcd.1-1 : USB Audio\nPlayback:\n Altset 1\n Format: S32_LE\n Channels: 4\n Rates: 48000\nCapture:\n Altset 1\n Format: S32_LE\n Channels: 10\n Rates: 44100, 48000, 96000\n'),
                (4, 'OtherUSB', 'USB Input at usb-1 : USB Audio\nCapture:\n Altset 1\n Format: S32_LE\n Channels: 4\n Rates: 48000\n'),
                (10, 'Loopback', '')]:
                card = root / f'card{number}'
                card.mkdir()
                (card / 'id').write_text(name)
                if stream:
                    (card / 'stream0').write_text(stream)
            sources = audio_sources.discover_sources(root)
        ids = [item['id'] for item in sources]
        self.assertEqual(ids[:4], ['tidal', 'usb:iD14:pair-1', 'usb:iD14:mono-1', 'usb:iD14:mono-2'])
        self.assertNotIn('usb:iD14:pair-3', ids)
        self.assertIn('usb:OtherUSB:pair-3', ids)
        self.assertEqual(sources[1]['channels'], 10)
        self.assertIn('Audient iD14', sources[1]['label'])

    def test_mono_duplicates_to_stereo_without_changing_any_speaker_setting(self):
        original = config()
        original['filters']['output_2_polarity']['parameters']['inverted'] = True
        for channel in (0, 1):
            with self.subTest(channel=channel):
                changed = audio_sources.source_config(original, usb((channel, channel), f'mono-{channel+1}'))
                self.assertEqual(changed['pipeline'][1:], original['pipeline'])
                self.assertEqual(changed['filters'], original['filters'])
                self.assertEqual(changed['devices']['playback'], original['devices']['playback'])
                mixer = changed['mixers'][audio_sources.SOURCE_MIXER]
                self.assertEqual([row['sources'][0]['channel'] for row in mixer['mapping']], [channel, channel])
                self.assertTrue(changed['devices']['enable_rate_adjust'])
                self.assertEqual(changed['devices']['resampler']['type'], 'AsyncSinc')
                self.assertEqual(changed['devices']['capture']['channels'], 10)
                self.assertEqual(audio_sources.source_config(changed, audio_sources.TIDAL), original)

    def test_many_switches_restore_tidal_clock_and_do_not_accumulate_mixers(self):
        original = config()
        original['devices']['enable_rate_adjust'] = None
        original['devices']['resampler'] = None
        changed = original
        for _ in range(3):
            changed = audio_sources.source_config(changed, usb())
            changed = audio_sources.source_config(changed, usb((1, 1), 'mono-2'))
        self.assertEqual(len(changed['pipeline']), len(original['pipeline']) + 1)
        # DSP edits made while listening to an input survive switching back.
        changed['filters']['output_2_polarity']['parameters']['mute'] = True
        restored = audio_sources.source_config(changed, audio_sources.TIDAL)
        original['filters']['output_2_polarity']['parameters']['mute'] = True
        self.assertEqual(restored, original)

    def test_invalid_rate_unknown_capture_and_unplugged_id_are_rejected(self):
        before = config()
        source = usb()
        source['rates'] = [96000]
        with self.assertRaises(ValueError):
            audio_sources.source_config(before, source)
        other = copy.deepcopy(before)
        other['devices']['capture']['device'] = 'hw:unknown'
        with self.assertRaises(RuntimeError):
            audio_sources.source_config(other, usb())
        with patch.object(server, 'discover_sources', return_value=[audio_sources.TIDAL]), patch.object(server, 'DSP') as dsp:
            for value in ('usb:unplugged:mono-1', '../123', 'tidal\nplay', None):
                with self.assertRaises(ValueError):
                    server.select_source(value)
            dsp.assert_not_called()

    def test_switch_persists_verified_config_and_rolls_back_failures_muted(self):
        for outcome in ('success', 'reject', 'timeout', 'pause-failed'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                profile = Path(directory) / 'channel-dsp.yml'
                original = config()
                profile.write_text(yaml.safe_dump(original), encoding='utf-8')
                before = profile.read_bytes()
                loaded = copy.deepcopy(original)
                dsp = MagicMock()
                dsp.__enter__.return_value = dsp

                def call(name, *args):
                    nonlocal loaded
                    if name == 'GetConfigFilePath':
                        return str(profile)
                    if name == 'GetConfigJson':
                        return json.dumps(loaded)
                    if name == 'GetState':
                        return 'Running'
                    if name == 'Reload' and outcome == 'success':
                        loaded = yaml.safe_load(profile.read_text())
                        loaded['devices']['capture']['labels'] = None  # CamillaDSP adds defaults.

                dsp.call.side_effect = call
                with patch.object(server, 'CHANNEL_PROFILE', profile), patch.object(server, 'DSP', return_value=dsp), \
                     patch.object(server, 'discover_sources', return_value=[audio_sources.TIDAL, usb()]), \
                     patch.object(server.subprocess, 'run', return_value=MagicMock(returncode=int(outcome == 'reject'))), \
                     patch.object(server, 'mpd', side_effect=RuntimeError('offline') if outcome == 'pause-failed' else None) as mpd, \
                     patch.object(server.time, 'sleep'):
                    if outcome == 'success':
                        server.select_source(usb()['id'])
                        self.assertEqual(audio_sources.source_metadata(loaded)['id'], usb()['id'])
                        self.assertEqual(loaded['filters'], original['filters'])
                        mpd.assert_called_once_with('pause 1')
                    else:
                        with self.assertRaises(RuntimeError):
                            server.select_source(usb()['id'])
                        self.assertEqual(profile.read_bytes(), before)
                    calls = [item.args for item in dsp.call.call_args_list]
                    self.assertNotIn(('SetMute', False), calls)
                    self.assertNotIn('SetVolume', [item[0] for item in calls])
                    if outcome == 'reject':
                        self.assertNotIn(('SetMute', True), calls)
                        mpd.assert_not_called()
                    else:
                        self.assertIn(('SetMute', True), calls)

    def test_tidal_transport_does_not_play_over_selected_input(self):
        changed = audio_sources.source_config(config(), usb())
        dsp = MagicMock()
        dsp.__enter__.return_value = dsp
        dsp.call.side_effect = lambda name, *args: str(server.CHANNEL_PROFILE) if name == 'GetConfigFilePath' else json.dumps(changed) if name == 'GetConfigJson' else None
        with patch.object(server, 'DSP', return_value=dsp), patch.object(server, 'mpd') as mpd:
            for action in ('resume', 'previous', 'next', 'position', 'seek'):
                with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, 'Select Tidal'):
                    server.action(action, dict(position=0, seconds=1))
            mpd.assert_not_called()

    def test_visualizer_uses_selected_mono_input_instead_of_other_live_mic(self):
        changed = audio_sources.source_config(config(), usb((1, 1), 'mono-2'))
        channels = audio_sources.meter_channels(changed)
        self.assertEqual(channels, (1, 1))
        dsp = MagicMock()
        dsp.call.side_effect = lambda name, *args: 'Running' if name == 'GetState' else dict(capture_rms=[-20, -90], capture_peak=[-10, -70])
        with patch.object(server, 'ACTIVE_CAPTURE_CHANNELS', channels):
            frame = server.visualizer_frame(dsp)
        self.assertEqual(frame['rms'], [-90, -90])
        self.assertEqual(frame['peak'], [-70, -70])


if __name__ == '__main__':
    unittest.main()
