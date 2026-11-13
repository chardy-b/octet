"""A slow metadata response must not own playback controls or resurrect old intent."""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import server


class PlaybackConcurrency(unittest.TestCase):

    def test_tuning_stop_bypasses_pending_source_restore(self):
        with patch.object(server.TUNING, 'status', return_value={'audio_owned': True}), \
                patch.object(server.TUNING, 'request', return_value={}) as stop, \
                patch.object(server, 'recover_source', side_effect=RuntimeError('pending')) as recover:
            server.action('auto-stop', {})
            stop.assert_called_once_with('stop', {})
            recover.assert_not_called()

    def test_pending_radio_does_not_block_stop_and_cannot_start_after_it(self):
        entered, release = threading.Event(), threading.Event()
        failures = []
        def slow(_):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test deadline')
            return ([dict(id='1')], 0, dict(kind='radio'))
        def request():
            try:
                server.action('play-radio', dict(id='1'))
            except Exception as error:
                failures.append(str(error))
        with patch.object(server, 'prepare_radio', side_effect=slow), \
                patch.object(server, '_action', return_value={}) as commit:
            thread = threading.Thread(target=request)
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(server.action('stop', {}), {})
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            commit.assert_called_once_with('stop', {})
            self.assertEqual(len(failures), 1)
            self.assertIn('cancelled by a newer control', failures[0])

    def test_sdk_http_timeout_is_installed_even_for_login_and_refresh(self):
        request = Mock()
        session = SimpleNamespace(request_session=SimpleNamespace(request=request))
        server.bounded_tidal_http(session)
        session.request_session.request('GET', 'https://example.invalid', timeout=None)
        self.assertEqual(request.call_args.kwargs['timeout'], (3.0, 8.0))

    def test_stream_resolver_does_not_wait_for_metadata_lock(self):
        manifest = SimpleNamespace(is_encrypted=False, is_mpd=False, urls=['https://example.invalid/audio'])
        track = SimpleNamespace(get_stream=lambda: SimpleNamespace(get_stream_manifest=lambda: manifest))
        session = SimpleNamespace(track=lambda _: track)
        result = []
        with patch.object(server, 'STREAM_SESSION', session), server.TIDAL_LOCK:
            thread = threading.Thread(target=lambda: result.append(server.stream_url('1')))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result, [('redirect', 'https://example.invalid/audio')])

    def test_installed_sdk_login_and_refresh_use_bounded_http(self):
        try:
            import tidalapi
        except ImportError:
            self.skipTest('Optional installed Tidal SDK unavailable')
        session = tidalapi.Session()
        server.bounded_tidal_http(session)
        with patch.object(session.request_session, 'send', side_effect=TimeoutError('test')) as send:
            with self.assertRaises(TimeoutError):
                session.load_oauth_session('Bearer', 'test', 'test')
            self.assertEqual(send.call_args.kwargs['timeout'], (3.0, 8.0))
            with self.assertRaises(TimeoutError):
                session.token_refresh('test')
            self.assertEqual(send.call_args.kwargs['timeout'], (3.0, 8.0))
