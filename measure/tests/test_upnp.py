import re
import threading
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

from octetmeasure import upnp
from octetmeasure.wavio import write_pcm_wav

DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<friendlyName>Fake WiiM</friendlyName><modelName>Fake Amp</modelName>
<serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
<controlURL>/ctl</controlURL></service></serviceList></device></root>"""


class FakeRenderer:
    def __init__(self):
        self.actions = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(DESC.encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                act = self.headers["SOAPACTION"].strip('"').split("#")[1]
                outer.actions.append((act, body))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<ok/>")

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.location = f"http://127.0.0.1:{self.srv.server_port}/desc.xml"

    def close(self):
        self.srv.shutdown()


def test_describe_and_play_sequence(tmp_path):
    fake = FakeRenderer()
    try:
        r = upnp._describe(fake.location)
        assert r.name == "Fake WiiM" and r.control_url.endswith("/ctl")
        upnp.play(r, "http://192.168.1.5:9999/sweep.wav & more")
        names = [a for a, _ in fake.actions]
        assert names == ["Stop", "SetAVTransportURI", "Play"]
        set_uri = fake.actions[1][1]
        assert "&amp;" in set_uri                       # URL and metadata are XML-escaped
        assert "http://192.168.1.5:9999/sweep.wav" in set_uri
        assert "audio/wav" in set_uri
    finally:
        fake.close()


def test_file_server_serves_ranges(tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(bytes(range(256)) * 40)
    srv = upnp.FileServer(f, "127.0.0.1")
    try:
        full = urllib.request.urlopen(srv.url).read()
        assert full == f.read_bytes()
        req = urllib.request.Request(srv.url, headers={"Range": "bytes=10-19"})
        resp = urllib.request.urlopen(req)
        assert resp.status == 206 and resp.read() == f.read_bytes()[10:20]
        assert resp.headers["Content-Range"] == f"bytes 10-19/{f.stat().st_size}"
        req = urllib.request.Request(srv.url, headers={"Range": "bytes=-5"})
        assert urllib.request.urlopen(req).read() == f.read_bytes()[-5:]
        assert len(srv.requests) == 3
    finally:
        srv.close()


def test_wav_writer_roundtrip(tmp_path):
    y = np.sin(2 * np.pi * 440 * np.arange(4800) / 48000) * 0.5
    for bits in (16, 24, 32):
        p = tmp_path / f"t{bits}.wav"
        write_pcm_wav(p, 48000, y, bits)
        with wave.open(str(p)) as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, bits // 8, 48000)
            raw = w.readframes(w.getnframes())
        if bits == 24:
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
            v = (b[:, 0].astype(np.int32) | b[:, 1].astype(np.int32) << 8 | b[:, 2].astype(np.int32) << 16)
            v = (v ^ 0x800000) - 0x800000
            decoded = v[::2] / 8388607
        elif bits == 16:
            decoded = np.frombuffer(raw, "<i2")[::2] / 32767
        else:
            decoded = np.frombuffer(raw, "<i4")[::2] / 2147483647
        assert np.allclose(decoded, y, atol=2 / 2 ** (bits - 1))


def test_stereo_playback_channels():
    from octetmeasure.sweep import make_sweep, stereo_playback
    sw = make_sweep(48000, duration=1.0)
    left = stereo_playback(sw, "left", 0.5, 0.5)
    right = stereo_playback(sw, "right", 0.5, 0.5)
    both = stereo_playback(sw, "both", 0.5, 0.5)
    assert np.abs(left[:, 1]).max() == 0 and np.abs(left[:, 0]).max() > 0
    assert np.abs(right[:, 0]).max() == 0 and np.abs(right[:, 1]).max() > 0
    assert np.array_equal(both[:, 0], both[:, 1]) and np.array_equal(both[:, 0], left[:, 0])


def test_stereo_playback_invert():
    from octetmeasure.sweep import make_sweep, stereo_playback
    sw = make_sweep(48000, duration=1.0)
    flipped = stereo_playback(sw, "both", 0.5, 0.5, invert="right")
    assert np.array_equal(flipped[:, 1], -flipped[:, 0]) and np.abs(flipped[:, 0]).max() > 0
    assert np.array_equal(stereo_playback(sw, "both", 0.5, 0.5, invert="left")[:, 0], flipped[:, 1])
