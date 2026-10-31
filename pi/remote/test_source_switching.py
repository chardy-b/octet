"""Source switching retains immutable tuning and recovers failed activations."""
import copy
from contextlib import contextmanager, ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, MagicMock, patch

import yaml

import audio_sources
import server
from test_audio_sources import config, usb
from tuning_proxy import TuningProxy


class ListeningDSP:
    def __init__(self, path):
        self.path = str(path)
        self.document = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
        self.volume = -31.0
        self.muted = False
        self.state = 'Running'
        self.calls = []
        self.fail_next_reload = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def call(self, name, value=None):
        self.calls.append((name, value))
        if name == 'GetConfigFilePath':
            return self.path
        if name == 'GetConfigJson':
            return json.dumps(self.document)
        if name in ('ReadConfigJson', 'ReadConfig'):
            return value
        if name == 'GetState':
            return self.state
        if name == 'GetMute':
            return self.muted
        if name == 'GetVolume':
            return self.volume
        if name == 'SetConfigFilePath':
            self.path = value
        elif name == 'SetMute':
            self.muted = value
        elif name == 'SetVolume':
            self.volume = value
        elif name == 'Reload':
            if self.fail_next_reload:
                self.fail_next_reload = False
                self.state = 'Starting'
            else:
                self.document = yaml.safe_load(Path(self.path).read_text(encoding='utf-8'))
                self.state = 'Running'
        else:
            raise AssertionError('Unexpected DSP command: ' + name)


class SourceSwitching(unittest.TestCase):
    @contextmanager
    def system(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as patches:
            root = Path(directory)
            origin = root / 'saved-tuning.yml'
            working = root / 'listening'
            working.mkdir()
            baseline = config()
            baseline['title'] = 'Living room tuning'
            baseline['filters']['output_2_polarity']['parameters'].update(inverted=True, mute=True)
            origin.write_text(yaml.safe_dump(baseline, sort_keys=False), encoding='utf-8')
            tuning = dict(id='saved-tune', name='Living room tuning', kind='tuned',
                          config_path=str(origin), sha256=hashlib.sha256(origin.read_bytes()).hexdigest(),
                          chosen={'L': {'low': 470}},
                          verification={'passed': True})
            tuning_root = root / 'autotune'
            tuning_root.mkdir()
            (tuning_root / 'active.json').write_text(json.dumps(tuning), encoding='utf-8')
            dsp = ListeningDSP(origin)
            validation = MagicMock(returncode=0)
            mpd = patches.enter_context(patch.object(server, 'mpd'))
            patches.enter_context(patch.object(server, 'CHANNEL_PROFILE', working / 'channel-dsp.yml'))
            patches.enter_context(patch.object(server, 'DSP', return_value=dsp))
            patches.enter_context(patch.object(server, 'discover_sources', return_value=[audio_sources.TIDAL, usb()]))
            patches.enter_context(patch.object(server, 'TUNING', TuningProxy(tuning_root)))
            patches.enter_context(patch.object(server.TUNING, 'status', return_value={'audio_owned': False}))
            patches.enter_context(patch.object(server, 'player_queue', return_value=(
                {'state': 'pause', 'song': '-1'}, [], [], dict(manual={}, autoplay={}, source={}))))
            patches.enter_context(patch.object(server.subprocess, 'run', return_value=validation))
            patches.enter_context(patch.object(server.INPUT_LEVELS, 'release'))
            patches.enter_context(patch.object(server.time, 'sleep'))
            patches.enter_context(patch.object(server, 'ACTIVE_CAPTURE_CHANNELS', (0, 1)))
            # Every non-Tidal input runs from one file; its record names the profile.
            target = working / 'running.yml'
            yield origin, baseline, tuning, dsp, validation, mpd, working / 'running.json', target

    def assert_tuning_available(self, dsp, tuning, source):
        status = server.current_status()
        self.assertTrue(status['ready'])
        self.assertTrue(status['source_switching_available'])
        self.assertEqual(status['source'], source)
        self.assertEqual(status['profile'], tuning['name'])
        self.assertEqual(status['volume'], dsp.volume)
        self.assertEqual(status['muted'], dsp.muted)
        server.approved(dsp)
        details = server.dsp_status()
        self.assertTrue(details['auto_tuned'])
        self.assertEqual(details['profile'], tuning['name'])
        self.assertEqual(details['tuning'], tuning)

    def test_saved_tuning_switches_to_usb_and_back_without_changing_speaker_settings(self):
        with self.system() as (origin, baseline, tuning, dsp, validation, mpd, profiles, target):
            original_bytes = origin.read_bytes()
            self.assert_tuning_available(dsp, tuning, 'tidal')
            for source in (usb()['id'], 'tidal'):
                server.select_source(source)
                # Tidal runs the saved profile itself.
                self.assertEqual(dsp.path, str(origin if source == 'tidal' else target))
                self.assertEqual(origin.read_bytes(), original_bytes)
                self.assertEqual(audio_sources.source_config(dsp.document, audio_sources.TIDAL), baseline)
                self.assertEqual(dsp.document['filters'], baseline['filters'])
                self.assertEqual(dsp.volume, -31)
                self.assertTrue(dsp.muted)
                self.assert_tuning_available(dsp, tuning, source)
                self.assertEqual(json.loads(profiles.read_text())['profile'], str(origin))
            self.assertEqual(dsp.document, baseline)
            server.action('volume', {'db': -32})
            self.assertEqual(dsp.volume, -32)
            self.assertEqual(mpd.call_args_list, [call('pause 1'), call('pause 1')])
            self.assertNotIn(('SetMute', False), dsp.calls)

    def test_validation_failure_leaves_active_source_and_working_files_unchanged(self):
        for existing in (False, True):
            with self.subTest(existing_working_copy=existing), self.system() as (
                    origin, baseline, tuning, dsp, validation, mpd, profiles, target):
                if existing:
                    server.select_source(usb()['id'])
                original_bytes = origin.read_bytes()
                manifest = profiles
                before = (target.read_bytes(), manifest.read_bytes()) if existing else None
                previous_path = dsp.path
                previous_document = copy.deepcopy(dsp.document)
                dsp.calls.clear()
                mpd.reset_mock()
                validation.returncode = 1
                other = usb((2, 3), 'pair-2')
                with patch.object(server, 'discover_sources', return_value=[audio_sources.TIDAL, usb(), other]), \
                     self.assertRaisesRegex(RuntimeError, 'DSP rejected the source'):
                    server.select_source(other['id'] if existing else usb()['id'])
                self.assertEqual(dsp.path, previous_path)
                self.assertEqual(dsp.document, previous_document)
                self.assertEqual(origin.read_bytes(), original_bytes)
                if existing:
                    self.assertEqual((target.read_bytes(), manifest.read_bytes()), before)
                else:
                    self.assertFalse(target.exists() or manifest.exists())
                self.assertNotIn(('SetMute', True), dsp.calls)
                mpd.assert_not_called()
                self.assertEqual(list(target.parent.glob('*.candidate.yml')), [])

    def test_activation_failure_restores_previous_path_file_manifest_and_mutes(self):
        for existing in (False, True):
            with self.subTest(existing_working_copy=existing), self.system() as (
                    origin, baseline, tuning, dsp, validation, mpd, profiles, target):
                if existing:
                    server.select_source(usb()['id'])
                original_bytes = origin.read_bytes()
                manifest = profiles
                before = (target.read_bytes(), manifest.read_bytes()) if existing else None
                previous_path = dsp.path
                previous_document = copy.deepcopy(dsp.document)
                dsp.calls.clear()
                dsp.fail_next_reload = True
                with self.assertRaisesRegex(RuntimeError, 'previous source was restored and output is muted'):
                    server.select_source('tidal' if existing else usb()['id'])
                self.assertEqual(dsp.path, previous_path)
                self.assertEqual(dsp.document, previous_document)
                self.assertEqual(dsp.state, 'Running')
                self.assertTrue(dsp.muted)
                self.assertEqual(dsp.volume, -31)
                self.assertEqual(origin.read_bytes(), original_bytes)
                if existing:
                    self.assertEqual((target.read_bytes(), manifest.read_bytes()), before)
                else:
                    self.assertFalse(target.exists() or manifest.exists())
                    self.assertIn(('SetConfigFilePath', str(origin)), dsp.calls)
                self.assertNotIn(('SetMute', False), dsp.calls)
                self.assertEqual(list(target.parent.glob('*.candidate.yml')), [])
                self.assert_tuning_available(dsp, tuning, usb()['id'] if existing else 'tidal')

    def test_profile_switch_keeps_playing_and_limits_the_master(self):
        for source in ('tidal', usb()['id']):
            with self.subTest(source=source), self.system() as (
                    origin, baseline, tuning, dsp, validation, mpd, record, target):
                other = origin.with_name('other.yml')
                changed = copy.deepcopy(baseline)
                changed['title'] = 'Other tuning'
                changed['filters']['output_2_polarity']['parameters'].update(inverted=False)
                other.write_text(yaml.safe_dump(changed, sort_keys=False), encoding='utf-8')
                presets = origin.parent / 'autotune' / 'presets'
                presets.mkdir()
                (presets / 'other.json').write_text(json.dumps(dict(
                    id='other', name='Other tuning', kind='tuned', config_path=str(other))))
                if source != 'tidal':
                    server.select_source(source)
                dsp.muted, dsp.volume = False, -10.0
                dsp.calls.clear()
                mpd.reset_mock()
                with patch.object(server.BLUETOOTH_CONTROL, 'owns_audio', return_value=False), \
                     patch.object(server.AIRPLAY_CONTROL.baseline, 'exists', return_value=False):
                    status = server.action('auto-select', {'id': 'other'})
                self.assertFalse(dsp.muted)
                self.assertNotIn(('SetMute', True), dsp.calls)
                mpd.assert_not_called()
                self.assertEqual(dsp.volume, server.MAX_DB)
                self.assertEqual(dsp.document['title'], 'Other tuning')
                self.assertEqual(audio_sources.source_metadata(dsp.document)['id'], source)
                self.assertEqual(dsp.path, str(other if source == 'tidal' else target))
                self.assertEqual(server.profile_path(dsp.path), str(other))
                self.assertEqual(status['active']['id'], 'other')
                self.assertEqual(origin.read_text(encoding='utf-8'), yaml.safe_dump(baseline, sort_keys=False))

    def test_unapproved_configuration_cannot_switch_sources_or_unmute(self):
        with self.system() as (origin, baseline, tuning, dsp, validation, mpd, profiles, target):
            dsp.path = str(origin.with_name('unapproved.yml'))
            status = server.current_status()
            self.assertFalse(status['ready'])
            self.assertFalse(status['source_switching_available'])
            self.assertIn('saved speaker profile', status['source_switching_reason'])
            for operation in (lambda: server.select_source(usb()['id']),
                              lambda: server.action('mute', {'muted': False})):
                with self.assertRaises(RuntimeError):
                    operation()
            self.assertFalse(target.exists())
            self.assertNotIn(('SetMute', False), dsp.calls)
            mpd.assert_not_called()


if __name__ == '__main__':
    unittest.main()
