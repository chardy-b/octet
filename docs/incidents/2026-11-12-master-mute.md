# AirPlay active with master mute, 2026-11-12

The user reported a cast that appeared active but required Unmute. The installed
kernel was healthy, with no DAC/DMA faults or audio-service restart since 13:10.
The new source-independent recovery ladder was not installed or exercised.

Retained service journals show AirPlay setup mute at 13:27:50, cleared at
13:27:52, then a Music-page Unmute at 18:34:42 (PST). There is no intervening
logged mute command. Native CamillaGUI/API writes and some older internal writes
were not audited, so the actor cannot be identified. Earlier development tests
had not been fully isolated from live state; they cannot be categorically ruled
out either. Subsequent tests run with private network/devices, inaccessible
service sockets and temporary filesystems over live state directories.

The prior idle/resume path retained the existing native mute bit. A resumed cast
could therefore appear active while a stale mute persisted. The user explicitly
requested removal of master mute everywhere, including startup, tuning and faults.

Remove the Music/DSP master controls, receiver shutdown mute hooks and all
production master-mute writes. Boot normalizes saved fader latches. Octet clients
clear and log any native legacy mute on connection, including periodic watchdog
connections. Stop acts on playback/processing and recovery stops owners before
resetting hardware. Keep queue, profile, volume, lease and hardware-progress
verification. Per-output driver masks are separate from the removed master latch.

CamillaDSP upstream defines Stop as stopping processing and waiting for a new
configuration: https://github.com/HEnquist/camilladsp/blob/master/websocket.md
An isolated CamillaDSP 4.1.3 process using a synthetic input and /dev/null playback
confirmed Inactive state, null active config and retained previous config after
Stop. Status reads may inspect the selected file without restarting it; only a
new playback/source intent restarts processing.
