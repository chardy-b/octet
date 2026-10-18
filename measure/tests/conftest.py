import pytest

from octetmeasure import store


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throw-away data folder and settings folder, so tests never touch real measurements or settings."""
    monkeypatch.setenv("OCTET_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(store, "ROOT", tmp_path / "measurements")
    monkeypatch.setattr(store, "CLOCK_FILE", tmp_path / "measurements" / "clock.json")
    (tmp_path / "measurements").mkdir()
    return tmp_path
