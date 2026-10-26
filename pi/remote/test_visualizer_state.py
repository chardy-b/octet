import json
import io
from email.message import Message
from unittest.mock import MagicMock
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server
from visualizer_state import VisualizerState, validate


class SharedVisualizer(unittest.TestCase):
    def test_durable_settings_and_rejected_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'visualizer.json'
            state = VisualizerState(path)
            result = state.update(dict(theme='turell 5', solar=dict(tone='natural', mode='realism', speed=.5),
                                       media=dict(theme='turell 5', time=360, paused=True)))
            restored = VisualizerState(path).read()
            self.assertEqual(restored['settings'], result['settings'])
            self.assertEqual(restored['updated'], result['updated'])
            for bad in [dict(theme='../server.py'), dict(edge=float('nan')), dict(paletteClock=-1),
                        dict(zoom={'unknown': 1}), dict(solar={'mode': 'unknown'}),
                        dict(media=dict(theme='water', time=1, paused='yes')), dict(command='shell')]:
                with self.assertRaises(ValueError):
                    state.update(bad)
            self.assertEqual(state.read()['settings'], result['settings'])
            self.assertFalse(result['display']['online'])
            state.heartbeat(result['revision'])
            self.assertTrue(state.read()['display']['online'])
            self.assertEqual(state.read()['display']['revision'], result['revision'])
            view = dict(theme='turell 5', fullscreen=True, controls=0, width=1920, height=1080, renderer='webgl')
            state.heartbeat(result['revision'], view)
            self.assertEqual(state.read()['display']['view'], view)
            state.heartbeat(result['revision'], {**view, 'width': -1})
            self.assertEqual(state.read()['display']['view'], view)

    def test_preview_revision_size_freshness_and_unchanged_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            state = VisualizerState(Path(directory) / 'visualizer.json')
            self.assertIsNone(state.preview_frame())
            self.assertTrue(state.read()['previewRequested'])
            jpeg = b'\xff\xd8frame\xff\xd9'
            self.assertFalse(state.publish_frame('old', jpeg))
            for bad in (b'bad', b'not a jpeg', b'\xff\xd8' + b'x' * 524288 + b'\xff\xd9'):
                with self.assertRaises(ValueError):
                    state.publish_frame(state.revision, bad)
            self.assertTrue(state.publish_frame(state.revision, jpeg))
            self.assertEqual(state.preview_frame(), (1, jpeg))
            self.assertEqual(state.preview_frame(1), (1, None))
            state.frame_seen -= 9
            self.assertIsNone(state.preview_frame())
            state.update({'theme': 'off'})
            self.assertIsNone(state.frame_data)
            self.assertFalse(state.read()['display']['online'])

    def test_preview_upload_local_only_and_get_frame_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            state = VisualizerState(Path(directory) / 'visualizer.json')
            jpeg = b'\xff\xd8frame\xff\xd9'
            handler = object.__new__(server.Handler)
            handler.headers = Message()
            handler.headers['Host'] = '127.0.0.1:8767'
            handler.headers['Content-Type'] = 'image/jpeg'
            handler.headers['Content-Length'] = str(len(jpeg))
            handler.send = MagicMock()
            with patch.object(server, 'VISUALIZER', state), patch.object(server, 'BIND', '127.0.0.1'), \
                 patch.object(server, 'PORT', 8767), patch.object(server.NETWORK, 'snapshot', return_value=(frozenset(), ())):
                handler.path = '/api/control/visualizer-frame?revision=' + state.revision
                for peer, forwarded, expected in [('100.80.1.2', False, 403), ('127.0.0.1', True, 403), ('127.0.0.1', False, 200)]:
                    handler.client_address = (peer, 123)
                    if forwarded:
                        handler.headers['X-Forwarded-For'] = '100.80.1.2'
                    elif 'X-Forwarded-For' in handler.headers:
                        del handler.headers['X-Forwarded-For']
                    handler.rfile = io.BytesIO(jpeg)
                    handler.do_POST()
                    self.assertEqual(handler.send.call_args.args[1], expected)
                handler.path = '/api/visualizer/frame'
                handler.do_GET()
                self.assertEqual(handler.send.call_args.args, (jpeg, 200, 'image/jpeg', {'X-Octet-Frame': 1}))
                handler.path += '?after=1'
                handler.do_GET()
                self.assertEqual(handler.send.call_args.args[1], 204)
                state.frame_seen -= 9
                handler.do_GET()
                self.assertEqual(handler.send.call_args.args[1], 503)

    def test_controller_to_projection_http_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            state = VisualizerState(Path(directory) / 'visualizer.json')
            http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            port = http.server_address[1]
            threading.Thread(target=http.serve_forever, daemon=True).start()
            base = f'http://127.0.0.1:{port}'
            try:
                with patch.object(server, 'VISUALIZER', state), patch.object(server, 'BIND', '127.0.0.1'), patch.object(server, 'PORT', port):
                    request = Request(base+'/api/control/visualizer', data=json.dumps(dict(theme='water', edge=.7)).encode(),
                                      headers={'Content-Type': 'application/json', 'Origin': base})
                    with urlopen(request) as response:
                        selected = json.load(response)
                    with urlopen(base+'/api/visualizer/settings?display=1&revision='+selected['revision']) as response:
                        displayed = json.load(response)
                    self.assertEqual(displayed['settings']['theme'], 'water')
                    self.assertEqual(displayed['display']['revision'], selected['revision'])
                    with urlopen(base+'/visualizer/display') as response:
                        self.assertIn(b'class="visualizer-page projection"', response.read())
                    with urlopen(base+'/visualizer-sync.js') as response:
                        self.assertIn(b'/api/control/visualizer', response.read())
                    request.headers['Origin'] = 'https://untrusted.example'
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request)
                    self.assertEqual(error.exception.code, 403)
            finally:
                http.shutdown()
                http.server_close()


if __name__ == '__main__':
    unittest.main()
