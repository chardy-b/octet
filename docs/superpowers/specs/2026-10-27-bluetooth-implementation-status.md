# WIL-249 implementation status — 2026-10-27

**Incomplete; Bluetooth must remain disabled.** This is a bounded, offline
contract/preflight slice, not an installed receiver. No Pi connection, deployment,
installer execution, reboot, commit, push or PR was performed. The provided
worktree's `.git` points to an unavailable host path; branch `wil-249-bluetooth-bluealsa`
and base `e601df0` could not be independently verified. Git metadata was untouched.

## Reviewable changes

- Preflight requires exact BlueALSA `4.3.1` output and reviewed `--help` capabilities,
  plus both idle, correctly identified sub2 endpoints. It always reports
  `available=false` and `advertise=false`. It is not an activation token.
- Pure argv preview requires one explicit allowlisted MAC; rejects wildcard,
  missing and malformed addresses. Uses `--profile-a2dp`,
  `--pcm=hw:Loopback,0,2`, `--volume=software`, `--single-audio`.
  It never executes the command, verifies pairing/trust, or starts services.
- Pure candidate builder accepts only native 44100/48000 Hz, S16_LE/S32_LE stereo,
  captures sub2 and preserves all supplied filters, pipeline, mixers, output
  settings and eight-output explicit hardware playback. No implicit conversion,
  default/plughw fallback or generic Bluetooth source/UI/API was added.
  This does not authenticate baseline approval or validate Camilla syntax.
- Staged `pi/etc/bluetooth-staged/snd-aloop.conf` provisions three substreams when
  deliberately copied during future maintenance. Existing setup/install paths
  remain unchanged and do not consume it.

CLI spellings match the user-verified Debian 4.3.1-3 installation and upstream
[v4.3.1 player option parser](https://github.com/arkq/bluez-alsa/blob/v4.3.1/utils/aplay/aplay.c)
and [daemon option parser](https://github.com/arkq/bluez-alsa/blob/v4.3.1/src/main.c).
No Debian executable was available or executed in Cloud. Help/version checks
are deliberately strict and cannot prove codec or D-Bus readiness.

## Cloud verification

From `pi/remote`, Python 3.12:

```sh
PYTHONPATH=/tmp/wil249-test-deps python3 -m unittest test_bluetooth_contract test_bluetooth_preflight test_airplay test_audio_sources test_profile_rollback test_install_airplay test_server test_network_access test_visualizer_state test_output_controls test_active_settings test_driver_pairs test_input_levels
node test_airplay_ui.cjs
node test_browser_cache.cjs
```

- Full Python command: **141 tests passed** before adding one additional
  preflight rejection regression. Final changed Bluetooth suites rerun:
  **16 tests passed** (`test_bluetooth_contract test_bluetooth_preflight`).
- Both browser scripts passed, including six browser cache checks.
- Initial attempts failed due to wrong working directory, absent PyYAML, and a
  software-option token boundary bug. Corrected directory and validator;
  PyYAML 6.0.2 wheel extracted into `/tmp/wil249-test-deps` after SHA256 verification
  against PyPI metadata; no system/repo dependency installation. CI already installs
  PyYAML and now includes the new contract suite.
- Existing installer tests use mocks/temp files; their “AirPlay enabled” output
  is not a deployment. No live installer ran.
- Preservation uses a synthetic 129-filter fixture, not the actual Pi baseline.
  No real Camilla `--check`, BlueALSA/ALSA negotiation, hardware listening, or
  cross-platform measurement suite was run. Prior AirPlay listening remains unverified.

## Blocking work before activation

No Bluetooth lifecycle controller, installer, rollback command, service overrides,
BlueZ pairing agent or live stream observer exists in this slice. Therefore no
Bluetooth mute-first/recovery, Auto Tune/AirPlay races, restart, service-loss,
stale-event or codec integration behavior is claimed tested. Existing AirPlay
regressions passing does not establish Bluetooth safety.

Required follow-up: shared control-lock arbitration (AirPlay wins), existing
Auto Tune `audio.lock` lease, MPD pause without resume, approved original
path/digest journal durable before mutation, mute-first crash recovery with
journal retained on failure, fresh stream generations/progress deadlines,
service/D-Bus identity and paired/trusted allowlisted phone checks, exact both-end
PCM params/progress, approved loaded path/config and real Camilla candidate
`--check`, -18 dB initial limit, no boot/reconnect auto-unmute, failure mute/restore,
and deterministic manual override. Keep actual 129-filter Tidal baseline,
eight outputs, all gains/EQ/crossover/mutes; never substitute CHANNEL_PROFILE.

Implement and test local-admin-only bounded pairing, normally non-discoverable
and non-pairable, no LAN pairing API, and real systemd dependency/stop coordination.
Installer must snapshot and restore managed files/states without ever starting
vendor bare aplay, keep both vendor units masked until all guards pass, refuse
live module changes, and require actual third endpoints. Add fault-injection
coverage and real Pi acceptance before exposing Bluetooth.

## Safe deployment boundary and rollback

There is **no safe Bluetooth activation command for this revision**. Keep current
`bluealsa.service` and `bluealsa-aplay.service` masked/inactive. Do not run the
preview argv or the general setup script as a Bluetooth upgrade.

Future separately approved maintenance prerequisite only (not authorization here):

1. Record masks, current approved baseline path/SHA256 and existing modprobe file;
   retain a byte-for-byte backup. Mute and verify mute before maintenance.
2. Replace `/etc/modprobe.d/snd-aloop.conf` with the staged file, checking other
   modprobe files for conflicting options. Do not unload/reload live snd_aloop.
3. Perform the separately approved maintenance reboot. Confirm both vendor units
   are still masked/inactive. Read both `/proc/asound/Loopback/pcm0p/sub2/info`
   and `pcm1c/sub2/info` and statuses; run the read-only preflight. Its pass is
   insufficient to enable Bluetooth. Verify existing Tidal/AirPlay routing and
   approved baseline before any deliberate listening action.
4. Stop here until the controller, guarded installer/rollback and physical
   acceptance gates above have been implemented and reviewed.

This repo-only slice installed nothing, so there is nothing to uninstall on the Pi.
If the staged modprobe file is used in later maintenance, rollback means restoring
its recorded original bytes and taking a separately approved muted reboot; never
unload the live module. Preserve Tidal/AirPlay files and both Bluetooth masks.
