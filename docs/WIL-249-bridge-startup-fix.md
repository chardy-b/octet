# WIL-249 Bluetooth bridge startup fix

Code: `pi/remote/bluetooth_bridge.py` (`parse_pcm`, `verify_task`, `BridgeSupervisor`).
Status: software fix with offline tests only. **Not deployed to the Pi. Bluetooth
playback remains BLOCKED** pending the attended physical acceptance gate in
[`pi/remote/README.md`](../pi/remote/README.md#bluetooth-a2dp-receiver).

## Live evidence that motivated the fix (operator-reported)

- BlueALSA's A2DP sink accepted a Pixel 11 Pro at 44100 Hz / S16_LE / stereo.
- `bluealsa-aplay` (main Popen PID, e.g. 9876) spawns an IO worker task (e.g.
  TID 9878). That worker is the `owner_pid` in
  `/proc/asound/Loopback/pcm0p/sub2/status`.
- The sub2 playback endpoint briefly reports `PREPARED` before `RUNNING`.
- A muted direct diagnostic confirmed PREPARED → RUNNING with the worker owner.
  No physical speaker test was performed.

Two defects aborted every phone start:

1. `parse_pcm` raised `PCM is not RUNNING` on the first, transient `PREPARED`.
2. The supervisor required `owner_pid == Popen.pid`, which is never true when
   the worker thread opens the PCM.

## Behaviour after the fix

- `parse_pcm` accepts only `RUNNING` or `PREPARED` and reports the state; every
  other state is still rejected.
- Playback `PREPARED` is tolerated **only** before playback has ever been seen
  `RUNNING`, and only within the existing startup deadline. It never triggers
  route preparation and never counts as pointer progress. `PREPARED` after any
  `RUNNING` playback observation fails closed.
- Capture must be `RUNNING` whenever it is open. Preparation still requires a
  `RUNNING` playback observation; readiness still requires both endpoints
  `RUNNING` with all four pointers advancing.
- `verify_task(pid, tid)` accepts the owner only if `/proc/<tid>/status` has
  `Pid == tid` and exactly one `Tgid == pid`, **and** `/proc/<pid>/task/<tid>`
  exists. It is checked on every observation against the exact spawned child's
  PID, followed by `child.poll()` so a reaped (reusable) PID is never trusted.
  Unrelated, vanished or inconsistent tasks raise `Playback is not owned by bridge`.
- The first observed playback owner is pinned; any later change (including
  between `PREPARED` and `RUNNING`) fails closed.
- Unchanged: fail-closed callback ordering, startup/lifetime/operation
  deadlines, exact native formats, policy/identity/bus-owner checks, the exact
  `bluealsa-aplay` argv, and rollback/stop-child semantics.

## Tests

RED: the new tests failed to import before `verify_task` existed; the original
module was also run directly against the live scenario and reproduced both
failures (`PCM is not RUNNING`; `Playback is not owned by bridge`).

GREEN: the CI unittest list in `pi/remote` (314 tests) passes. New coverage in
`test_bluetooth_bridge.py`: PREPARED parsing and other states rejected; fake
`/proc` membership/Tgid/Pid/duplicate/vanished cases; a real disposable Python
child's worker thread accepted and an unrelated/killed process rejected;
supervisor PREPARED → RUNNING with worker owner, PREPARED-forever deadline,
PREPARED after RUNNING, PREPARED capture, unrelated/changed/vanished owner, and
reaped child during the owner check. `test_bluetooth_runtime.py` now models the
worker-task owner through an injected fake task table.

## Limitations

- `/proc` reads and ALSA status reads are sequential observations, not an
  atomic lease; a worker could exit between them. The next tick re-checks.
- PREPARED tolerance is bounded by the startup deadline (≤ 120 s), not by a
  separate shorter window.
- Not validated on the Pi or with real phone audio; no speaker output tested.
