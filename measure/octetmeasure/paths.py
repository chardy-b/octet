"""Where the app keeps its settings and measurements, on Windows, macOS and Linux.

Settings:      ~/.octetmeasure/config.json   (or $OCTET_HOME/config.json)
Measurements:  $OCTET_DATA, else the "data_dir" setting, else a ./measurements folder next to the source checkout
               if one already exists, else ~/OctetMeasure/measurements
"""
import json
import os
from pathlib import Path


def app_dir() -> Path:
    return Path(os.environ.get("OCTET_HOME") or (Path.home() / ".octetmeasure"))


def config_path() -> Path:
    return app_dir() / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(**updates) -> dict:
    cfg = load_config()
    cfg.update(updates)
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def data_dir() -> Path:
    env = os.environ.get("OCTET_DATA")
    if env:
        return Path(env).expanduser()
    cfg = load_config().get("data_dir")
    if cfg:
        return Path(cfg).expanduser()
    checkout = Path(__file__).resolve().parent.parent / "measurements"
    if checkout.exists():
        return checkout
    return Path.home() / "OctetMeasure" / "measurements"
