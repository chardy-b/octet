# WIL-249 third slice: recovery and read-only identity gate

**Playback remains disabled. This is not a working manual Bluetooth receiver.**
`BluetoothControl.start()` rejects every request, even with an enabled local policy.
No installer, service, HTTP endpoint, socket, worker, pairing/connect/trust mutation,
volume change or unmute was added.

The supplied `.git` points to an unavailable host worktree. Commit `16659aa` and
parent `118ace0` could not be independently verified. Git metadata was untouched;
the patch compares against supplied files. No Pi, production `/var/lib`, audio
device, general `setup.sh`, AirPlay implementation or baseline config was changed.
No commit, push or restart occurred.

## Recovery review

`fail()` holds the process lock and independently captures mute, owned bridge-stop
and recovery errors. Stop failure no longer skips recovery. An initial mute/stop
failure prevents cleanup even after a verified muted restore. Recovery failures
retain the journal and acquired flock. Errors identify all failed stages.

Journal validation requires exact keys, integer version 1, lowercase 64-hex SHA256
and lowercase 32-hex generation. Duplicate keys, oversized/nonobject records,
symlinks and nonregular files fail closed. Every read checks disk bytes and inode,
even with a cached record. Journal publication is exclusive and cannot overwrite
another recovery record. Recovery rechecks approval and original bytes before path
mutation and after reload; it never writes to the original baseline.

Cleanup rechecks the loaded baseline, Running and mute, and verifies the journal
inode immediately before unlink. The lock stays held through directory fsync.
On cleanup fsync failure, exclusive recreation of evidence is attempted; the
record and lock remain held. If storage also rejects recreation, on-disk evidence
cannot be guaranteed. The failure propagates without releasing the lock.

Emergency mute deliberately precedes journaling and is attempted even when lock,
ownership, approval or filesystem access is unavailable. Waiting for a journal
before silencing speakers violates the mute-first invariant. DSP path mutation
still requires verified mute, the shared lock and durable recovery data. No failed
restore, changed baseline or unknown DSP can cause unmute.

POSIX has no conditional inode-checked unlink. These checks detect observed races;
correct operation additionally requires a protected local state directory and
cooperative writers using `audio.lock`. They do not defend stat/unlink against a
hostile directory writer. The future guarded installer must establish this boundary.

The test fixture now uses `Path(self.tmp.name).resolve()`. A fresh subprocess
exercises recovery with symlinked TMPDIR; no test hardcodes a workspace path or
creates a nested Git checkout.

## Read-only identity prerequisite

`bluetooth_identity.py` is unregistered and cannot enable start. `local_policy()`
defaults to `/etc/octet/bluetooth.json`. It rejects non-root callers, missing or
disabled policy, noncanonical/symlink paths, unprotected ancestors, non-root files,
non-0600 permissions, oversized/changed files, duplicate keys and invalid MACs.
The exact schema is version 1, boolean enabled true, and addresses containing
1–16 unique canonical explicit MACs. No policy file was installed or written.
An enabled policy is a prerequisite, never sufficient playback authorization.

`verify_identity()` performs bounded, shell-free `/usr/bin/busctl` reads of
`org.bluez.Device1` on `/org/bluez/hci0/dev_<explicit MAC>`. Address, Adapter,
Paired, Trusted and Connected must match exact typed stdout twice, with rc0 and
empty stderr. Names and connection alone cannot authorize a device. This is a
point-in-time observation, not an atomic identity lease or BlueZ-owner watchdog.
Tests mock the bus and administrator metadata; no live bus is contacted.

## Exact remaining activation gates

1. Guarded installer, verified protected state directory and disabled-by-default
   policy integrated with an authenticated local-only controller. No remote HTTP
   pairing or activation. The new read-only helper is not that controller.
2. Owned, bounded-lifetime supervision of only
   `bluealsa-aplay --profile-a2dp --pcm=hw:Loopback,0,2 --volume=software --single-audio <MAC>`;
   BlueZ bus-owner, identity/disconnect and daemon/process-loss monitoring.
3. Verified mute and MPD pause without queue loss before mutation; no auto-resume.
4. Actual pcm0p/sub2 negotiation restricted to 44100/48000, S16_LE/S32_LE stereo;
   matching live pcm1c/sub2, advancing pointers on both endpoints and output/stall
   deadlines. The supplied Pi still needs its separately attended maintenance
   restart for sub2. No restart was attempted.
5. Runtime candidate construction preserving all 129 active filters, real
   `/opt/camilladsp/camilladsp --check`, exact loaded path/config and Running,
   volume capped at −18 dB, then a single deliberate guarded unmute.
6. Complete watchdog/startup/shutdown integration for stale generation, DSP drift,
   remote restart, Auto Tune and AirPlay arbitration including CHANNEL_PROFILE.
   The existing tick only verifies the muted original baseline.

The user-reported BlueALSA 4.3.1 CLI and CamillaDSP 4.1.3 candidate checks remain
hardware evidence, not runtime validation of this component. No automatic pairing,
reconnection, unmute or service enabling was introduced.

## Verification

The workflow includes the new identity suite. With PyYAML available, from
`pi/remote`:

```sh
python3 -m unittest test_bluetooth_contract test_bluetooth_preflight test_bluetooth_identity test_bluetooth_control test_airplay test_audio_sources test_profile_rollback test_install_airplay test_server test_network_access test_visualizer_state test_output_controls test_active_settings test_driver_pairs test_input_levels
node test_airplay_ui.cjs
node test_browser_cache.cjs
```

The Bluetooth suite passes 57 tests. Fault injection covers independent mute,
stop and restore failures, malformed/changed journals, symlinks, publication,
unlink/fsync failure and retry, lock retention through sync, cleanup-time swaps,
unknown/unmuted DSP, changed baselines, real cross-process flock contention and
symlink TMPDIR. Identity tests cover policy rejection, exact argv/properties,
disconnect between observations, non-admin/unlisted callers and bus failures.

The full remote CI command passes 183 Python tests and both UI scripts pass.
Four additional visualizer Node scripts pass. Broader Python discovery has one
unrelated failure: `test_visualizer.Visualizer.test_http_assets_and_navigation_and_host_validation`
expects 1024 video bytes, but this checkout has a 134-byte Git LFS pointer. The
media and test were left unchanged. Playwright/MS Edge browser scripts outside
the CI smoke command were not run; those dependencies are absent.

PyYAML was extracted into temporary test dependencies from a SHA256-verified
wheel. No production packages/services were installed. Installer test messages
saying “AirPlay enabled” are from mocks, not actual activation.
