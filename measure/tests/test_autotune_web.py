import copy
import json
from pathlib import Path
import socket

import numpy as np
import pytest
from scipy.signal import sosfreqz
import yaml

from octetmeasure import peq
from octetmeasure.autotune import config, dsp
from octetmeasure.autotune.camilla import export
from octetmeasure.autotune.web import Controller, check_safety, create_app

# Auto Tune DSP control is a Pi-only AF_UNIX integration. Measurement remains
# cross-platform; Windows has no socket.AF_UNIX to exercise the real endpoint.
unix_only = pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='requires AF_UNIX')


class UnixEndpoint:
    """Real AF_UNIX RFC6455 server standing in for the DSP proxy socket."""
    def __init__(self, directory, replies=None):
        import socket, threading
        self.path = str(directory / 'control.sock')
        self.replies, self.requests, self.handshakes = replies or {}, [], []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen(4)
        threading.Thread(target=self.serve, daemon=True).start()

    @staticmethod
    def exact(conn, count):
        data = b''
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            with conn:
                try:
                    self.session(conn)
                except (EOFError, OSError):
                    pass

    def session(self, conn):
        import base64, hashlib, struct
        head = b''
        while not head.endswith(b'\r\n\r\n'):
            head += self.exact(conn, 1)
        self.handshakes.append(head)
        key = next(l.split(b': ', 1)[1] for l in head.split(b'\r\n') if l.lower().startswith(b'sec-websocket-key'))
        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        conn.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        while True:
            first, second = self.exact(conn, 2)
            length = second & 127
            if length == 126:
                length = struct.unpack('!H', self.exact(conn, 2))[0]
            mask = self.exact(conn, 4)
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(self.exact(conn, length)))
            if first & 15 == 8:
                return
            request = json.loads(payload)
            name = request if isinstance(request, str) else next(iter(request))
            self.requests.append(request)
            body = json.dumps({name: self.replies.get(name, dict(result='Ok'))}).encode()
            conn.sendall(bytes([0x81, len(body)]) + body)


@pytest.fixture
def unix_dsp(monkeypatch):
    if not hasattr(socket, 'AF_UNIX'):
        pytest.skip('requires AF_UNIX')
    import tempfile
    from pathlib import Path
    from octetmeasure.autotune import camilla
    pytest.importorskip('websocket')
    dialed = []
    def refuse(address, *args, **kwargs):
        dialed.append(address)
        raise AssertionError(f'direct TCP dial: {address!r}')
    monkeypatch.setattr(socket, 'create_connection', refuse)
    # websocket-client's own TCP path goes through the same helpers; none may run.
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: dialed.append(a) or (_ for _ in ()).throw(AssertionError('resolve')))
    with tempfile.TemporaryDirectory() as short:   # AF_UNIX paths are length-limited
        yield from _serve(Path(short), monkeypatch, camilla, dialed)


def _serve(tmp_path, monkeypatch, camilla, dialed):
    endpoint = UnixEndpoint(tmp_path, {'GetState': dict(result='Ok', value='Running'),
                                        'SetMute': dict(result='Error')})
    monkeypatch.setattr(camilla, 'DSP_SOCKET', endpoint.path)
    endpoint.dialed = dialed
    yield endpoint
    endpoint.listener.close()


def test_camilla_control_uses_unix_websocket_never_tcp(unix_dsp):
    from octetmeasure.autotune.camilla import control
    assert control([('GetState', None), ('GetState', None)]) == ['Running', 'Running']
    assert unix_dsp.requests == ['GetState', 'GetState']
    assert unix_dsp.handshakes[0].startswith(b'GET / HTTP/1.1\r\n')
    assert unix_dsp.dialed == []


def test_camilla_control_keeps_rejection_error(unix_dsp):
    from octetmeasure.autotune.camilla import control
    with pytest.raises(RuntimeError, match='CamillaDSP rejected SetMute'):
        control([('SetMute', True)])


@unix_only
def test_camilla_control_missing_socket_fails_closed(tmp_path, monkeypatch):
    import socket
    from octetmeasure.autotune import camilla
    dialed = []
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **k: dialed.append(a))
    monkeypatch.setattr(camilla, 'DSP_SOCKET', str(tmp_path / 'absent.sock'))
    with pytest.raises(OSError):
        camilla.control([('SetMute', False)])
    assert dialed == []


def test_camilla_control_production_socket_path():
    from octetmeasure.autotune import camilla
    assert camilla.DSP_SOCKET == '/run/octet-dsp/control.sock'


def test_camilla_control_closes_websocket_without_context_manager(unix_dsp):
    import websocket
    from octetmeasure.autotune.camilla import control
    created = []
    real = websocket.create_connection
    def spy(*a, **k):
        created.append(real(*a, **k))
        return created[-1]
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(websocket, 'create_connection', spy)
        assert control([('GetState', None)]) == ['Running']
    assert created and not created[0].connected


@unix_only
def test_camilla_control_closes_unix_socket_when_handshake_fails(monkeypatch):
    import socket, tempfile, threading, types
    from pathlib import Path
    from octetmeasure.autotune import camilla
    pytest.importorskip('websocket')
    made = []
    class Tracked(socket.socket):
        closed = False
        def close(self):
            self.closed = True
            super().close()
    def factory(*a, **k):
        made.append(Tracked(*a, **k))
        return made[-1]
    shim = types.SimpleNamespace(AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM, socket=factory)
    with tempfile.TemporaryDirectory() as short:
        path = str(Path(short) / 'bad.sock')
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        def refuse():
            conn, _ = listener.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
        threading.Thread(target=refuse, daemon=True).start()
        monkeypatch.setattr(camilla, 'socket', shim)
        monkeypatch.setattr(camilla, 'DSP_SOCKET', path)
        with pytest.raises(Exception):
            camilla.control([('GetState', None)])
        listener.close()
    assert len(made) == 1 and made[0].closed


def candidate():
    roles = ('sub', 'a', 'b', 'tweeter')
    return dict(strategy='both', order=4, low=400, high=4200, split=1000,
                delays=dict.fromkeys(roles, 0.), gains=dict.fromkeys(roles, 0.),
                polarity=dict.fromkeys(roles, 1), score=0)


def document():
    cfg = config.example()
    chosen = {s: candidate() for s in ('L', 'R')}
    eq = {d['id']: [] for d in cfg['drivers']}
    return cfg, chosen, eq


def test_tweeter_floor_enforced_in_profile_stimulus_and_export():
    cfg, chosen, eq = document()
    bad = next(d for d in cfg['drivers'] if d['role'] == 'tweeter')
    bad['min_hz'] = 100
    with pytest.raises(ValueError, match='Tweeter'):
        config.validate(cfg)
    with pytest.raises(ValueError, match='Tweeter'):
        dsp.protected_sweep(bad, cfg, -40)
    with pytest.raises(ValueError, match='Tweeter'):
        export(cfg, chosen, eq, {})


def test_named_safety_guard_export_matches_render_with_room_eq():
    cfg, chosen, eq = document()
    room = {'L': [dict(kind='PK', fc=500., gain_db=-4., q=1.)]}
    doc, _ = export(cfg, chosen, eq, room)
    check_safety(doc, limits=True)
    f = np.geomspace(50, 20000, 600)
    for d in cfg['drivers']:
        expected = dsp.branch_sos(cfg, d, chosen[d['side']], [])
        if d['side'] in room:
            expected = np.vstack([peq.sos_for([peq.Peq(**p) for p in room[d['side']]], cfg['fs']), expected])
        expected = sosfreqz(expected, worN=f, fs=cfg['fs'])[1]
        actual = np.ones(len(f), dtype=complex)
        stage = next(s for s in doc['pipeline'] if s.get('channels') == [d['output'] - 1])
        for name in stage['names']:
            fil = doc['filters'][name]; p = fil['parameters']
            if fil['type'] == 'BiquadCombo':
                actual *= dsp.fast_lr(f, p['freq'], 'highpass', p['order'], cfg['fs'])
            elif fil['type'] == 'Biquad':
                actual *= sosfreqz([[p['b0'], p['b1'], p['b2'], 1, p['a1'], p['a2']]], worN=f, fs=cfg['fs'])[1]
        assert np.allclose(actual, expected, atol=1e-10)
    unsafe = copy.deepcopy(doc)
    unsafe['filters']['L_tweeter_safety_hp']['parameters']['freq'] = 100
    with pytest.raises(ValueError, match='safety'):
        check_safety(unsafe)


def test_automatic_run_saves_and_recalls_complete_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(dsp, 'search', lambda *a, **k: [candidate()])
    controller = Controller(tmp_path, demo=True)
    cfg = config.example()
    cfg.update(duration=1, peq_bands=1)
    for driver in cfg['drivers']:
        driver.update(start_db=-30, max_db=-30)
    controller.runner.set_profile(yaml.safe_dump(cfg))
    with pytest.raises(ValueError, match='microphone'):
        controller.start({})
    controller.start(dict(microphone_ready=True, name='Desk'))
    controller.runner.thread.join(180)
    state = controller.status()
    assert not state['running']
    assert state['status'] == 'applied', state['message']
    assert not state['audio_owned']
    assert len(state['profiles']) == 1
    saved = state['profiles'][0]
    assert saved['name'] == 'Desk'
    assert saved['chosen']['L']['low'] == 400
    controller.activate(saved['id'])
    assert controller.status()['active']['id'] == saved['id']
    Path(saved['config_path']).write_text('filters: {}')
    with pytest.raises(ValueError, match='changed'):
        controller.activate(saved['id'])


def test_api_rejects_arbitrary_actions_and_profile_paths(tmp_path):
    from fastapi.testclient import TestClient
    with TestClient(create_app(tmp_path, demo=True)) as client:
        assert client.get('/api/auto').json()['available']
        assert client.post('/api/auto/select', json={'id': '../../etc/passwd'}).status_code == 400
        assert client.post('/api/auto/shell', json={}).status_code == 400
        assert client.post('/api/auto/start', json={}).status_code == 400


def test_recall_selects_backup_without_writing_shared_manual_config(tmp_path, monkeypatch):
    import octetmeasure.autotune.web as web
    cfg, chosen, eq = document()
    doc, _ = export(cfg, chosen, eq, {})
    backup = tmp_path/'backup.yml'
    original = tmp_path/'manual.yml'
    statefile = tmp_path/'state.yml'
    backup.write_text(yaml.safe_dump(doc))
    original.write_text('Original manual configuration must stay untouched')
    statefile.write_text('config_path: previous.yml\n')
    cfg['camilla']['statefile'] = str(statefile)
    class Backend:
        def __init__(self, cfg): pass
        def take_dac(self): pass
        def systemctl(self, action, service=None): pass
        def master(self, volume, muted):
            assert volume == -36 and muted
    monkeypatch.setattr(web, 'Hardware', Backend)
    monkeypatch.setattr(web.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(web.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(web, 'control', lambda *a: [str(backup), 'Running'])
    controller = Controller(tmp_path/'runs', demo=False)
    controller._activate_hardware(cfg, dict(kind='manual', config_path=str(backup), original_path=str(original)), restore=True)
    assert yaml.safe_load(statefile.read_text())['config_path'] == str(backup)
    assert original.read_text() == 'Original manual configuration must stay untouched'
