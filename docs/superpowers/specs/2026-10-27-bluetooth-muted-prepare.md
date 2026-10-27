# WIL-249: muted candidate preparation

`BluetoothRoute.prepare(rate, format)` serializes with `server.LOCK`, verifies
physical mute, holds the existing shared Auto Tune `audio.lock` flock, and requires
the exact loaded, active approved 129-filter stereo Tidal baseline. The existing
Bluetooth snapshot and exclusive durable journal remain separate from AirPlay's
files. Candidate construction preserves filters, pipeline and DAC configuration;
only capture sub2 and its asynchronous rate adjustment settings change.

The candidate is durably written and passed to the real
`/opt/camilladsp/camilladsp --check` subprocess (bounded, shell-free). Failure or a
missing binary is fatal. Ownership, approval, journal, original baseline, mute and
candidate bytes are rechecked around the DSP mutation. The journal is durable
before either `SetConfigFilePath` or `Reload`. Success requires exact loaded path
and config, Running and verified mute. Failure attempts mute again and retains
recovery evidence and the flock whenever a journal exists. Recovery continues to
restore the original approved baseline; no original baseline is overwritten.

`BridgeSupervisor(..., prepare=route.prepare)` calls the trusted callback once on
the first supported, bridge-owned playback sub2 observation. Capture may still be
closed. It discards pre-callback observations and rechecks identity, child and the
startup deadline. Both endpoints must then match, remain live and show advancing
pointers before readiness. Changed playback negotiation, missing capture, callback
failure and expired deadlines fail closed. The callback must be bounded; Python
cannot safely preempt arbitrary in-process callbacks. Omission preserves the
existing pre-prepared-route embedding contract.

(Historical slice record. `start()` and its watchdog were later implemented; see
`2026-10-27-bluetooth-manual-receiver.md`.) At the time of this slice it remained
standalone infrastructure. `BluetoothControl.start()` was disabled;
its existing baseline watchdog is not a candidate playback watchdog. No service,
server activation, unmute, volume change, Pi access, commit or push is included.
As in the existing AirPlay pattern, filesystem race safety assumes protected local
state directories and cooperative writers using the shared lock. No remote or
hostile directory writer is authorized by these APIs.

The supplied `.git` references an unavailable host worktree. `3abc6bc` could not be
independently verified; Git metadata was left untouched. Dependencies for tests
were installed in `/tmp/wil249-venv`, not production services. See verification
results below.

## Verification in this sandbox

- Remote workflow command: 214 tests passed. After adding the last four regression
  cases, the final prepare/supervisor subset passed all 29 tests. Tests cover
  candidate/approval/DSP/Auto Tune/AirPlay races, exclusive journal retention,
  journal directory-sync failure, interrupted reload, crash recovery, native PCM
  rejection, callback ordering, missing capture and startup deadlines. Existing
  cross-process flock and AirPlay recovery tests also passed.
- Both workflow Node UI scripts and four additional visualizer scripts passed.
- Full remote discovery: 242 passed, one existing visualizer failure. Its video is
  a 134-byte Git LFS pointer, while the test expects a 1024-byte HTTP range.
- Measurement workflow on Linux/Python 3.12: 157 passed, two failed solely at
  `import sounddevice` because the sandbox lacks PortAudio. The workflow normally
  installs `libportaudio2`. No audio hardware or service was accessed.
- Wheel built successfully; all 51 entries inspected and bundled UI index found.
- Five browser UI scripts attempted, blocked by absent Playwright; their required
  Microsoft Edge executable is also absent. No browser pass is claimed.
- CamillaDSP is absent at `/opt/camilladsp/camilladsp`. The runtime calls the real
  binary and fails closed when missing. Unit tests mock the subprocess and DSP;
  they do not constitute a real Camilla candidate check or hardware validation.
- The Windows/macOS and Python 3.10 CI matrix cannot be reproduced in this Linux
  Python 3.12 sandbox. Git worktree metadata remains unavailable.

Test logs are in `/tmp/wil249-remote-ci.log`,
`/tmp/wil249-remote-discovery.log`, `/tmp/wil249-new-tests.log`,
`/tmp/wil249-build.log` and `/tmp/wil249-test_*.cjs.log`.
