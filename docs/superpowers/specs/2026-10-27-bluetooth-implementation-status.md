# WIL-249 second implementation slice — 2026-10-27

**Playback remains disabled.** This is local Docker-sandboxed Codex work entirely
under `/workspace`. No Pi connection, host edit, install, reboot, push, service
activation or physical audio occurred. The supplied `.git` points outside the
container to an unavailable worktree. The requested WIP `567a142`, parent AirPlay
`118ace0`, could not be independently verified. Git metadata was left untouched;
the accompanying patch is relative to the supplied workspace contents.

## Fixed hardware regression

Both Debian BlueALSA 4.3.1-3 binaries must return stdout **exactly `v4.3.1\n`**
(one trailing LF), empty stderr, rc0 for `--version`. No stripping or newline
normalization is allowed. `--help` must return reviewed capabilities on stdout,
empty stderr, rc0. The daemon option is `--profile=a2dp-sink`; player option is
`--profile-a2dp`. `--profile=A2DP` is not accepted as a substitute.

A regression fixture using the supplied real CLI stdout and missing sub2 yields
exactly **one blocker**, the dedicated loopback endpoint prerequisite. This is
not a genuine Pi preflight run; the hardware still needs that read-only check.

## Implemented recovery foundation

`bluetooth_route.py` reuses `BaselineRoute`'s existing shared `audio.lock` inode
lease and inode validation. The fd stays in the owning process until successful
recovery; it is never transferred over a socket or replaced. Preparation rejects
AirPlay active/pending/recovery, Auto Tune ownership or unknown status, absent or
contended lock, unapproved baseline, mismatched loaded path/content, non-Running
DSP, non-Tidal stereo topology, non-eight-output DAC and non-129-filter baseline.

Preparation verifies mute, preserves original path/SHA256 and exact bytes
(including comments) in a separate read-only snapshot, then durably writes a
Bluetooth journal with a unique generation. It does **not** load a candidate or
start playback. No protected config stages are edited. The earlier pure candidate
builder still supports only 44.1/48 kHz S16_LE/S32_LE stereo and sub2 capture.

Recovery verifies mute before any path mutation, holds the shared lock, reloads
the exact original path and verifies original byte digest/approval, loaded content,
Running and mute. Changed original files or approval are refused rather than
silently overwriting another baseline; recovery evidence remains. Failed recovery
retains the journal and any acquired lock. The snapshot remains after successful
recovery; the journal is removed last. No recovery path unmutes.

`bluetooth_control.py` provides explicit startup recovery and a callable fail-closed
watcher for baseline drift and stale generations. Its injected stop callback must
stop only a supervisor-owned bridge. Unreachable DSP still invokes that callback
and never reports verified physical mute. Every start request is disabled; stale
start/stop events cannot unmute. It has no pairing, bridge launch, volume setting,
MPD mutation, server registration, worker thread, service unit or LAN endpoint.
**Restart recovery is implemented as a method, not installed boot behavior.**

## Remaining activation blockers

- No trusted local BlueZ identity/MAC policy loader or live Paired AND Trusted
  verification exists. The pure argv helper is not authorization. Every playback
  request remains rejected regardless of supplied MAC.
- No owned bridge supervisor, disconnect/receiver-stop observer, fresh progress
  observation on both sub2 endpoints, native format/rate verification or deadlines.
  Future bridge argv must be explicit MAC, `--profile-a2dp`,
  `--pcm=hw:Loopback,0,2`, `--volume=software`, `--single-audio`.
- No real CamillaDSP `--check` execution or candidate load path in this component.
  No MPD pause/queue preservation integration, bounded −18 dB volume or unmute gate.
- No production integration with server.LOCK, AirPlay hook arbitration (including
  the CHANNEL_PROFILE path), remote restart or systemd stop dependencies. Methods
  are designed for the existing process control lock; a shared flock alone does
  not establish a complete cross-process AirPlay handoff. No socket handshake was
  invented. The watcher must not be installed or called as a playback controller.
- Missing physical sub2 remains a maintenance prerequisite. Staged modprobe data
  is not consumed automatically. Keep Bluetooth services masked/inactive and all
  existing AirPlay/MPD defaults unchanged. No generic LAN pairing/auto-pairing API.

## Local verification

PyYAML 6.0.2 CPython 3.12 manylinux wheel was downloaded and SHA256 verified against
PyPI metadata, then extracted into `/workspace/.wil249-review/deps` without an
installation. Tests use `TMPDIR=/workspace/.wil249-review`.

From `/workspace/pi/remote`:

```sh
PYTHONPATH=/workspace/.wil249-review/deps TMPDIR=/workspace/.wil249-review python3 -m unittest test_bluetooth_contract test_bluetooth_preflight test_bluetooth_control test_airplay test_audio_sources test_profile_rollback test_install_airplay test_server test_network_access test_visualizer_state test_output_controls test_active_settings test_driver_pairs test_input_levels
node test_airplay_ui.cjs
node test_browser_cache.cjs
```

**Results: 157 Python tests passed; both UI smoke scripts passed, including six
browser cache checks.** Full Python output is in `.wil249-review/python-tests.txt`.

Fault injection covers startup mute, failed mute, journal-write failure, restart
recovery, real second-process flock contention in both directions, running-config
drift, stale start/stop/tick, failed reload, changed snapshot/baseline/lock inode,
AirPlay/Auto Tune ownership and unreachable DSP. Assertions forbid unmute and
volume changes. Synthetic 129-filter fixtures are not Pi acceptance. Installer
tests are mocked; their “AirPlay enabled” text is not an actual installation.

No real Camilla binary validation, BlueZ/ALSA negotiation, phone playback,
hardware listening or Pi acceptance is claimed. There is no activation command.
