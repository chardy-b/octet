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


def loopback_fixture(root, state='PREPARED'):
    card = root / 'card10'
    card.mkdir(exist_ok=True)
    (root / 'Loopback').symlink_to(card, target_is_directory=True)
    (card / 'id').write_text('Loopback\n')
    (root / 'modules').write_text('10 snd_aloop\n')
    for endpoint, device, direction in [('pcm0p', 0, 'PLAYBACK'), ('pcm1c', 1, 'CAPTURE')]:
        path = card / endpoint / 'sub1'
        path.mkdir(parents=True)
        (path / 'info').write_text(f'card: 10\ndevice: {device}\nsubdevice: 1\nstream: {direction}\n')
        (path / 'status').write_text(f'state: {state}\nowner_pid: 123\nhw_ptr: 100\nappl_ptr: 100\n')
        (path / 'hw_params').write_text('format: S32_LE\nchannels: 2\nrate: 44100 (44100/1)\n')
    return card


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

    def test_airplay_discovery_and_untagged_capture_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual([s['id'] for s in audio_sources.discover_sources(root)], ['tidal'])
            loopback_fixture(root)
            self.assertEqual([s['id'] for s in audio_sources.discover_sources(root)], ['tidal', 'airplay'])
        raw = config()
        raw['devices']['capture']['device'] = 'hw:Loopback,1,1'
        self.assertEqual(audio_sources.source_metadata(raw)['id'], 'airplay')
        self.assertEqual(audio_sources.meter_channels(raw), (0, 1))
        with self.assertRaisesRegex(RuntimeError, 'metadata'):
            audio_sources.source_config(raw, audio_sources.TIDAL)

    def test_status_files_alone_do_not_advertise_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for endpoint in ('pcm0p', 'pcm1c'):
                path = root / 'Loopback' / endpoint / 'sub1'
                path.mkdir(parents=True)
                (path / 'status').write_text('closed')
            self.assertFalse(audio_sources.loopback_available(root))

    def test_probe_timeout_fails_closed(self):
        import subprocess
        with patch.object(audio_sources.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', 2)):
            self.assertFalse(audio_sources.probe_pcm(10, 1, True))

    def test_negotiation_requires_capture_format_rate_and_channels(self):
        alsa = MagicMock()
        for name in ('open', 'hw_params_malloc', 'hw_params_any', 'hw_params_set_access',
                     'hw_params_set_format', 'hw_params_set_channels', 'hw_params_set_rate', 'hw_params'):
            getattr(alsa, 'snd_pcm_' + name).return_value = 0
        alsa.snd_pcm_format_value.return_value = 10
        with patch.object(audio_sources.ctypes, 'CDLL', return_value=alsa), \
             patch.object(audio_sources, 'pcm_params_match', return_value=True):
            audio_sources.negotiate_pcm(10, 1, 1)
            self.assertEqual(alsa.snd_pcm_open.call_args.args[1:], (b'hw:10,1,1', 1, 1))
            self.assertEqual(alsa.snd_pcm_hw_params_set_channels.call_args.args[-1], 2)
            self.assertEqual(alsa.snd_pcm_hw_params_set_rate.call_args.args[-2:], (44100, 0))
            self.assertEqual(alsa.snd_pcm_hw_params_set_format.call_args.args[-1], 10)
            alsa.snd_pcm_hw_params_set_channels.return_value = -22
            with self.assertRaises(RuntimeError):
                audio_sources.negotiate_pcm(10, 1, 1)
            self.assertEqual(alsa.snd_pcm_close.call_count, 2)

    def test_loopback_identity_and_negotiated_capture_are_required(self):
        for file, value in [('id', 'Other'), ('../modules', '10 snd_usb_audio'),
                            ('pcm1c/sub1/info', 'card: 10\ndevice: 1\nsubdevice: 0\nstream: CAPTURE'),
                            ('pcm1c/sub1/hw_params', 'format: S32_LE\nchannels: 2\nrate: 48000'),
                            ('pcm0p/sub1/info', 'card: 9\ndevice: 0\nsubdevice: 1\nstream: CAPTURE')]:
            with self.subTest(file=file), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                card = loopback_fixture(root)
                (card / file).write_text(value)
                self.assertFalse(audio_sources.loopback_available(root))

    def test_closed_endpoints_require_both_negotiation_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            card = loopback_fixture(root)
            for endpoint in ('pcm0p', 'pcm1c'):
                (card / endpoint / 'sub1/status').write_text('closed')
            with patch.object(audio_sources, 'probe_pcm', side_effect=[True, False]) as probe:
                self.assertFalse(audio_sources.loopback_available(root))
                self.assertEqual([c.args for c in probe.call_args_list], [(10, 0, False), (10, 1, True)])
            with patch.object(audio_sources, 'probe_pcm', return_value=True):
                self.assertTrue(audio_sources.loopback_available(root))

    def test_airplay_rate_protection_and_round_trip(self):
        original = config()
        original['filters']['output_2_polarity']['parameters']['mute'] = True
        changed = audio_sources.source_config(original, audio_sources.AIRPLAY)
        self.assertEqual(changed['devices']['capture']['device'], 'hw:Loopback,1,1')
        self.assertEqual(changed['devices']['capture_samplerate'], 44100)
        self.assertEqual(changed['devices']['samplerate'], 48000)
        self.assertEqual(changed['devices']['resampler']['type'], 'AsyncSinc')
        self.assertEqual(changed['filters'], original['filters'])
        self.assertEqual(changed['devices']['playback'], original['devices']['playback'])
        self.assertEqual(audio_sources.source_metadata(changed)['id'], 'airplay')
        changed = audio_sources.source_config(changed, usb())
        self.assertEqual(audio_sources.source_config(changed, audio_sources.TIDAL), original)
        original['devices']['samplerate'] = 96000
        with self.assertRaisesRegex(ValueError, '48000'):
            audio_sources.source_config(original, audio_sources.AIRPLAY)

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

    def test_real_deferred_source_path_activates_only_after_live_pcm(self):
        from airplay_control import AirPlayController
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'channel-dsp.yml'
            original = config()
            profile.write_text(yaml.safe_dump(original))
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
                if name == 'Reload':
                    loaded = yaml.safe_load(profile.read_text())
                    loaded['devices']['capture']['labels'] = None

            dsp.call.side_effect = call
            with patch.object(server, 'CHANNEL_PROFILE', profile), \
                 patch.object(server, 'DSP', return_value=dsp), \
                 patch.object(server, 'approved'), \
                 patch.object(server, 'discover_sources', return_value=[audio_sources.AIRPLAY]), \
                 patch.object(server.subprocess, 'run', return_value=MagicMock(returncode=0)), \
                 patch.object(server, 'mpd') as mpd, \
                 patch.object(server.TUNING, 'status', return_value={'audio_owned': False}), \
                 patch('airplay_control.loopback_available', return_value=True), \
                 patch('airplay_control.receiver_running', return_value=True), \
                 patch('airplay_control.pcm_running', return_value=False) as live:
                server.ACTIVE_CAPTURE_CHANNELS = (1, 1)
                controller = AirPlayController(server)
                controller.event('start', now=0)
                controller.tick(now=1)
                self.assertNotIn(('SetMute', False), [c.args for c in dsp.call.call_args_list])
                live.return_value = True
                controller.tick(now=2)
                self.assertTrue(controller.active)
                self.assertEqual(server.ACTIVE_CAPTURE_CHANNELS, (0, 1))
                self.assertEqual([c.args for c in dsp.call.call_args_list][-2:],
                                 [('SetVolume', -18.0), ('SetMute', False)])
                self.assertEqual(loaded['filters'], original['filters'])
                mpd.assert_called_once_with('pause 1')

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


def baseline_config():
    """Representative synthetic Auto Tune baseline: 129 filters, distinctive DAC, output mute."""
    result = config()
    for i in range(129 - len(result['filters'])):
        name = f'baseline_only_{i}'
        result['filters'][name] = dict(type='Gain', parameters=dict(gain=-i / 100))
        result['pipeline'].append(dict(type='Filter', channels=[i % 8], names=[name]))
    result['filters']['output_2_polarity']['parameters']['mute'] = True
    result['devices']['playback']['device'] = 'hw:DistinctiveDAC8x'
    return result


class OutputEquivalence(unittest.TestCase):
    """The speaker side of an approved profile may never differ from its source route."""

    def test_baseline_round_trip_proves_output_subtree_for_every_source(self):
        original = baseline_config()
        self.assertEqual(len(original['filters']), 129)
        for source in (usb(), usb((0, 0), 'mono-1'), usb((1, 1), 'mono-2'),
                       audio_sources.AIRPLAY, audio_sources.TIDAL):
            with self.subTest(source=source['id']):
                routed = audio_sources.source_config(original, source)
                audio_sources.verify_output_equivalence(original, routed, source)
                audio_sources.verify_output_equivalence(original, routed, None)
                self.assertEqual(routed['pipeline'][1:] if source['id'] != 'tidal' else routed['pipeline'],
                                 original['pipeline'])
                self.assertEqual(audio_sources.source_config(routed, audio_sources.TIDAL), original)

    def test_repeated_switches_from_the_original_never_accumulate_input_stages(self):
        original = baseline_config()
        routed = original
        for source in (usb(), usb((1, 1), 'mono-2'), audio_sources.AIRPLAY, usb((0, 0), 'mono-1')):
            routed = audio_sources.source_config(routed, source)
            stages = [s for s in routed['pipeline'] if s.get('name') == audio_sources.SOURCE_MIXER]
            self.assertEqual(len(stages), 1)
            audio_sources.verify_output_equivalence(original, routed, source)

    def test_any_speaker_side_drift_is_rejected(self):
        original = baseline_config()
        source = usb((0, 0), 'mono-1')
        def gain(c): c['filters']['baseline_only_3']['parameters']['gain'] = 3
        def unmute(c): c['filters']['output_2_polarity']['parameters']['mute'] = False
        def dac(c): c['devices']['playback']['device'] = 'hw:Other'
        def drop(c): c['pipeline'].pop()
        def reorder(c): c['pipeline'][1], c['pipeline'][2] = c['pipeline'][2], c['pipeline'][1]
        def extra_filter(c): c['filters']['extra'] = dict(type='Gain', parameters=dict(gain=0))
        def extra_mixer(c): c['mixers']['sneaky'] = copy.deepcopy(c['mixers']['exciter_pairs'])
        def mixer_map(c): c['mixers']['exciter_pairs']['mapping'][0]['sources'][0]['channel'] = 1
        def limit(c): c['devices']['volume_limit'] = 10.0
        def chunk(c): c['devices']['chunksize'] = 4096
        def title(c): c['title'] = 'other'
        def top_level(c): c['extra'] = True
        def pipeline_extra(c): c['pipeline'].append(dict(type='Mixer', name='exciter_pairs'))
        for mutate in (gain, unmute, dac, drop, reorder, extra_filter, extra_mixer, mixer_map, limit,
                       chunk, title, top_level, pipeline_extra):
            with self.subTest(mutate=mutate.__name__):
                candidate = audio_sources.source_config(original, source)
                mutate(candidate)
                for checked in (source, None):
                    with self.assertRaises(RuntimeError):
                        audio_sources.verify_output_equivalence(original, candidate, checked)

    def test_source_stage_must_be_single_first_and_truthful(self):
        original = baseline_config()
        source = usb((0, 0), 'mono-1')
        def duplicate(c): c['pipeline'].insert(1, dict(type='Mixer', name=audio_sources.SOURCE_MIXER))
        def not_first(c): c['pipeline'].append(c['pipeline'].pop(0))
        def wrong_map(c): c['mixers'][audio_sources.SOURCE_MIXER]['mapping'][0]['sources'][0]['channel'] = 5
        def wrong_capture(c): c['devices']['capture']['device'] = 'hw:CARD=Other,DEV=0'
        def false_restore(c):
            metadata = audio_sources.source_metadata(c)
            metadata['tidal_capture']['device'] = 'hw:CARD=Other,DEV=0'
            c['mixers'][audio_sources.SOURCE_MIXER]['description'] = (
                audio_sources.SOURCE_DESCRIPTION + json.dumps(metadata, separators=(',', ':')))
        for mutate in (duplicate, not_first, wrong_map, wrong_capture, false_restore):
            with self.subTest(mutate=mutate.__name__):
                candidate = audio_sources.source_config(original, source)
                mutate(candidate)
                # Without the requested source a remapped input is merely a different valid input.
                for checked in ((source,) if mutate is wrong_map else (source, None)):
                    with self.assertRaises(RuntimeError):
                        audio_sources.verify_output_equivalence(original, candidate, checked)

    def test_loaded_defaults_are_tolerated_only_on_input_side(self):
        original = baseline_config()
        source = usb((0, 0), 'mono-1')
        loaded = audio_sources.source_config(original, source)
        loaded['devices']['capture']['labels'] = None
        loaded['mixers'][audio_sources.SOURCE_MIXER]['mapping'][0]['sources'][0]['inverted'] = False
        audio_sources.verify_output_equivalence(original, loaded, source)
        loaded['filters']['baseline_only_1']['parameters']['extra_default'] = None
        with self.assertRaises(RuntimeError):
            audio_sources.verify_output_equivalence(original, loaded, source)


class StrictSourceMetadata(unittest.TestCase):
    def routed(self):
        return audio_sources.source_config(baseline_config(), usb((0, 0), 'mono-1'))

    def rewrite(self, config, text):
        config['mixers'][audio_sources.SOURCE_MIXER]['description'] = audio_sources.SOURCE_DESCRIPTION + text

    def test_malformed_or_ambiguous_metadata_is_rejected_before_any_mutation(self):
        good = audio_sources.source_metadata(self.routed())
        def dump(**changes):
            value = copy.deepcopy(good)
            for key, item in changes.items():
                if item is KeyError:
                    del value[key]
                else:
                    value[key] = item
            return json.dumps(value, separators=(',', ':'))
        capture = good['tidal_capture']
        clock = good['tidal_clock']
        bad = {
            'not json': '{',
            'not an object': '[1]',
            'duplicate id': '{"id":"airplay",' + json.dumps(good, separators=(',', ':'))[1:],
            'unknown id': dump(id='../../etc'),
            'unexpected tidal id': dump(id='tidal'),
            'bad label': dump(label=7),
            'missing capture': dump(tidal_capture=KeyError),
            'missing clock': dump(tidal_clock=KeyError),
            'extra key': dump(extra=1),
            'capture not tidal loopback': dump(tidal_capture=dict(capture, device='hw:CARD=X,DEV=0')),
            'capture not stereo': dump(tidal_capture=dict(capture, channels=10)),
            'clock missing key': dump(tidal_clock={k: v for k, v in clock.items() if k != 'resampler'}),
            'clock unknown key': dump(tidal_clock=dict(clock, surprise=dict(present=False, value=None))),
            'clock bad present': dump(tidal_clock=dict(clock, resampler=dict(present='yes', value=None))),
            'clock extra field': dump(tidal_clock=dict(clock, resampler=dict(present=False, value=None, x=1))),
        }
        for label, text in bad.items():
            with self.subTest(label=label):
                config = self.routed()
                self.rewrite(config, text)
                with self.assertRaises(RuntimeError):
                    audio_sources.source_metadata(config)
                with self.assertRaises(RuntimeError):
                    audio_sources.source_config(config, audio_sources.TIDAL)

    def test_ambiguous_topology_is_rejected(self):
        def two_stages(c): c['pipeline'].insert(1, dict(type='Mixer', name=audio_sources.SOURCE_MIXER))
        def no_definition(c): del c['mixers'][audio_sources.SOURCE_MIXER]
        def stage_gone(c): c['pipeline'].pop(0)
        def late_stage(c): c['pipeline'].append(c['pipeline'].pop(0))
        def wrong_shape(c): c['mixers'][audio_sources.SOURCE_MIXER]['channels']['out'] = 8
        def wrong_gain(c): c['mixers'][audio_sources.SOURCE_MIXER]['mapping'][0]['sources'][0]['gain'] = 20
        def channel_range(c): c['mixers'][audio_sources.SOURCE_MIXER]['mapping'][0]['sources'][0]['channel'] = 99
        def prefix(c): c['mixers'][audio_sources.SOURCE_MIXER]['description'] = 'Other: {}'
        def device(c): c['devices']['capture']['device'] = 'hw:Loopback,1,0'
        for mutate in (two_stages, no_definition, stage_gone, late_stage, wrong_shape, wrong_gain,
                       channel_range, prefix, device):
            with self.subTest(mutate=mutate.__name__):
                config = self.routed()
                mutate(config)
                with self.assertRaises(RuntimeError):
                    audio_sources.source_metadata(config)

    def test_parsed_defaults_in_the_loaded_mixer_are_tolerated_but_audible_changes_are_not(self):
        config = self.routed()
        for row in config['mixers'][audio_sources.SOURCE_MIXER]['mapping']:
            row['mute'] = None
            row['sources'][0].update(inverted=False, mute=False, scale=None, gain=0.0)
        self.assertEqual(audio_sources.source_metadata(config)['id'], 'usb:iD14:mono-1')
        for change in (dict(mute=True), dict(extra=1)):
            broken = copy.deepcopy(config)
            broken['mixers'][audio_sources.SOURCE_MIXER]['mapping'][0].update(change)
            with self.assertRaises(RuntimeError):
                audio_sources.source_metadata(broken)

    def test_unmanaged_original_is_not_a_tidal_baseline(self):
        for mutate in (lambda c: c['devices']['capture'].update(channels=10),
                       lambda c: c['devices']['capture'].update(device='hw:CARD=Other,DEV=0'),
                       lambda c: c['devices'].update(samplerate=44100),
                       lambda c: c['devices']['playback'].update(device='hw:Loopback,0,0'),
                       lambda c: c['devices']['playback'].update(channels=2)):
            original = baseline_config()
            mutate(original)
            with self.assertRaises(RuntimeError):
                audio_sources.require_tidal_baseline(original)
        audio_sources.require_tidal_baseline(baseline_config())
        with self.assertRaises(RuntimeError):
            audio_sources.require_tidal_baseline(self.routed())

    def test_source_description_is_validated(self):
        for label, change in dict(device=dict(device='/dev/null'), channels=dict(channels=0),
                                  mapping=dict(mapping=[0, 99]), rates=dict(rates=[])).items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    audio_sources.validate_source(dict(usb(), **change))
        audio_sources.validate_source(usb())
        audio_sources.validate_source(audio_sources.AIRPLAY)
        audio_sources.validate_source(audio_sources.TIDAL)


if __name__ == '__main__':
    unittest.main()
