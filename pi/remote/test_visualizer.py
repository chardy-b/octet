import io
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

import server


class Visualizer(unittest.TestCase):
    def dsp(self, levels=None, state='Running'):
        dsp = MagicMock()
        dsp.__enter__.return_value = dsp
        dsp.call.side_effect = lambda name, *args: state if name == 'GetState' else levels
        return dsp

    def test_input_envelopes_are_read_only_and_independent_of_output_gain(self):
        dsp = self.dsp(dict(capture_rms=[-20, -22], capture_peak=[-8, -10],
                            playback_rms=[-80] * 8))
        self.assertEqual(server.visualizer_frame(dsp),
                         dict(available=True, active=True, rms=[-20, -22], peak=[-8, -10]))
        self.assertEqual([call.args for call in dsp.call.call_args_list],
                         [('GetSignalLevelsSince', 0.1), ('GetState',)])

    def test_paused_inactive_and_stalled_sources_clear_old_levels(self):
        for state in ['Paused', 'Inactive', 'Stalled', 'Starting']:
            frame = server.visualizer_frame(self.dsp(dict(capture_rms=[-10], capture_peak=[-1]), state))
            self.assertFalse(frame['active'])
            self.assertEqual(frame['rms'], [])
            self.assertEqual(frame['peak'], [])

    def test_missing_nonfinite_and_out_of_range_levels_remain_json_safe(self):
        frame = server.visualizer_frame(self.dsp(dict(capture_rms=[float('nan'), None],
                                                      capture_peak=[5, -200, -1])))
        self.assertEqual(frame['rms'], [-120, -120])
        self.assertEqual(frame['peak'], [0, -120])
        json.dumps(frame, allow_nan=False)
        self.assertFalse(server.visualizer_frame(self.dsp({}))['active'])

    def test_stream_reuses_connection_and_closes_on_browser_disconnect(self):
        handler = SimpleNamespace(wfile=io.BytesIO(), send_response=MagicMock(),
                                  send_header=MagicMock(), end_headers=MagicMock())
        dsp = self.dsp(dict(capture_rms=[-12], capture_peak=[-3]))
        with patch.object(server, 'DSP', return_value=dsp) as opening, \
                patch.object(server.time, 'sleep', side_effect=BrokenPipeError):
            server.Handler.stream_visualizer(handler)
        opening.assert_called_once()
        dsp.__exit__.assert_called_once()
        handler.send_header.assert_any_call('Content-Type', 'text/event-stream')
        self.assertTrue(handler.close_connection)
        data = handler.wfile.getvalue().decode()
        frame = json.loads(data.split('data: ')[1].split('\n')[0])
        self.assertTrue(frame['active'])

    def test_dsp_failure_is_an_unavailable_event_not_an_http_or_json_error(self):
        handler = SimpleNamespace(wfile=io.BytesIO(), send_response=MagicMock(),
                                  send_header=MagicMock(), end_headers=MagicMock())
        with patch.object(server, 'DSP', side_effect=OSError('offline')):
            server.Handler.stream_visualizer(handler)
        frame = json.loads(handler.wfile.getvalue().decode().split('data: ')[1])
        self.assertEqual(frame, dict(available=False, active=False, rms=[], peak=[]))
        handler.send_response.assert_called_once_with(200)

    def test_http_assets_and_navigation_and_host_validation(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        port = http.server_address[1]
        try:
            with patch.object(server, 'BIND', '127.0.0.1'), patch.object(server, 'PORT', port):
                for path, content_type in [('/visualizer', 'text/html'),
                                           ('/visualizer.js', 'text/javascript'),
                                           ('/visualizer-themes.js', 'text/javascript'),
                                           ('/visualizer-solar.js', 'text/javascript'),
                                           ('/visualizer-audio.js', 'text/javascript'),
                                           ('/visualizer-media/sky.jpg', 'image/jpeg'),
                                           ('/visualizer-media/water.jpg', 'image/jpeg'),
                                           ('/visualizer-media/stars.jpg', 'image/jpeg'),
                                           ('/visualizer-media/solar.jpg', 'image/jpeg'),
                                           ('/visualizer-media/sun.jpg', 'image/jpeg'),
                                           ('/visualizer-media/lunar.jpg', 'image/jpeg'),
                                           ('/visualizer.css', 'text/css')]:
                    with urlopen(f'http://127.0.0.1:{port}{path}') as response:
                        self.assertTrue(response.headers['Content-Type'].startswith(content_type))
                        self.assertTrue(response.read())
                for path in ['/', '/dsp']:
                    with urlopen(f'http://127.0.0.1:{port}{path}') as response:
                        self.assertIn(b'href="/visualizer"', response.read())
                for name in ['sky.mp4', 'water.mp4', 'stars.mp4', 'solar.mp4', 'sun.mp4', 'lunar.mp4', 'edm.mp3', 'classical.mp3']:
                    url = f'http://127.0.0.1:{port}/visualizer-media/{name}'
                    size = (server.ROOT / 'visualizer-media' / name).stat().st_size
                    with urlopen(Request(url, headers={'Range': 'bytes=0-1023'})) as response:
                        self.assertEqual(response.status, 206)
                        self.assertEqual(response.headers['Content-Range'], f'bytes 0-1023/{size}')
                        self.assertEqual(len(response.read()), 1024)
                    with urlopen(Request(url, method='HEAD')) as response:
                        self.assertEqual(int(response.headers['Content-Length']), size)
                        self.assertEqual(response.read(), b'')
                    with urlopen(Request(url, headers={'Range': 'bytes=-16'})) as response:
                        with (server.ROOT / 'visualizer-media' / name).open('rb') as clip:
                            clip.seek(-16, 2)
                            self.assertEqual(response.read(), clip.read())
                    for invalid in ['bytes=-0', 'bytes=9-2', f'bytes={size}-', 'bytes=0-1,2-3']:
                        with self.assertRaises(HTTPError) as failure:
                            urlopen(Request(url, headers={'Range': invalid}))
                        self.assertEqual(failure.exception.code, 416)
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
