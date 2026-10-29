# WIL-252 Codex evidence

Base: `d38ee83923de65a14d630160e97530da1aea4865`

- `cd measure && python -m pytest tests/test_session.py` — passed: 12 tests in 10.31s.
- `cd measure && python -m pytest` — blocked by the environment: 159 passed and 2 failed because PortAudio is not installed; both failures in `tests/test_review_regressions.py` occur while importing `sounddevice` (`OSError: PortAudio library not found`).
- `cd measure && python -m build --wheel` — blocked by the environment: `/root/.pyenv/versions/3.12.13/bin/python: No module named build`.
