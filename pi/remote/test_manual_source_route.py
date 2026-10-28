"""Manual source selection on an approved Auto Tune baseline (WIL-250).

Real approval/digest/journal files in a scratch directory; the CamillaDSP
WebSocket, MPD and ALSA are simulated. Nothing here proves physical playback.
"""
import copy
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

import airplay_baseline
import audio_sources
import server
from airplay_control import AirPlayController
from test_audio_sources import baseline_config, config, usb
from tuning_proxy import TuningProxy

MONO_1 = usb((0, 0), 'mono-1')
MONO_2 = usb((1, 1), 'mono-2')
PAIR = usb()
AIRPLAY = audio_sources.AIRPLAY


class RouteFixture(unittest.TestCase):
    """An approved 129-filter baseline loaded in a simulated CamillaDSP."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.storage = self.root / 'dsp'
        self.storage.mkdir()
        tuning_root = self.root / 'autotune'
        (tuning_root / 'baselines').mkdir(parents=True)
        self.original = tuning_root / 'baselines' / 'before-20261026-041307-327269.yml'
        # Distinctive protections, gains, output mutes and DAC; not the 37-filter preset.
        self.config = baseline_config()
        self.original.write_text('# Immutable Auto Tune baseline\n' + yaml.safe_dump(self.config))
        self.original_bytes = self.original.read_bytes()
        self.sha = airplay_baseline.digest(self.original_bytes)
        (tuning_root / 'active.json').write_text(json.dumps(dict(
            config_path=str(self.original), sha256=self.sha, name='Auto Tune 2026-10-26')))
        self.lock_path = tuning_root / 'audio.lock'
        self.lock_path.write_bytes(b'lock sentinel')
        self.lock_path.chmod(0o640)
        self.tuning = TuningProxy(tuning_root)
        self.owned = False
        self.path = str(self.original)
        self.loaded = copy.deepcopy(self.config)
        self.muted = False
        self.volume = -31.0
        self.calls = []
        self.reload_failure = False
        self.dsp = MagicMock()
        self.dsp.__enter__.return_value = self.dsp
        self.dsp.call.side_effect = self.call
        self.sources = [audio_sources.TIDAL, audio_sources.AIRPLAY, MONO_1, MONO_2, PAIR]
        self.patches = [
            patch.object(airplay_baseline, 'ROOT', self.storage),
            patch.object(server, 'DSP', return_value=self.dsp),
            patch.object(server, 'TUNING', self.tuning),
            patch.object(self.tuning, 'status', side_effect=lambda: {'audio_owned': self.owned}),
            patch.object(server, 'mpd', return_value=[[], [], []]),
            patch.object(server.INPUT_LEVELS, 'release'),
            patch.object(server, 'discover_sources', side_effect=lambda: copy.deepcopy(self.sources)),
            patch('manual_source_route.subprocess.run', return_value=MagicMock(returncode=0)),
            patch('airplay_baseline.subprocess.run', return_value=MagicMock(returncode=0)),
            patch('airplay_control.loopback_available', return_value=True),
            patch('airplay_control.receiver_running', return_value=True),
            patch('airplay_control.pcm_running', return_value=True),
            patch('manual_source_route.time.sleep'),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.new_process()

    def new_process(self):
        """Fresh controllers and route, as after a service restart (lease released)."""
        for old in ('manual', 'control'):
            if hasattr(self, old):
                (getattr(self, old).baseline if old == 'control' else getattr(self, old)).release()
        from manual_source_route import ManualSourceRoute
        self.control = AirPlayController(server)
        self.manual = ManualSourceRoute(server)
        for target, value in (('AIRPLAY_CONTROL', self.control), ('MANUAL_ROUTE', self.manual)):
            patcher = patch.object(server, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.manual.release)
        self.addCleanup(self.control.baseline.release)

    # --- simulated CamillaDSP -------------------------------------------------
    def call(self, name, *args):
        self.calls.append((name, *args))
        if name == 'GetConfigFilePath':
            return self.path
        if name == 'GetConfigJson':
            return json.dumps(self.loaded)
        if name == 'GetState':
            return 'Running'
        if name == 'GetVolume':
            return self.volume
        if name == 'GetMute':
            return self.muted
        if name == 'SetMute':
            self.muted = args[0]
        if name == 'SetVolume':
            self.volume = args[0]
        if name == 'SetConfigFilePath':
            if Path(args[0]).name.startswith('manual-source-'):
                # No candidate may ever be selected before its journal is durable.
                self.assertTrue((self.storage / 'manual-source-recovery.json').exists())
            self.path = args[0]
        if name == 'Reload':
            if self.reload_failure:
                raise RuntimeError('reload failed')
            self.loaded = yaml.safe_load(Path(self.path).read_bytes())
            if Path(self.path).name.startswith('manual-source-'):
                # CamillaDSP returns parsed defaults for fields the YAML omitted.
                self.loaded['devices']['capture']['labels'] = None
                # The approved baseline is already a full dump; only the new input stage gains them.
                for row in self.loaded.get('mixers', {}).get(audio_sources.SOURCE_MIXER, {}).get('mapping', []):
                    for source in row['sources']:
                        source.setdefault('inverted', False)
        return None

    def names(self):
        return [call[0] for call in self.calls]

    def assert_never_unmuted(self):
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertNotIn('SetVolume', self.names())

    def assert_baseline_restored(self):
        self.assertEqual(self.path, str(self.original))
        self.assertEqual(self.loaded, self.config)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertTrue(self.muted)
        self.assertFalse((self.storage / 'manual-source-recovery.json').exists())
        self.assertEqual(list(self.storage.glob('manual-source-*.yml')), [])

    def select(self, source):
        return server.action('source', {'id': source['id']})

    def reset_baseline(self, config=None):
        """Install, approve and load a (possibly altered) baseline."""
        config = config or self.config
        self.original.write_text(yaml.safe_dump(config))
        self.original_bytes = self.original.read_bytes()
        (self.root / 'autotune' / 'active.json').write_text(json.dumps(dict(
            config_path=str(self.original), sha256=airplay_baseline.digest(self.original_bytes), name='n')))
        self.loaded = copy.deepcopy(config)
        self.path = str(self.original)
        self.calls.clear()


class ApprovedBaselineSelection(RouteFixture):
    def test_usb_selection_keeps_approved_baseline_and_every_output_setting(self):
        self.select(MONO_1)
        self.assertEqual(audio_sources.source_metadata(self.loaded)['id'], MONO_1['id'])
        self.assertNotEqual(self.path, str(self.original))
        # Only the allowlisted input stage differs from the approved speaker chain.
        self.assertEqual(self.loaded['pipeline'][1:], self.config['pipeline'])
        self.assertEqual(self.loaded['filters'], self.config['filters'])
        self.assertEqual(self.loaded['devices']['playback'], self.config['devices']['playback'])
        self.assertEqual(len(self.loaded['filters']), 129)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertTrue(self.muted)
        self.assert_never_unmuted()


JOURNAL = 'manual-source-recovery.json'


class Selection(RouteFixture):
    def test_repeated_switches_round_trip_from_the_original_each_time(self):
        seen = []
        real = audio_sources.source_config
        def spy(original, source):
            seen.append((copy.deepcopy(original), source['id']))
            return real(original, source)
        with patch('manual_source_route.source_config', side_effect=spy):
            for source in (MONO_1, PAIR, AIRPLAY, MONO_2, MONO_1):
                self.select(source)
                self.assertEqual(audio_sources.source_metadata(self.loaded)['id'], source['id'])
                stages = [st for st in self.loaded['pipeline'] if st.get('name') == audio_sources.SOURCE_MIXER]
                self.assertEqual(len(stages), 1)
                self.assertEqual(self.loaded['pipeline'][1:], self.config['pipeline'])
                self.assertEqual(self.loaded['filters'], self.config['filters'])
                self.assertEqual(len(list(self.storage.glob('manual-source-*.yml'))), 1)
                self.assertEqual(self.original.read_bytes(), self.original_bytes)
        # Candidates are never derived from a previous candidate.
        self.assertTrue(all(original == self.config for original, _ in seen))
        self.select(audio_sources.TIDAL)
        self.assert_baseline_restored()
        self.assertEqual(server.ACTIVE_CAPTURE_CHANNELS, (0, 1))
        self.assert_never_unmuted()
        self.assertIn((('pause 1',)), [c.args for c in server.mpd.call_args_list])
        self.assertNotIn(('clear',), [c.args for c in server.mpd.call_args_list])

    def test_same_source_and_tidal_baseline_are_verified_noops(self):
        self.select(audio_sources.TIDAL)
        self.assertNotIn('SetConfigFilePath', self.names())
        self.select(MONO_1)
        self.calls.clear()
        self.select(MONO_1)
        self.assertNotIn('SetConfigFilePath', self.names())
        self.assertNotIn('Reload', self.names())
        self.assert_never_unmuted()

    def test_manual_airplay_selection_is_waiting_never_a_cast(self):
        self.select(AIRPLAY)
        status = server.current_status()
        self.assertEqual(status['source'], 'airplay')
        self.assertFalse(status['airplay']['active'])
        self.assertFalse(status['airplay']['preparing'])
        self.assertTrue(status['muted'])
        self.assertFalse(self.control.active)
        self.control.tick(now=5)
        self.assert_never_unmuted()

    def test_explicit_unmute_needs_the_verified_candidate_and_play_needs_tidal(self):
        self.select(MONO_1)
        server.action('mute', {'muted': False})
        self.assertIn(('SetMute', False), self.calls)
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'Select Tidal'):
            server.action('resume', {})
        self.loaded['filters']['baseline_only_1']['parameters']['gain'] = 9  # foreign edit
        for action, body in (('mute', dict(muted=False)), ('volume', dict(db=-40)),
                             ('unlock', dict(enabled=True))):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                self.muted = True
                server.action(action, body)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertNotIn('SetVolume', self.names())

    def test_candidate_path_is_not_approved_without_a_verified_journal(self):
        self.select(MONO_1)
        approved_path = self.path
        self.manual.release()
        self.new_process()  # a different process has no verified route state
        with self.assertRaises(RuntimeError):
            server.approved(self.dsp)
        self.assertEqual(self.path, approved_path)
        (self.storage / JOURNAL).unlink()
        with self.assertRaises(RuntimeError):
            server.approved(self.dsp)


class FailClosedBeforeMutation(RouteFixture):
    def assert_untouched(self):
        self.assertNotIn('SetConfigFilePath', self.names())
        self.assertNotIn('Reload', self.names())
        self.assertFalse((self.storage / JOURNAL).exists())
        self.assertEqual(list(self.storage.glob('manual-source-*.yml')), [])
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assert_never_unmuted()

    def refuse(self, source=MONO_1, error=RuntimeError, pattern=None):
        with self.assertRaisesRegex(error, pattern or ''):
            self.select(source)
        self.assert_untouched()

    def test_unapproved_tampered_or_different_loaded_baseline(self):
        (self.root / 'autotune' / 'active.json').unlink()
        self.refuse(pattern='approved')
        (self.root / 'autotune' / 'active.json').write_text(json.dumps(dict(
            config_path=str(self.original), sha256=self.sha, name='n')))
        self.loaded['devices']['playback']['device'] = 'hw:Other'
        self.refuse(pattern='exact approved baseline')
        self.loaded = copy.deepcopy(self.config)
        self.original.write_bytes(self.original_bytes + b'# tampered\n')
        self.original_bytes = self.original.read_bytes()  # only the *approved* bytes are immutable
        self.refuse(pattern='exact approved baseline')

    def test_unknown_foreign_and_non_baseline_paths(self):
        for path in ('/opt/camilladsp/configs/exciter-audition.yml', '/tmp/other.yml',
                     str(self.storage / 'airplay-baseline.yml'),
                     str(self.original.parent / 'sub' / 'x.yml')):
            with self.subTest(path=path):
                self.path = path
                self.refuse()

    def test_autotune_ownership_unknown_or_owned_fails_closed(self):
        for value in (True, None, 'maybe', 0):
            with self.subTest(value=value):
                self.owned = value
                self.refuse()
        with patch.object(self.tuning, 'status', return_value={}):
            self.refuse()
        with patch.object(self.tuning, 'status', side_effect=RuntimeError('offline')):
            self.refuse()

    def test_unsupported_topology_rate_and_missing_device(self):
        for label, mutate in dict(
                rate=lambda c: c['devices'].update(samplerate=44100),
                channels=lambda c: c['devices']['capture'].update(channels=10),
                capture=lambda c: c['devices']['capture'].update(device='hw:CARD=Other,DEV=0'),
                dac=lambda c: c['devices']['playback'].update(device='hw:Loopback,0,0'),
                outputs=lambda c: c['devices']['playback'].update(channels=2),
                ambiguous=lambda c: c['pipeline'].insert(1, dict(type='Mixer', name=audio_sources.SOURCE_MIXER)),
        ).items():
            with self.subTest(label=label):
                altered = copy.deepcopy(self.config)
                mutate(altered)
                self.reset_baseline(altered)
                self.refuse()
        self.reset_baseline()
        slow = dict(MONO_1, rates=[96000])
        self.sources = [audio_sources.TIDAL, slow]
        self.refuse(slow, ValueError)
        self.sources = [audio_sources.TIDAL]
        self.refuse(MONO_2, ValueError, 'no longer connected')

    def test_busy_or_replaced_audio_lock_and_dsp_not_running(self):
        with self.lock_path.open('rb') as competitor:
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.refuse(pattern='audio lock')
        base = self.dsp.call.side_effect
        self.dsp.call.side_effect = lambda name, *a: 'Stopped' if name == 'GetState' else base(name, *a)
        self.refuse(pattern='not running')

    def test_mute_mpd_check_and_candidate_races_never_switch(self):
        base = self.dsp.call.side_effect
        self.dsp.call.side_effect = lambda name, *a: False if name == 'GetMute' else base(name, *a)
        self.refuse(pattern='mute')
        self.dsp.call.side_effect = base
        self.calls.clear()
        with patch.object(server, 'mpd', side_effect=RuntimeError('offline')):
            self.refuse(pattern='offline')
        self.calls.clear()
        with patch('manual_source_route.subprocess.run', return_value=MagicMock(returncode=1)):
            self.refuse(pattern='rejected')
        self.calls.clear()
        def tamper(command, **kwargs):
            Path(command[-1]).write_bytes(b'tampered')
            return MagicMock(returncode=0)
        with patch('manual_source_route.subprocess.run', side_effect=tamper):
            self.refuse(pattern='changed')

    def test_failed_journal_durability_never_switches_path(self):
        with patch('manual_source_route.os.fdatasync', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                self.select(MONO_1)
        self.assert_untouched()
        self.assertEqual(list(self.storage.glob('.manual-*')), [])
        real = __import__('airplay_baseline').sync_directory
        def failing(root):
            if (self.storage / JOURNAL).exists():
                raise OSError('directory sync failed')
            real(root)
        self.calls.clear()
        with patch('manual_source_route.sync_directory', side_effect=failing):
            with self.assertRaises(OSError):
                self.select(MONO_1)
        self.assertNotIn('SetConfigFilePath', self.names())
        self.assertTrue((self.storage / JOURNAL).exists())
        self.assertTrue(self.manual.blocked)
        # Evidence with no switched path: the next selection retries recovery first.
        self.select(audio_sources.TIDAL)
        self.assert_baseline_restored()

    def test_orphan_candidate_from_a_crash_before_the_journal_is_swept(self):
        orphan = self.storage / ('manual-source-' + 'a' * 32 + '.yml')
        orphan.write_text('stale')
        self.select(MONO_1)
        self.assertFalse(orphan.exists())
        self.assertEqual(len(list(self.storage.glob('manual-source-*.yml'))), 1)


class CrashBoundaries(RouteFixture):
    """A process death must leave the original recoverable, muted, with nothing unmuted."""

    def crash_at(self, boundary, sources=(MONO_1,)):
        normal = self.dsp.call.side_effect
        import manual_source_route
        real_publish = manual_source_route.publish
        def crash_call(name, *args):
            result = normal(name, *args)
            if ((boundary == 'path' and name == 'SetConfigFilePath' and args[0] != str(self.original))
                    or (boundary == 'reload' and name == 'Reload' and self.path != str(self.original))):
                raise SystemExit('simulated process death')
            return result
        def crash_publish(path, data, mode):
            if boundary == 'before-journal':
                raise SystemExit('simulated process death')
            real_publish(path, data, mode)
            if boundary == 'journal':
                raise SystemExit('simulated process death')
        with patch.object(manual_source_route, 'publish', side_effect=crash_publish), \
             patch.object(self.dsp, 'call', side_effect=crash_call):
            with self.assertRaises(SystemExit):
                for source in sources:
                    self.select(source)

    def restart(self):
        self.new_process()
        server.initialize_dsp()

    def test_first_selection_crash_before_journal_after_journal_path_and_reload(self):
        for boundary in ('before-journal', 'journal', 'path', 'reload'):
            with self.subTest(boundary=boundary):
                self.calls.clear()
                self.crash_at(boundary)
                self.assertEqual((self.storage / JOURNAL).exists(), boundary != 'before-journal')
                self.restart()
                if boundary == 'before-journal':
                    self.assertEqual(self.path, str(self.original))
                    self.assertEqual(self.loaded, self.config)
                    self.assertEqual(self.manual.blocked, None)
                else:
                    self.assert_baseline_restored()
                self.assertTrue(self.muted)
                self.assert_never_unmuted()
                self.assertEqual(self.original.read_bytes(), self.original_bytes)
                self.select(MONO_1)  # the next selection starts clean and then recovers
                self.select(audio_sources.TIDAL)
                self.assert_baseline_restored()

    def test_second_switch_crash_after_replacement_and_after_path_switch(self):
        for boundary in ('journal', 'path', 'reload'):
            with self.subTest(boundary=boundary):
                self.calls.clear()
                self.select(MONO_1)
                first = json.loads((self.storage / JOURNAL).read_text())['generation']
                import manual_source_route
                # Crash points are armed only for the second selection.
                normal = self.dsp.call.side_effect
                real_persist = manual_source_route.persist
                def crash_persist(path, data, mode):
                    real_persist(path, data, mode)
                    if boundary == 'journal' and path.name == JOURNAL \
                            and json.loads(data)['generation'] != first:
                        raise SystemExit('simulated process death')
                def crash_call(name, *args):
                    result = normal(name, *args)
                    if ((boundary == 'path' and name == 'SetConfigFilePath')
                            or (boundary == 'reload' and name == 'Reload')):
                        raise SystemExit('simulated process death')
                    return result
                with patch.object(manual_source_route, 'persist', side_effect=crash_persist), \
                     patch.object(self.dsp, 'call', side_effect=crash_call):
                    with self.assertRaises(SystemExit):
                        self.select(MONO_2)
                record = json.loads((self.storage / JOURNAL).read_text())
                self.assertNotEqual(record['generation'], first)
                self.assertEqual(record['previous'], first)
                self.restart()
                self.assert_baseline_restored()
                self.assert_never_unmuted()

    def test_crash_while_deleting_leaves_evidence_until_baseline_verified(self):
        self.select(MONO_1)
        real_sync = __import__('airplay_baseline').sync_directory
        def crash(root):
            if not (self.storage / JOURNAL).exists():
                raise SystemExit('simulated process death')
            real_sync(root)
        with patch('manual_source_route.sync_directory', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.manual.recover()
        self.restart()
        self.assert_baseline_restored()


class JournalAndRecovery(RouteFixture):
    def leave_journal(self, source=MONO_1):
        self.select(source)
        self.new_process()
        return (self.storage / JOURNAL).read_bytes()

    def assert_retained(self, journal):
        self.assertEqual((self.storage / JOURNAL).read_bytes(), journal)
        self.assertTrue(self.muted)
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue(self.manual.blocked)

    def test_malformed_stale_or_hostile_journals_stay_muted_and_retained(self):
        good = json.loads(self.leave_journal())
        bad = {
            'empty': b'{}', 'oversized': b'x' * 8193, 'not json': b'{', 'list': b'[]',
            'duplicate': json.dumps(good).replace('{', '{"version":1,', 1).encode(),
            'extra': json.dumps(dict(good, extra=1)).encode(),
            'missing': json.dumps({k: v for k, v in good.items() if k != 'source'}).encode(),
            'bad generation': json.dumps(dict(good, generation='../../x')).encode(),
            'same previous': json.dumps(dict(good, previous=good['generation'])).encode(),
            'bad source': json.dumps(dict(good, source='tidal')).encode(),
            'bool version': json.dumps(dict(good, version=True)).encode(),
            'foreign original': json.dumps(dict(good, original='/etc/passwd')).encode(),
            'bad digest': json.dumps(dict(good, sha256='0' * 64)).encode(),
        }
        for label, content in bad.items():
            with self.subTest(label=label):
                (self.storage / JOURNAL).write_bytes(content)
                self.calls.clear()
                self.new_process()
                server.initialize_dsp()
                self.assert_retained(content)
                self.assertNotIn('SetConfigFilePath', self.names())
                self.assertNotIn('Reload', self.names())
                for action, body in (('mute', dict(muted=False)), ('volume', dict(db=-40)),
                                     ('source', dict(id=MONO_2['id'])), ('unlock', dict(enabled=True))):
                    with self.assertRaises(Exception):
                        server.action(action, body)
                self.assertNotIn(('SetMute', False), self.calls)
        # The same journal restored byte-for-byte can now be recovered.
        (self.storage / JOURNAL).write_text(json.dumps(good))
        self.new_process()
        server.initialize_dsp()
        self.assert_baseline_restored()

    def test_symlink_journal_is_never_followed_or_removed(self):
        self.leave_journal()
        target = self.root / 'elsewhere.json'
        target.write_bytes((self.storage / JOURNAL).read_bytes())
        (self.storage / JOURNAL).unlink()
        (self.storage / JOURNAL).symlink_to(target)
        self.new_process()
        server.initialize_dsp()
        self.assertTrue((self.storage / JOURNAL).is_symlink())
        self.assertTrue(target.exists())
        self.assertTrue(self.manual.blocked)

    def test_stale_journal_recovers_muted_at_startup_without_playing(self):
        self.leave_journal()
        self.muted = False  # a reload or crash may leave sound enabled
        self.calls.clear()
        server.initialize_dsp()
        self.assert_baseline_restored()
        self.assertEqual(self.calls[0], ('SetMute', True))
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertNotIn('SetVolume', self.names())
        self.assertNotIn(('play',), [c.args for c in server.mpd.call_args_list])

    def test_changed_approval_or_baseline_retains_candidate_and_evidence(self):
        journal = self.leave_journal()
        candidate = next(self.storage.glob('manual-source-*.yml'))
        candidate_bytes = candidate.read_bytes()
        self.original.write_bytes(self.original_bytes + b'# changed\n')
        self.calls.clear()
        server.initialize_dsp()
        self.assert_retained(journal)
        self.assertEqual(candidate.read_bytes(), candidate_bytes)
        self.assertNotEqual(self.path, str(self.original))
        self.assertNotIn('SetConfigFilePath', self.names())
        with self.assertRaises(RuntimeError):
            server.action('mute', {'muted': False})
        self.assertNotIn(('SetMute', False), self.calls)
        self.original.write_bytes(self.original_bytes)
        server.action('mute', {'muted': True})  # the retry latch recovers once approval is intact
        self.assert_baseline_restored()

    def test_external_config_under_the_candidate_path_recovers_the_approved_baseline(self):
        for change in (lambda: self.loaded['filters']['baseline_only_2']['parameters'].update(gain=7),
                       lambda: self.loaded['devices']['playback'].update(device='hw:Other')):
            self.leave_journal()
            change()
            server.initialize_dsp()
            self.assert_baseline_restored()
            self.assert_never_unmuted()

    def test_foreign_config_under_the_original_path_is_retained(self):
        journal = self.leave_journal()
        self.path = str(self.original)
        self.loaded['filters']['baseline_only_2']['parameters']['gain'] = 7
        self.calls.clear()
        server.initialize_dsp()
        self.assert_retained(journal)
        self.assertNotIn('SetConfigFilePath', self.names())
        self.assertNotIn('Reload', self.names())

    def test_external_path_change_is_never_overwritten(self):
        for label, change in dict(
                path=lambda: setattr(self, 'path', '/opt/camilladsp/configs/other.yml'),
                airplay=lambda: setattr(self, 'path', str(self.storage / 'airplay-baseline.yml')),
                bluetooth=lambda: setattr(self, 'path', str(self.storage / 'bluetooth-baseline.yml')),
        ).items():
            with self.subTest(label=label):
                self.leave_journal()
                journal = (self.storage / JOURNAL).read_bytes()
                change()
                self.calls.clear()
                server.initialize_dsp()
                self.assert_retained(journal)
                self.assertNotIn('SetConfigFilePath', self.names())
                self.assertNotIn('Reload', self.names())
                # Restore the route's own state so the next case starts clean.
                self.path = str(next(self.storage.glob('manual-source-*.yml')))
                self.loaded = yaml.safe_load(Path(self.path).read_bytes())
                self.new_process()
                server.initialize_dsp()
                self.assert_baseline_restored()

    def test_reload_failure_restores_the_baseline_and_a_failed_restore_blocks_everything(self):
        self.reload_failure = True
        with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
            self.select(MONO_1)
        self.assertTrue((self.storage / JOURNAL).exists())
        self.assertTrue(self.muted)
        self.assertIsNotNone(self.manual._lock_fd)
        self.assertTrue(self.manual.blocked)
        self.assert_never_unmuted()
        status = server.current_status()
        self.assertFalse(status['source_switching_available'])
        self.assertIn('recovery', status['source_switching_reason'].lower())
        for action, body in (('mute', dict(muted=False)), ('volume', dict(db=-40)),
                             ('source', dict(id=MONO_2['id'])), ('unlock', dict(enabled=True))):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                server.action(action, body)
        self.assertNotIn(('SetMute', False), self.calls)
        self.reload_failure = False
        server.action('mute', {'muted': True})  # the retry latch runs recovery first
        self.assert_baseline_restored()
        self.assertTrue(server.current_status()['source_switching_available'])

    def test_failed_reload_with_working_restore_reports_restored_and_stays_muted(self):
        normal = self.dsp.call.side_effect
        def flaky(name, *args):
            if name == 'Reload' and self.path != str(self.original):
                raise RuntimeError('reload failed')
            return normal(name, *args)
        with patch.object(self.dsp, 'call', side_effect=flaky):
            with self.assertRaisesRegex(RuntimeError, 'original baseline was restored'):
                self.select(MONO_1)
        self.assert_baseline_restored()
        self.assert_never_unmuted()

    def test_wrong_loaded_config_after_reload_restores_baseline(self):
        normal = self.dsp.call.side_effect
        def wrong(name, *args):
            result = normal(name, *args)
            if name == 'Reload' and self.path != str(self.original):
                self.loaded['filters']['baseline_only_9']['parameters']['gain'] = 12
            return result
        with patch.object(self.dsp, 'call', side_effect=wrong):
            with self.assertRaises(RuntimeError):
                self.select(MONO_1)
        self.assert_baseline_restored()
        self.assert_never_unmuted()

    def test_mute_lost_during_load_fails_closed(self):
        normal = self.dsp.call.side_effect
        def lose_mute(name, *args):
            if name == 'GetState' and self.path != str(self.original):
                self.muted = False
            return normal(name, *args)
        with patch.object(self.dsp, 'call', side_effect=lose_mute):
            with self.assertRaises(RuntimeError):
                self.select(MONO_1)
        self.assert_baseline_restored()



class Arbitration(RouteFixture):
    """Three journals, one deterministic owner: conflicts stay muted with evidence retained."""

    def bluetooth(self):
        from bluetooth_control import BluetoothControl
        control = BluetoothControl(server)
        patcher = patch.object(server, 'BLUETOOTH_CONTROL', control)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(control.route.release)
        return control

    def evidence(self):
        return {p.name: p.read_bytes() for p in self.storage.iterdir() if p.is_file()}

    def test_every_pairwise_journal_combination_stays_muted_and_retains_everything(self):
        for foreign in ('airplay-recovery.json', 'bluetooth-recovery.json'):
            with self.subTest(foreign=foreign):
                self.select(MONO_1)
                bt = self.bluetooth()
                self.new_process()
                bt.route.release()
                (self.storage / foreign).write_bytes(b'{"foreign":"owner evidence"}')
                before = self.evidence()
                self.calls.clear()
                server.initialize_dsp()
                self.assertEqual(self.evidence(), before)
                self.assertTrue(self.muted)
                self.assertNotIn('SetConfigFilePath', self.names())
                self.assertNotIn('Reload', self.names())
                self.assertNotIn(('SetMute', False), self.calls)
                self.assertTrue(self.manual.blocked)
                self.assertTrue(self.control.needs_mute)
                for action, body in (('mute', dict(muted=False)), ('source', dict(id=MONO_2['id'])),
                                     ('volume', dict(db=-40))):
                    with self.assertRaises(Exception):
                        server.action(action, body)
                self.assertEqual(self.evidence(), before)
                # Only an operator removing the other journal lets recovery proceed.
                self.manual.release()
                bt.route.release()
                (self.storage / foreign).unlink()
                self.new_process()
                server.initialize_dsp()
                self.assert_baseline_restored()

    def test_foreign_candidate_paths_are_never_reloaded_over(self):
        for candidate in ('airplay-baseline.yml', 'bluetooth-baseline.yml'):
            with self.subTest(candidate=candidate):
                self.select(MONO_1)
                before = self.evidence()
                self.path = str(self.storage / candidate)
                self.new_process()
                self.calls.clear()
                server.initialize_dsp()
                self.assertEqual(self.evidence(), before)
                self.assertNotIn('Reload', self.names())
                self.assertNotIn('SetConfigFilePath', self.names())
                self.assertTrue(self.manual.blocked)
                self.path = str(next(self.storage.glob('manual-source-*.yml')))
                self.loaded = yaml.safe_load(Path(self.path).read_bytes())
                self.new_process()
                server.initialize_dsp()
                self.assert_baseline_restored()

    def test_airplay_and_bluetooth_routes_refuse_while_a_manual_journal_exists(self):
        self.select(MONO_1)
        self.new_process()
        before = self.evidence()
        self.calls.clear()
        with self.assertRaisesRegex(RuntimeError, 'manual source'):
            self.control.baseline.begin(str(self.original), copy.deepcopy(self.config))
        with self.assertRaisesRegex(RuntimeError, 'manual source'):
            self.control.baseline.recover()
        bt = self.bluetooth()
        with self.assertRaisesRegex(RuntimeError, 'manual source'):
            bt.route.begin(str(self.original), copy.deepcopy(self.config))
        self.assertEqual(self.evidence(), before)
        self.assertNotIn('SetConfigFilePath', self.names())

    def test_airplay_cast_recovers_a_manual_route_first_and_never_layers_candidates(self):
        self.select(MONO_1)
        self.calls.clear()
        with patch('airplay_control.pcm_running', return_value=False):  # the hook precedes PCM
            self.control.event('start', now=0)
        paths = [c[1] for c in self.calls if c[0] == 'SetConfigFilePath']
        self.assertEqual(paths, [str(self.original), str(self.control.baseline.candidate)])
        self.assertFalse((self.storage / JOURNAL).exists())
        self.assertEqual(list(self.storage.glob('manual-source-*.yml')), [])
        self.assertTrue((self.storage / 'airplay-recovery.json').exists())
        self.assertNotIn(audio_sources.SOURCE_MIXER, self.loaded.get('mixers', {}))
        self.assertEqual(self.loaded['pipeline'], self.config['pipeline'])
        self.assertNotIn(('SetMute', False), self.calls)  # the cast unmutes only once live
        self.control.tick(now=1)
        self.assertTrue(self.control.active)
        self.assertEqual(self.calls.count(('SetMute', False)), 1)

    def test_airplay_start_refuses_over_a_manual_conflict(self):
        self.select(MONO_1)
        self.new_process()
        (self.storage / 'bluetooth-recovery.json').write_bytes(b'{}')
        before = self.evidence()
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.control.event('start', now=0)
        self.assertEqual(self.evidence(), before)
        self.assertNotIn('SetConfigFilePath', self.names())
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertFalse(self.control.active)

    def test_bluetooth_start_refuses_while_a_manual_route_is_loaded(self):
        self.select(MONO_1)
        bt = self.bluetooth()
        with self.assertRaisesRegex(RuntimeError, 'Manual source'):
            bt._idle()
        self.assertFalse(bt.owns_audio())

    def test_manual_selection_recovers_a_live_airplay_baseline_first(self):
        self.control.event('start', now=0)
        self.control.tick(now=1)
        self.assertTrue(self.control.active)
        self.calls.clear()
        self.select(MONO_1)
        self.assertFalse((self.storage / 'airplay-recovery.json').exists())
        self.assertFalse(self.control.active)
        self.assertEqual(audio_sources.source_metadata(self.loaded)['id'], MONO_1['id'])
        self.assertTrue(self.muted)
        self.control.tick(now=5)
        self.control.event('start', now=6)  # a stale duplicate while PCM still runs
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue((self.storage / JOURNAL).exists())

    def test_failed_airplay_recovery_blocks_manual_selection_without_a_route(self):
        self.control.event('start', now=0)
        self.control.tick(now=1)
        self.reload_failure = True
        self.calls.clear()
        with self.assertRaises(RuntimeError):
            self.select(MONO_1)
        self.assertFalse((self.storage / JOURNAL).exists())
        self.assertTrue((self.storage / 'airplay-recovery.json').exists())
        self.assertNotIn(('SetMute', False), self.calls)

    def test_autotune_handoff_restores_the_manual_route_first(self):
        self.select(MONO_1)
        seen = []
        def request(action, body):
            seen.append((self.path, (self.storage / JOURNAL).exists(), self.muted))
            return dict(ok=True)
        with patch.object(self.tuning, 'request', side_effect=request):
            server.action('auto-start', {})
        self.assertEqual(seen, [(str(self.original), False, True)])

    def test_stale_receiver_events_cannot_touch_a_manual_route(self):
        self.select(MONO_1)
        before = self.evidence()
        self.calls.clear()
        self.control.event('stop', now=1)
        self.control.tick(now=3)
        self.control.event('start', now=4)
        self.assertEqual(self.evidence(), before)
        self.assertNotIn('SetConfigFilePath', self.names())
        self.assertNotIn(('SetMute', False), self.calls)
        self.assertTrue(self.manual.approves(self.dsp, self.path))

    def test_autotune_ownership_blocks_controls_but_never_the_stop_request(self):
        self.select(MONO_1)
        self.owned = True
        self.assertFalse(self.manual.approves(self.dsp, self.path))
        with self.assertRaisesRegex(RuntimeError, 'Auto mode owns'):
            server.action('mute', {'muted': False})
        self.assertNotIn(('SetMute', False), self.calls)
        for name, body in (('stop', {}), ('auto-stop', {}), ('mute', dict(muted=True))):
            with self.subTest(name=name), patch.object(self.tuning, 'request', return_value=dict(ok=True)) as request:
                server.action(name, body)
            request.assert_called_once_with('stop', {})
        self.assertTrue((self.storage / JOURNAL).exists())  # evidence untouched by the stop request
        self.assertTrue(self.muted)

    def test_in_memory_airplay_or_bluetooth_ownership_refuses_a_manual_route(self):
        for label, change in dict(active=lambda: setattr(self.control, 'active', True),
                                  pending=lambda: setattr(self.control, 'pending', 1.0),
                                  restore=lambda: setattr(self.control, 'restore_pending', b'old')).items():
            with self.subTest(label=label):
                change()
                with self.assertRaisesRegex(RuntimeError, 'AirPlay owns'):
                    self.manual.select(MONO_1)
                self.control.active, self.control.pending, self.control.restore_pending = False, None, None
        bt = self.bluetooth()
        bt.session = object()
        with self.assertRaisesRegex(RuntimeError, 'Bluetooth owns'):
            self.manual.select(MONO_1)
        bt.session = None
        self.assertNotIn('SetConfigFilePath', self.names())
        self.assertFalse((self.storage / JOURNAL).exists())

    def test_startup_recovery_order_is_manual_airplay_bluetooth(self):
        order = []
        bluetooth = MagicMock()
        bluetooth.startup.side_effect = lambda: order.append('bluetooth')
        control = MagicMock(needs_mute=False)
        control.recover.side_effect = lambda: order.append('airplay')
        with patch.object(server, 'BLUETOOTH_CONTROL', bluetooth), patch.object(server, 'AIRPLAY_CONTROL', control), \
             patch.object(self.manual, 'exists', return_value=True), \
             patch.object(self.manual, 'recover', side_effect=lambda: order.append('manual')):
            server.initialize_dsp()
        self.assertEqual(order, ['manual', 'airplay', 'bluetooth'])

    def test_failed_manual_startup_skips_airplay_recovery_but_still_mutes(self):
        control = MagicMock(needs_mute=False)
        with patch.object(server, 'AIRPLAY_CONTROL', control), \
             patch.object(self.manual, 'exists', return_value=True), \
             patch.object(self.manual, 'recover', side_effect=RuntimeError('conflict')):
            server.initialize_dsp()
        control.recover.assert_not_called()
        self.assertTrue(control.needs_mute)
        self.assertTrue(self.muted)


class Status(RouteFixture):
    def test_approved_baseline_offers_selection_and_names_the_output_profile(self):
        status = server.current_status()
        self.assertTrue(status['source_switching_available'])
        self.assertIsNone(status['source_switching_reason'])
        self.assertEqual(status['profile'], 'Auto Tune 2026-10-26')
        self.assertTrue(status['ready'])
        self.assertEqual(status['source'], 'tidal')
        self.assertEqual(len(status['sources']), 5)

    def test_input_route_keeps_the_output_profile_label_and_is_not_an_editable_preset(self):
        self.select(MONO_1)
        status = server.current_status()
        self.assertEqual(status['source'], MONO_1['id'])
        self.assertEqual(status['profile'], 'Auto Tune 2026-10-26')
        self.assertTrue(status['ready'])
        self.assertTrue(status['source_switching_available'])
        self.assertEqual(status['source_route'], dict(active=True, recovery_pending=False,
                                                       source=MONO_1['id'], error=None))
        dsp = server.dsp_status()
        self.assertTrue(dsp['auto_tuned'])
        self.assertEqual(dsp['profile'], 'Auto Tune 2026-10-26')
        self.assertEqual(dsp['channels'], [])
        self.assertEqual(dsp['input_label'], MONO_1['label'])

    def test_blocked_selection_states_a_specific_reason(self):
        cases = {}
        def owned():
            return patch.object(server, 'meter_audio_owned', return_value=True)
        def not_running():
            base = self.dsp.call.side_effect
            return patch.object(self.dsp, 'call', side_effect=lambda n, *a: 'Stopped' if n == 'GetState' else base(n, *a))
        def unapproved():
            (self.root / 'autotune' / 'active.json').unlink()
            return patch.object(server, 'meter_audio_owned', return_value=False)
        for label, make, pattern in (('autotune', owned, 'Auto Tune'), ('stopped', not_running, 'not running'),
                                     ('unapproved', unapproved, 'not an approved')):
            with self.subTest(label=label), make():
                status = server.current_status()
                self.assertFalse(status['source_switching_available'])
                self.assertRegex(status['source_switching_reason'], pattern)
        (self.root / 'autotune' / 'active.json').write_text(json.dumps(dict(
            config_path=str(self.original), sha256=self.sha, name='n')))

    def test_unknown_topology_and_foreign_airplay_route_are_blocked(self):
        altered = copy.deepcopy(self.config)
        altered['devices']['capture']['channels'] = 10
        self.reset_baseline(altered)
        status = server.current_status()
        self.assertFalse(status['source_switching_available'])
        self.assertRegex(status['source_switching_reason'], 'stereo Tidal')

    def test_classic_channel_profile_and_live_airplay_baseline_remain_selectable(self):
        with patch.object(server, 'CHANNEL_PROFILE', Path(self.path)):
            self.assertTrue(server.current_status()['source_switching_available'])
        self.control.event('start', now=0)
        self.control.tick(now=1)
        status = server.current_status()
        self.assertTrue(status['source_switching_available'])
        self.assertTrue(status['airplay']['active'])

    def test_pending_recovery_and_conflicts_block_with_a_reason(self):
        self.select(MONO_1)
        self.manual.blocked = 'pending'
        status = server.current_status()
        self.assertFalse(status['source_switching_available'])
        self.assertIn('recovery', status['source_switching_reason'])
        self.assertTrue(status['source_route']['recovery_pending'])
        self.manual.blocked = None
        self.control.needs_mute = True
        self.assertRegex(server.current_status()['source_switching_reason'], 'AirPlay recovery')


if __name__ == '__main__':
    unittest.main()
