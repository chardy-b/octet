import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "octetmeasure" / "ui" / "static"


@pytest.fixture
def without_portaudio(monkeypatch):
    """Pretend sounddevice cannot be imported (PortAudio missing), then put everything back."""
    from octetmeasure import audio, capture
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    importlib.reload(capture)
    importlib.reload(audio)
    yield capture, audio
    monkeypatch.undo()
    importlib.reload(capture)
    importlib.reload(audio)


def test_missing_audio_library_gives_a_plain_message(without_portaudio):
    capture, audio = without_portaudio
    with pytest.raises(RuntimeError, match="Audio support is not available"):
        capture.list_devices()
    with pytest.raises(RuntimeError, match="PortAudio|portaudio|libportaudio|sounddevice"):
        audio.RealIO().devices()


def test_the_app_and_demo_mode_still_work_without_it(without_portaudio, workspace):
    from fastapi.testclient import TestClient

    from octetmeasure.ui.server import create_app
    with TestClient(create_app(demo=True)) as c:
        assert c.get("/api/info").json()["demo"] is True
        assert c.get("/api/devices").status_code == 200
    with TestClient(create_app(demo=False)) as c:
        r = c.get("/api/devices")
        assert r.status_code == 503 and "Audio support is not available" in r.json()["detail"]
        assert c.get("/api/measurements").status_code == 200             # everything that needs no audio still works


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_every_front_end_script_parses(tmp_path):
    for js in sorted(STATIC.rglob("*.js")):
        copy = tmp_path / (js.stem + ".mjs")                              # they are ES modules
        copy.write_text(js.read_text(encoding="utf-8"), encoding="utf-8")
        r = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
        assert r.returncode == 0, f"{js.name}: {r.stderr}"


def test_com_is_initialised_on_worker_threads_on_windows_and_ignored_elsewhere():
    """Opening a WASAPI stream on a worker thread needs COM there (found on real hardware). It must be a no-op elsewhere."""
    import threading

    from octetmeasure.audio import ensure_com
    results = []
    t = threading.Thread(target=lambda: results.append(ensure_com()))
    t.start()
    t.join()
    assert results == [sys.platform == "win32"]


def test_the_page_shell_points_at_files_that_exist():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for ref in ("/static/style.css", "/static/app.js"):
        assert ref in html and (STATIC / ref.split("/static/")[1]).exists()
    for name in ("util.js", "charts.js", "meter.js", "views/record.js", "views/recordings.js", "views/eq.js",
                 "views/calibration.js", "views/settings.js"):
        assert (STATIC / name).exists(), name
