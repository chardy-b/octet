# Octet audio deployment and recovery gates

Use this **repository-local** procedure for Bluetooth and Pi audio work alongside [`pi/remote/README.md`](../../pi/remote/README.md). This guide does not imply a WIL-249 receiver, policy, or fragment exists in the checked-out branch or on Octet; inspect the actual branch's README/code and the live host before acting. This procedure does not install a receiver or authorize an audio interruption or physical playback test.

## One read-only gate, reused at each boundary

Before a live change, capture and compare these against a protected snapshot:

- `/api/status`: source, ready/DSP state, mute, level, MPD playback, queue IDs, song index and elapsed time; `GetConfigFilePath`/`GetConfigJson` over the local CamillaDSP API for the active preset and its loaded configuration.
- `mpc status`; `wpctl status -n` **as the `octet` user** with `XDG_RUNTIME_DIR=/run/user/1000` and `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus`.
- The named Loopback card, sink and source, and DAC card and sink. For the last verified graph these were `alsa_card.platform-snd_aloop.0`, `alsa_output.platform-snd_aloop.0.analog-stereo`, `alsa_input.platform-snd_aloop.0.analog-stereo`, `alsa_card.platform-soc_107c000000_sound`, and `alsa_output.platform-soc_107c000000_sound.stereo-fallback`. Capture the *current* graph before relying on these names.
- Specific `/dev/snd` PCM owners and `/proc/asound/Loopback/.../subN/status` for the proposed route, plus the current BlueZ/profile owner, Bluetooth policy, phone connection/trust, service states and live fragment hash. A PCM device-level `fuser` result does not by itself identify which substream is occupied.

Run the same gate **before mutation, after service restart, after receiver registration, and after rollback**. Require every named graph node and the intended owner, mute, preset and queue state. CamillaDSP `Running` alone is not a graph-health result. Stop on drift; do not reinterpret an absent desktop node as an absent ALSA kernel card without checking `aplay -l` and PCM ownership.

## Muted, owner-free WirePlumber maintenance

Obtain approval for an interruption while music is playing. Snapshot queue/position, graph, baseline and volume; command mute and confirm it before touching services. Pause MPD **if it is playing** and confirm Loopback playback is released. Stop CamillaDSP while muted and confirm its Loopback capture and DAC playback endpoints are released. Keep the phone disconnected/untrusted, policy disabled, BlueALSA inactive and CamillaGUI stopped.

Restart only `octet`'s `wireplumber.service` using the runtime/bus environment above; verify a new active PID and that PipeWire/filter-chain PIDs did not change. Assert the complete captured Loopback/DAC graph **before** starting CamillaDSP. Restart Camilla with its existing startup `--mute` and approved baseline; read back `Running`, mute, semantic equality of the loaded config to the approved preset, queue/position and graph. Its startup level can differ from the pre-maintenance level: restore the captured level through the guarded API **while still muted**, and verify it. Leave playback paused/muted until the user deliberately resumes it.

In a pre-merge WIL-249 trial, restarting WirePlumber while MPD/CamillaDSP held ALSA devices lost the desktop Loopback/DAC graph. Removing the fragment and restarting again under the same ownership condition did not restore it. The owner-free sequence restored the nodes; that is evidence for an ownership-sensitive enumeration boundary, not proof of the earlier phone-related DSP failure's cause.

## Trial, rollback, and acceptance

Prepare **one** exact-hash manifest and a bounded preflight/apply/rollback plan before the first live mutation. Dry-run and test failure paths where practical; obtain risk-appropriate independent review of the complete sequence rather than a series of one-off privileged helpers. Root publication must refuse unexpected existing files and protect ownership, bytes, metadata and durability. After each external write, read back the exact target and runtime graph.

When a receiver implementation and muted maintenance trial are approved, a no-phone trial may install its reviewed `monitor.bluez` exclusion, restart WirePlumber owner-free, verify all graph nodes, and start the **manual** BlueALSA service briefly with policy disabled to confirm A2DP sink registration; then stop it. On a failed gate, keep output muted, stop BlueALSA, verify the exact fragment hash and metadata before removing it, and repeat the owner-free WirePlumber restart and full graph checks. If rollback does not recover the graph, stop and report the incomplete recovery. A file-only rollback is not a recovery.

Keep **code installed**, **policy/unit installed but disabled**, **receiver registration**, **phone connection**, **guarded route**, and **physical speaker acceptance** as distinct gates. No-phone registration and CI do not prove Bluetooth PCM reaches the speakers. A real source and an attended low-level listening/rollback check are required before calling playback accepted; never auto-unmute as part of maintenance.

If an approval times out, do not retry that action until the user responds. Once a bounded maintenance action is authorized, perform and verify the next safe diagnostic step instead of sending repeated status-only updates. Report the verified state and the genuine consent or physical-test boundary.
