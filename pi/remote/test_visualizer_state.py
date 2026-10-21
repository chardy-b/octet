import json
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
