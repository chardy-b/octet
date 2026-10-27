# WIL-249: BlueALSA A2DP sink preflight

This first slice is a read-only prerequisite check, not an installed receiver.
Run `python3 pi/remote/bluetooth_preflight.py` from the checkout. JSON reports
`prerequisites_ok`, explicit blockers and pending work. Exit 0 means only that
local binary and idle endpoint checks passed; exit 1 means a prerequisite failed.
`available` and `advertise` remain false in both cases. No source is added to the
remote UI and no BlueZ endpoint, pairing agent or service is registered or started.
A successful result is a point-in-time observation, never an activation token.

## Dedicated loopback contract

| Owner | Playback | Paired capture |
| --- | --- | --- |
| MPD | `hw:Loopback,0,0` | `hw:Loopback,1,0` |
| AirPlay | `hw:Loopback,0,1` | `hw:Loopback,1,1` |
| Future Bluetooth sink | `hw:Loopback,0,2` | `hw:Loopback,1,2` |

The checker resolves the Loopback card ID, requires `snd_aloop`, and validates
card number, device number, subdevice number and direction in **both** sub2 info
files. Missing, redirected, malformed or occupied endpoints fail closed. Both
statuses must be `closed`; this slice does not infer ownership of an active PCM.
No PCM is opened, so passing does not prove format negotiation. No fallback to
subdevice 0/1, `default`, `plughw` or a physical DAC exists.

BlueALSA daemon (`bluealsad`, or the older `bluealsa` name) and `bluealsa-aplay`
must exist and report exactly `4.3.1`; both `--version` and `--help` must succeed
within two seconds per call. Help must contain the reviewed daemon sink profile
and player A2DP, explicit PCM, software volume and single-audio options.
These checks do not establish a running D-Bus service, enabled A2DP sink support,
BlueZ adapter readiness, codec capabilities or actual runtime behavior.
Those remain explicit pending gates before any advertisement. Version checks do
not launch the daemon. No packages, services, module parameters or presets change.

The existing setup still specifies `pcm_substreams=2`; it remains unchanged.
An opt-in file at `pi/etc/bluetooth-staged/snd-aloop.conf` specifies three, but no
installer consumes it. See the 2026-10-27 implementation status before use. A later authorized maintenance operation must provision at least three
substreams and verify actual `/proc/asound` endpoints. Editing a modprobe file
alone does not create the third substream. Do not unload an in-use snd_aloop module;
coordinate a muted maintenance restart/reboot and recheck MPD and AirPlay afterward.

## Later protected audio route

Upstream [bluealsa-aplay documentation](https://github.com/arkq/bluez-alsa/blob/master/doc/bluealsa-aplay.1.rst)
states that the default output is `default` and sample format/channel count come
from the Bluetooth connection. Therefore a bare player invocation is unsafe and
cannot establish the required 44100 Hz / S32_LE / stereo contract. Validate the
actual installed version and design an explicit conversion stage if needed; any
conversion must terminate only at `hw:Loopback,0,2`. Do not assume an undocumented
player format flag or silently redirect a failed loopback open to the DAC.

Require exact negotiated parameters at playback 0,2 and capture 1,2. CamillaDSP
must capture `hw:Loopback,1,2` at the exact observed post-bridge stereo rate and
format with asynchronous resampling
into the approved 48000 Hz speaker pipeline. Preserve playback device, filters,
crossover, EQ, gains, polarity, output mutes and all baseline protection stages.
Validate the candidate with the real CamillaDSP binary. Do not expose Bluetooth
through generic source_config until dedicated rate and restore handling exists.

Mute before changing routes; pause MPD while retaining queue/position. Serialize
ownership with AirPlay, manual controls and Auto Tune under the existing control
lock. Verify approved path/digest, exact loaded candidate, running DSP, both live
PCM states and progressing capture application/hardware pointers before any
bounded startup level and unmute. Phone volume must attenuate input only, never
change DAC mixers or bypass the master limit. Do not automatically resume MPD.

## Later pairing, advertisement and lifecycle

Before registering an A2DP sink endpoint or enabling discoverability, recheck
binaries, actual daemon/BlueZ D-Bus capabilities, adapter, dedicated substream,
validated protected route and controller health. Check A2DP sink specifically;
exclude HFP/SCO and other profiles. No service should advertise merely because it
is installed. Dependency loss must withdraw discoverability/receiver availability
and mute. This preflight cannot disable an independently configured system receiver.

Pairing should require a deliberate, bounded local action; default to non-pairable
and non-discoverable. Define confirmation/authentication and a trusted-device
allowlist, explicit removal, one active audio owner, and replacement policy. Do
not auto-trust all devices or treat a friendly device name as identity. Validate
D-Bus callers/events and restrict controller IPC permissions. Avoid logging client
identifiers unnecessarily. Reconnection alone must not acquire audio ownership.

Use session generations, deadlines and cancellation to reject stale start/stop
or reconnect events. Persist crash recovery evidence before switching an approved
baseline; restore the exact original config/path under mute, retaining the journal
on failed verification. Monitor service loss, disconnect, stalled PCM, route drift,
remote restart and Auto Tune handoff. Manual override suppresses automatic restart;
stop, failed startup and recovery never auto-unmute. Document the limit that DSP
control failure cannot guarantee a software mute until connectivity recovers.

## Verification boundary

Unit tests exercise real temporary proc-style trees and subprocess failure paths;
the existing AirPlay/source/control suite protects unchanged behavior. Physical
acceptance remains blocked until an authorized Pi session has BlueALSA binaries,
a third substream and the later route/controller. Acceptance must include real
pairing, 44100 and 48000 phone streams, exact converted PCM params, left/right
meter and protected output checks, maximum phone volume, MPD/AirPlay coexistence,
manual override, Auto Tune exclusion, reconnect, crashes and recovery. Passing
fixture tests or this prerequisite command is not evidence of audible playback.
