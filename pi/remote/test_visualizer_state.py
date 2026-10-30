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
    def test_off_is_default_durable_and_clears_display_and_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'visualizer.json'
            state = VisualizerState(path)
            self.assertEqual(state.read()['settings']['theme'], 'off')
            selected = state.update(dict(theme='water'))
            state.heartbeat(selected['revision'])
            self.assertTrue(state.publish_frame(selected['revision'], b'\xff\xd8frame\xff\xd9'))
            disabled = state.update(dict(theme='off'))
            self.assertFalse(disabled['display']['online'])
            self.assertIsNone(state.preview_frame())
            self.assertEqual(VisualizerState(path).read()['settings']['theme'], 'off')
            self.assertEqual(state.update(dict(circleSize=.5))['settings']['theme'], 'off')

    def test_durable_settings_and_rejected_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'visualizer.json'
            state = VisualizerState(path)
            epoch = state.read()['animationEpoch']
            self.assertEqual(state.read()['settings']['circleSize'], 1)
            result = state.update(dict(theme='turell 5', circleSize=.65, solar=dict(tone='natural', mode='realism', speed=.5),
                                       media=dict(theme='turell 5', time=360, paused=True)))
            restored = VisualizerState(path).read()
            self.assertEqual(state.read()['animationEpoch'], epoch)
            self.assertLessEqual(epoch, state.read()['serverTime'])
            self.assertEqual(restored['settings'], result['settings'])
            self.assertEqual(restored['updated'], result['updated'])
            for bad in [dict(theme='../server.py'), dict(edge=float('nan')), dict(paletteClock=-1),
                        dict(zoom={'unknown': 1}), dict(solar={'mode': 'unknown'}),
                        dict(media=dict(theme='water', time=1, paused='yes')), dict(command='shell'),
                        dict(circleSize=.24), dict(circleSize=2.01), dict(circleSize=True), dict(circleSize=float('nan'))]:
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
                    request = Request(base+'/api/control/visualizer', data=json.dumps(dict(theme='water', edge=.7, circleSize=.65)).encode(),
                                      headers={'Content-Type': 'application/json', 'Origin': base})
                    with urlopen(request) as response:
                        selected = json.load(response)
                    with urlopen(base+'/api/visualizer/settings?display=1&revision='+selected['revision']) as response:
                        displayed = json.load(response)
                    self.assertEqual(displayed['settings']['theme'], 'water')
                    self.assertEqual(displayed['settings']['circleSize'], .65)
                    self.assertEqual(displayed['display']['revision'], selected['revision'])
                    with urlopen(base+'/visualizer/display') as response:
                        self.assertIn(b'class="visualizer-page projection"', response.read())
                    with urlopen(base+'/visualizer-sync.js') as response:
                        self.assertIn(b'/api/control/visualizer', response.read())
                    # Artwork frames are bounded, in-memory, origin checked and
                    # tied to the applied settings revision.
                    frame = b'\xff\xd8artwork\xff\xd9'
                    upload = Request(base+'/api/control/visualizer-frame?revision='+selected['revision'],
                                     data=frame, headers={'Content-Type': 'image/jpeg', 'Origin': base})
                    with urlopen(upload) as response:
                        self.assertTrue(json.load(response)['accepted'])
                    with urlopen(base+'/api/visualizer/frame') as response:
                        self.assertEqual(response.headers['Content-Type'], 'image/jpeg')
                        frame_id = response.headers['X-Octet-Frame']
                        self.assertEqual(response.read(), frame)
                    with urlopen(base+'/api/visualizer/frame?after='+frame_id) as response:
                        self.assertEqual(response.status, 204)
                    self.assertTrue(state.read()['previewRequested'])
                    with patch('visualizer_state.time.monotonic', return_value=state.frame_seen+9):
                        self.assertIsNone(state.preview_frame())
                    stale = Request(base+'/api/control/visualizer-frame?revision=old', data=frame,
                                    headers={'Content-Type': 'image/jpeg', 'Origin': base})
                    with self.assertRaises(HTTPError) as rejected:
                        urlopen(stale)
                    self.assertEqual(rejected.exception.code, 409)
                    upload.headers['Origin'] = 'https://untrusted.example'
                    with self.assertRaises(HTTPError) as rejected:
                        urlopen(upload)
                    self.assertEqual(rejected.exception.code, 403)
                    request.headers['Origin'] = 'https://untrusted.example'
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request)
                    self.assertEqual(error.exception.code, 403)
            finally:
                http.shutdown()
                http.server_close()


if __name__ == '__main__':
    unittest.main()
