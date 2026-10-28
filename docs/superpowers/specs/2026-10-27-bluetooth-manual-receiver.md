# WIL-249: manual BlueALSA A2DP receiver (software slice)

**Software path implemented and unit-tested; no phone has played through it.**
The Pi's third loopback substream **has been provisioned and tested** on the Pi
(operator-reported; see "Hardware state" below). These software sessions had no Pi,
BlueZ, BlueALSA, ALSA device, CamillaDSP binary or phone access. Nothing was
installed, restarted, pushed or committed. Bluetooth stays disabled until an
administrator installs it and edits a root-owned policy during an attended session.

## Hardware state (2026-10-27)

- **Loopback sub2: provisioned and tested on the Pi.** The operator reports that
  `snd-aloop` on the Pi now has the third substream, and that both dedicated
  endpoints (`pcm0p/sub2`, `pcm1c/sub2`) were tested. The Pi was not reachable from
  this session, so that result was not re-observed here, and no log of it is
  in this repository. Earlier notes (and the previous revision of this file) saying
  sub2 was missing, or that a maintenance restart was still pending, are outdated.
- **Repo setup still says two substreams.** `pi/install/setup.sh` and
  `pi/etc/modprobe.d-snd-aloop.conf` still write `pcm_substreams=2`;
  `pi/etc/bluetooth-staged/snd-aloop.conf` has `3`. Re-running `setup.sh` on the
  provisioned Pi would remove sub2 at the next boot. Reconcile this before any
  reprovisioning. It is deliberately left unchanged here.
- Still never observed on hardware: preflight on the provisioned Pi, BlueALSA
  daemon, pairing, a phone stream, real `camilladsp --check` of a sub2 candidate,
  listening.

## What now exists

| Piece | File | Behavior |
| --- | --- | --- |
| Runtime controller | `bluetooth_control.py` | `start(MAC)`, `stop()`, `tick()` watchdog, `startup()` recovery, `status()` |
| Local control socket | `bluetooth_control.serve` | `/run/octet-bluetooth/control.sock`, mode 0600, `SO_PEERCRED` uid 0 only; exact `start <MAC>`, `stop`, `status` lines |
| Server integration | `server.py` | `BLUETOOTH_CONTROL`; startup recovery; manual-action override; socket only if the RuntimeDirectory exists |
| AirPlay arbitration | `airplay_control.py`, `bluetooth_route.py` | first owner wins in both directions, CHANNEL_PROFILE included |
| Admin CLI | `bluetooth_admin.py` | bounded discoverability, trust/untrust, start/stop/status (root only) |
| Installer | `install_bluetooth.py` | dry run by default; `--apply` installs a disabled policy, the octet-remote drop-in and a non-enableable `octet-bluealsa.service`; unit state is recorded and re-verified, never changed; `--rollback` |

## Design decisions

**Policy authority vs. service user.** `octet-remote` runs as `upmpdcli` with
`NoNewPrivileges`, while the reviewed `local_policy()`/`verify_identity()` required
euid 0, so the gate could never pass where AirPlay arbitration lives (`server.LOCK`
is in-process). The new `service_policy()` makes file ownership the authority
instead: a root-owned regular file, mode exactly 0640, group `octet-bluetooth`,
in root-owned, non-group/other-writable ancestors. `observe_identity()` performs
the identical exact `busctl` Device1 reads without the euid gate; reading grants
nothing. Start requests still require a root peer on the socket.

**Policy file contract and pairing window (review fix).** Two defects found in the
independent review are fixed:

- *Policy mismatch.* `local_policy()` (root admin CLI) used to accept a root-only
  0600 file that `service_policy()` (octet-remote as `upmpdcli`) rejects. An
  operator could therefore pair and `trust` a phone successfully, after which
  `start` could never work. Now there is **one** file contract for every reader:
  root-owned, group `octet-bluetooth`, mode exactly 0640, every ancestor root-owned,
  not group/other-writable, **and world-searchable (`o+x`)** so that `upmpdcli` can
  reach the file. 0600 is refused by `local_policy()`, `service_policy()` and
  `pairing_policy()`, with the exact `chown`/`chmod` fix in the message. The
  installer applies the same directory rule and reports, but never changes, a
  pre-existing policy that breaks the contract.
- *Pairing deadlock.* `discoverable` used to require the enabled, nonempty
  allowlist, but a new phone's MAC is not known until it has paired. Now
  `discoverable` is gated by the root-only `pairing_policy()`. It applies the same
  file contract and schema (version 1, boolean `enabled`, 0–16 unique canonical
  MACs, no extra keys), but it accepts `enabled: false` and an empty allowlist.
  That is exactly the installer's default file. It returns data only and is never
  passed to `trust`, `start`, the controller or the supervisor.

What still grants playback is unchanged: `trust` requires `local_policy()`, which
needs `enabled: true` with the MAC allowlisted, plus Device1 Address/Adapter/Paired.
`start` requires `service_policy()` (same requirements, same file) plus
Paired+Trusted+Connected read twice, then the AirPlay/Auto Tune/baseline checks.
The supervisor keeps re-reading policy and identity while running. A paired but
untrusted, or not-allowlisted, phone can at most connect to the BlueALSA sink.
No bridge runs and sub2 is not routed, so nothing reaches CamillaDSP. If the
service cannot read the policy (for example, octet-remote has not been restarted
into the `octet-bluetooth` group since install), `start` now reports the cause
(`... no enabled local policy ([Errno 13] Permission denied ...)`) instead of just
"disabled".

**Ordering of a start.** A Paired+Trusted+Connected identity read (outside the lock),
policy, AirPlay (`active`, `pending`, journal, `restore_pending`, `needs_mute`,
`hook_busy`), Auto Tune and an approved Auto Tune baseline being loaded (not
CHANNEL_PROFILE) are all checked **before** any mute. A refused start never
disturbs current audio. Then: verified mute, `pause 1` with `status` confirming
not playing (queue kept), and supervisor spawn of exactly
`bluealsa-aplay --profile-a2dp --pcm=hw:Loopback,0,2 --volume=software --single-audio <MAC>`.
On the first bridge-owned playback observation, `BluetoothRoute.prepare` runs the
existing journaled, `--check`ed, muted candidate load. All 129 filters, the
pipeline and the DAC are unchanged; only capture moves to `hw:Loopback,1,2`.

**Whole-operation deadline.** One absolute deadline (≤ 120 s) is taken at `start()`
entry and shared: identity, mute, MPD pause, spawn, `BridgeSupervisor(deadline=)`,
the prepare callback (checked before and after) and the unmute itself. Expiry at
any stage fails closed.

**PCM progress boundary.** `BridgeSupervisor.progress_age()` reports time since the
stalest of the four sub2 pointers (playback/capture × hw/appl) last advanced, and
is infinite unless ready. The unmute requires `ready`, `negotiated` equal to the
prepared format and `progress_age() < stall_timeout` both before and after the
volume clamp. The watchdog requires the same while active. Pointer regression
or wrap is still rejected rather than treated as progress.

**The single guarded unmute (`_activate`).** Under `server.LOCK`: verify ownership,
original approval/bytes, unchanged candidate bytes, live path = candidate, live
config = expected, Running and muted. Set volume to −18 dB only if above it
(quieter levels are kept) and read it back. Then, on **one** DSP connection,
re-verify everything *including the volume again*, plus fresh progress and the
deadline, then `SetMute(False)`, then read back unmuted and ≤ −18 dB. If that
read-back fails, re-mute immediately on the same connection. Any exception →
`fail()`.

**Exclusion with other DSP writers: cooperative only, not atomic.** Every DSP
writer in the repository was checked:

| Writer | Exclusion while Bluetooth owns audio |
| --- | --- |
| octet-remote HTTP actions (`play-playlist`, source, volume, mute, EQ/channel controls, `auto-*`) | all go through `server.action()` under `server.LOCK`, which first stops Bluetooth muted (or refuses the action if that fails) |
| AirPlay start/recover (`airplay_control.py`, `airplay_baseline.py`) | under `server.LOCK`; start refuses while `owns_audio()` |
| Auto Tune run, apply, rollback and saved-profile activation (`measure/octetmeasure/autotune`) | take the shared `audio.lock` flock (`LOCK_NB`), which `BluetoothRoute` holds from prepare until recovery; `_verify` re-checks the lease (same inode, still held) immediately before unmuting |
| Auto Tune `mute()`/stop, `airplay_control.py mute-on-exit` (ExecStopPost) | not excluded, but they only stop CamillaDSP or mute; the watchdog then fails closed |

Tests assert that `server.LOCK` is owned and a second `audio.lock` flock fails at
the moment of `SetMute(False)`. CamillaDSP's websocket has no compare-and-set or
transaction, so the final read-back and `SetMute(False)` are still separate
commands. Any **non-cooperative** local client of `127.0.0.1:1234` (CamillaGUI, a
manual script, a CamillaDSP restart) can change volume, mute or config inside that
window. Octet cannot prevent this. It can only detect it: the same-connection
read-back right after the unmute (re-mute before any new connection), then the
0.25 s watchdog. So at most one websocket round-trip of output can occur at a
level set by such a client. Do not run other CamillaDSP clients during a
Bluetooth session.

**UNRESOLVED physical safety gate: non-cooperative CamillaDSP websocket clients.**
This race is **not solved**. It is narrowed and detected, not prevented. Nothing in
Octet can stop CamillaGUI, a manual script or a CamillaDSP restart from changing
volume, mute or config between Octet's final read-back and `SetMute(False)`, or at
any point during the session. The same-connection re-mute and the 0.25 s watchdog
bound the *duration* of the exposure. They do not bound the *level*, which that
client controls, and their effect on real speakers has not been measured. Until
CamillaDSP enforces exclusion itself (for example, the websocket bound to a socket
only Octet can reach, or authenticated), the attended operator must verify,
before every session, that CamillaGUI and any other `127.0.0.1:1234` clients are
**not running**. The physical acceptance must treat this as an open hazard and
must not be signed off on the software tests.

**Arbitration.** AirPlay `start` raises before muting while
`BLUETOOTH_CONTROL.owns_audio()` (session or journal). Every `server.action`
(including `auto-*`) first stops an owning Bluetooth session muted; if that
recovery fails, the action is refused. Nothing resumes MPD or AirPlay.

**Lifecycle and failure order.** `fail()` runs in a fixed order under
`server.LOCK`: (1) emergency mute, (2) set the session `stop_event` and call
`BridgeSupervisor.stop_child()`, which terminates and **reaps** only the session's
own Popen child (SIGTERM, bounded wait, SIGKILL, bounded wait), (3) only after
that reap is confirmed, restore and reload the original baseline, then clean up the
journal if no stage failed. Earlier revisions ran the restore from the supervisor's
callback *before* the child was terminated. That ordering is fixed.
`stop_child()` also forbids any later spawn: it is serialized with the spawn
under a lock, so a stop racing the launch can never be followed by a new child.
If termination is not confirmed, the DSP is **not** restored or reloaded. The
candidate stays loaded and muted, the journal and `audio.lock` flock are retained,
and `owns_audio()` stays true, so AirPlay and new starts are refused. The
supervisor's Popen is kept, and each watchdog tick or `stop` retries the reap.
After SIGKILL the retry only polls, so a stuck child cannot hold `server.LOCK`
for seconds per tick. Restore follows the first confirmed reap. Manual web
actions are refused while this is pending, because `stop()` raises. A child that
never exits (for example stuck in uninterruptible sleep) therefore leaves Octet
muted until an attended restart. Stale supervisor exits are no-ops. The watchdog (0.25 s) fails closed on thread exit,
stall, drift, mute/volume change, ownership change and deadline, and retries
orphaned journals. Lifetime is 30 minutes. `initialize_dsp()` runs Bluetooth
startup recovery independently of AirPlay's before the final mute confirmation.

**Discoverability.** `bluetooth_admin.py discoverable N` (1–180 s, root only, a
contract-valid policy required, which may be disabled or have an empty allowlist)
first checks, read-only, that hci0 is `Powered` (it never powers it on).
It then writes `PairableTimeout`/`DiscoverableTimeout` **before** enabling
`Pairable`/`Discoverable`, so bluetoothd ends the window even if the CLI dies.
Failure closes the window. `trust` requires an allowlisted MAC whose Device1
Address/Adapter/Paired match exactly, then closes the window.

**BlueALSA daemon unit.** `install_bluetooth.py --apply` writes
`/etc/systemd/system/octet-bluealsa.service`: `Type=dbus`, `BusName=org.bluealsa`,
`ExecStart=<preflight-verified /usr/bin daemon> --profile=a2dp-sink` (sink only),
`Restart=no`, `Requisite=bluetooth.service` (never starts BlueZ), hardening, and
**no `[Install]` section**, so it cannot be enabled and never starts at boot. It
does not use `Conflicts=bluealsa.service`, because that would stop the packaged unit
implicitly. If the packaged daemon already owns `org.bluealsa`, ours fails to start
(fail-closed). The installer records `is-enabled`/`is-active` for octet-remote,
octet-bluealsa, bluealsa and bluetooth, then re-reads them after `daemon-reload`.
Any change is reported as an error and never "repaired". Install and rollback
refuse while octet-bluealsa is running instead of stopping it. Rollback restores
the policy (only an untouched default is removed), the drop-in and the unit from
backup, or removes files that were absent before. It verifies the restore,
re-reads unit state and, only when fully verified, consumes the backup so that a
later install takes a fresh snapshot. A failed rollback retains the backup for
retry.

## Functional blockers before pairing and a listening test

1. **Run the A2DP sink daemon before pairing.** If no BlueALSA sink endpoint is
   registered with BlueZ, the phone pairs but sees no audio sink. Use the new
   disabled `octet-bluealsa.service`: an attended `systemctl start octet-bluealsa`.
   The packaged `bluealsa.service` (its Debian 4.3.1 name, ExecStart and default
   profiles are unverified) must be inactive. The installer only reports its
   state; stopping or masking it is a deliberate administrator action.
   Unverified on the Pi: the D-Bus policy that lets root own `org.bluealsa`,
   whether the hardening blocks anything (`ReadWritePaths=-/var/lib/bluealsa`), and
   `RestrictAddressFamilies=AF_UNIX AF_BLUETOOTH`.
2. **Pairing agent (manual by design).** BlueZ rejects pairing confirmations when
   no agent is registered. No agent is automated and none is auto-accepting,
   because an unattended NoInputNoOutput agent would accept any phone in range
   during the window. Before opening a window, confirm that no other agent service
   (for example `bt-agent` or a desktop agent) is running, since one could accept
   on its own. Do not use bluetoothctl's own `discoverable on` (no timeout).

   **Fresh phone, MAC unknown (exact procedure; not yet run on hardware).**
   1. Check the policy as installed:
      `sudo stat -c '%U:%G %a' /etc/octet/bluetooth.json` must print
      `root:octet-bluetooth 640`, and the content must be
      `{"version": 1, "enabled": false, "addresses": []}`. Check
      `stat -c '%U %a' /etc/octet` (root, 755).
   2. Make sure octet-remote has been restarted once since install (attended), so
      it runs with `SupplementaryGroups=octet-bluetooth`.
   3. With the packaged `bluealsa.service` inactive: `sudo systemctl start octet-bluealsa`.
   4. Terminal A: `sudo bluetoothctl`, then `agent on`, then `default-agent`. Keep
      it open and in view.
   5. Terminal B: `sudo python3 /opt/octet-remote/bluetooth_admin.py discoverable 120`.
      This works with the disabled, empty policy and grants no playback.
   6. On the phone, pick the Pi. In terminal A, check that the passkey matches the
      phone, answer `yes`, and approve the service authorization if asked. Reject
      any request you did not initiate. Read the phone's MAC from terminal A
      (`[NEW] Device AA:BB:...` / `Paired: yes`), or run `devices Paired`.
   7. Terminal B: `sudo python3 /opt/octet-remote/bluetooth_admin.py close` (or let
      bluetoothd's timeout expire). In terminal A, `agent off`, then `quit`.
   8. Edit the policy **in place** so that owner, group, mode and inode are kept, for
      example
      `echo '{"version": 1, "enabled": true, "addresses": ["AA:BB:CC:DD:EE:FF"]}' | sudo tee /etc/octet/bluetooth.json >/dev/null`
      (uppercase MAC). Do not write a new file and `mv` it into place. Run the
      step 1 `stat` again.
   9. `sudo python3 /opt/octet-remote/bluetooth_admin.py trust AA:BB:CC:DD:EE:FF`.
      It refuses unless the policy is enabled, the MAC is allowlisted, and the file
      meets the service contract. A refusal names the exact `chown`/`chmod` fix.
   10. `sudo python3 /opt/octet-remote/bluetooth_admin.py start AA:BB:CC:DD:EE:FF`
       with the phone connected (see the README for the session itself).

   To return to pairing-only mode, set `"enabled": false` in place.
   `untrust MAC` revokes BlueZ trust whatever the policy says.
3. **Adapter power.** hci0 must already be powered (BlueZ `AutoEnable`, or a
   deliberate `power on`). `discoverable` now refuses clearly instead of failing at
   the BlueZ write.
4. **D-Bus permissions for `upmpdcli`.** Still unverified: Device1 property reads on
   `org.bluez` (the drop-in adds group `bluetooth` only if it exists), and
   `bluealsa-aplay` access to `org.bluealsa` (expected via `audio`, which
   octet-remote already has).
5. **Real negotiation and `--check`.** No real phone stream has been observed. No
   real `camilladsp --check` of a sub2 candidate has been run against the live
   baseline.
6. **Process loss.** If octet-remote itself stops or crashes, the bridge child
   relies on systemd's default control-group kill. This is not exercised here.
   Within a running octet-remote, termination is now confirmed before any restore
   (see Lifecycle).
7. **UX gaps.** The web UI does not show Bluetooth state; during a session the
   profile reads "Other DSP preset". The DSP master is held at ≤ −18 dB, so phone
   volume is the only level control. Any web control ends the session. Start
   fails closed if MPD is unreachable. A bridge child that cannot be reaped keeps
   Octet muted and refuses web actions until an attended restart.
8. **Unbounded callbacks.** In-process callbacks cannot be preempted. Prepare is
   bounded only by the Camilla check timeout (10 s) plus the verification loop,
   with deadline checks on both sides.
9. **Physical acceptance list.** Everything in the 2026-10-26 design remains pending:
   44.1/48 kHz streams, L/R meters, maximum phone volume, coexistence, override,
   Auto Tune exclusion, reconnect, crash and recovery. Add: the fresh-phone pairing
   procedure above, end to end; a paired but not-yet-trusted phone connecting
   produces no audio; a 0600 policy is refused by `discoverable`/`trust`.
10. **UNRESOLVED physical safety gate: CamillaGUI / non-cooperative websocket
    race.** See "Exclusion with other DSP writers". This is not solved. Every
    attended session must first confirm that no CamillaGUI or other CamillaDSP
    websocket client is running. Acceptance must not be signed off while this
    gate is open.
11. **AirPlay to Bluetooth handoff (inspected, unchanged).** There is no automatic
    handoff, by design: first owner wins. Bluetooth `start` refuses, before any
    mute, while AirPlay is `active`, `pending`, has a baseline journal,
    `restore_pending`, `needs_mute`, or a hook still running. To hand off: (a) end
    the cast on the Apple device and wait until web status shows AirPlay inactive
    with no error, or press the web **Stop** (mutes, invalidates AirPlay and stops
    MPD); (b) if that cast used the Output DSP controls preset, activate the
    approved Auto Tune profile from the web UI, because Bluetooth refuses
    CHANNEL_PROFILE; (c) then `bluetooth_admin.py start MAC`. In the other
    direction, AirPlay start is refused while Bluetooth owns audio. Run
    `bluetooth_admin.py stop` (or use any web control) first. The physical handoff
    has not been exercised.

## Verification (this worktree, Linux, Python 3.14.9; no Pi access)

Pairing-window / policy-contract fix (2026-10-27, no SSH, Pi, services or push):

- Remote CI command: **292 passed**. Bluetooth modules alone: **166 passed** (159
  before this fix). Runtime, bridge, control and admin suites were repeated 4×
  without failure.
- New tests: `pairing_policy` accepts disabled/empty and still validates schema,
  root and the file contract; `local_policy`/`service_policy` refuse those
  same files; every reader refuses 0600 and unsearchable or writable ancestors;
  `discoverable` opens with a disabled, empty policy, never consults the start
  gate, writes bounded timeouts and registers no agent; a **fresh no-MAC flow**
  against real policy files (installer default bytes → window → simulated
  pairing → trust refused while disabled → close → in-place edit → trust →
  `service_policy` reads the same file); a 0600 policy refuses the window and
  trust before any bus write; the installer reports and never repairs a
  non-conforming existing policy and refuses 0700/0750/0777 policy directories;
  `start` reports the cause for a disabled or unreadable policy before touching audio.
- Mutation checks: gating the window on `local_policy()` again, re-accepting 0600,
  dropping the `o+x` ancestor check, or letting the start gate accept an empty
  allowlist each make new tests fail.
- No live acceptance: pairing, bluetoothctl agent behaviour, and the
  octet-remote group membership were **not** observed on hardware.

Earlier slice:

- Remote CI command (`.github/workflows/ci.yml`): **285 passed**. The Bluetooth
  modules alone: **159 passed**. The AirPlay neighbors (`test_airplay`,
  `test_install_airplay`, `test_server`): 68 passed, included in the 285. The
  threaded runtime, bridge and control suites were repeated 8× without failure.
- New regression tests cover: mute → terminate → reap → restore ordering, for
  both `stop` and a supervisor-detected stall; a bridge that will not exit
  (journal, flock, candidate and mute retained, no `SetConfigFilePath`/`Reload`,
  AirPlay and start refused, non-blocking retry, restore after the reap); a stop
  racing the spawn; `server.LOCK` and the `audio.lock` lease held at the unmute;
  a lost lease before unmute; an external volume raise between clamp and unmute
  (never unmutes); an external change right after the unmute (re-muted before
  any new connection); installer unit-state recording, reporting and never
  repairing; the non-enableable sink unit; backup and restore of the drop-in and
  unit; consumption and retention of the rollback backup; and the unpowered
  adapter.
- Mutation checks: reverting each fix (restore despite stop failure, skipping the
  reap, unguarded spawn, no final volume re-read, no same-connection re-mute) makes
  at least one new test fail. Removing the inner `server.LOCK` in `_activate` does
  **not** fail any test, because `start()` already holds the lock there. It is
  defence in depth.
- `node test_airplay_ui.cjs` and `node test_browser_cache.cjs`: passed.
- Full discovery: 314 run, 1 failure,
  `test_visualizer ... test_http_assets_and_navigation_and_host_validation`. It is
  **unrelated to Bluetooth, AirPlay and the loopback**: this checkout's
  `visualizer-media/*.mp4` are 131–134-byte Git LFS pointer files (LFS objects
  not fetched), so the test's 1024-byte HTTP `Range` assertion gets
  `bytes 0-133/134`. It is a media checkout issue, not an audio or code failure.
- Runtime tests drive the real `BridgeSupervisor` and `BluetoothRoute` (real
  journal, flock, snapshot) against a simulated DSP/MPD/bus/proc. Installer tests
  mock `systemctl`. None of this is hardware evidence.
