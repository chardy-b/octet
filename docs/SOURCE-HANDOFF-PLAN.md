# Plan: switch sources without muting

Status: written 2026-11-02.
- **Done in code:** the AirPlay part of Phase 1 (`airplay_control.py`: `pause`, `resume`, `release_idle`), except the volume cap: a new cast still sets −18 dB. Not yet deployed or tried with a phone.
- **Still to do:** the Bluetooth part and Phase 2.
- **Dropped:** Phase 3. Tidal is not to be resampled.

## Goal

When the phone switches apps, or a different source starts playing, Octet keeps
playing: no mute, no reset to the profile's Tidal input, and the newest source
takes over. Silence from the phone is just silence through the speakers.

## What happens today

"Restore the profile" never changes the speaker settings. The saved profile
(crossovers, EQ, levels, delays) stays the same throughout. What changes is the
**input** CamillaDSP reads:

| Input | CamillaDSP capture device | Config file |
|---|---|---|
| Tidal (MPD) | `hw:Loopback,1,0` | the profile file itself |
| AirPlay | `hw:Loopback,1,1` | `airplay-baseline.yml` (profile + AirPlay input) |
| Bluetooth | `hw:Loopback,1,2` | `bluetooth-baseline.yml` (profile + Bluetooth input, rate and format of the phone's stream) |

When a cast or phone stream ends, Octet does three things:
- **Mute:** it mutes first.
- **Switch back:** it points CamillaDSP back at the profile file, which reads the Tidal input.
- **Leave it muted:** it stays muted, and Tidal stays paused.

The next stream builds the route again: mute, reload, wait for audio to flow,
then unmute (Bluetooth caps the volume at −18 dB; AirPlay sets it to −18 dB).

| Event | Bluetooth (`bluetooth_control.py`, `bluetooth_route.py`) | AirPlay (`airplay_control.py`) |
|---|---|---|
| Stream ends | Transport leaves `active` → `stop_auto` → `fail()` → mute, kill bridge, `route.recover()` | Stop hook (1 s debounce) or 1 s of loopback silence → `end()` → mute, `baseline.recover()` |
| New stream | Fresh `idle→active` edge → `start_auto` → `prepare()` muted → wait for PCM → guarded unmute | Start hook → mute → `baseline.begin()` → wait for PCM → unmute at −18 |
| Other source starts | AirPlay refuses Bluetooth ("first owner wins") | Bluetooth refuses AirPlay |
| Music-page control | Volume and Mute adjust it in place (`set_level`, ≤ −18 dB); others `BLUETOOTH_CONTROL.stop()`, muted | Volume and Mute adjust it in place (`set_level`); others `invalidate()`, muted |
| Tidal Play | Refused until Tidal is the input, then needs an explicit unmute | same |

Each reload that changes the capture device restarts CamillaDSP's audio devices.
That costs a short gap even without a mute. An app switch today costs two reloads
(back to Tidal, then onto the phone again) plus the readiness wait.

## Design: the input stays where it is

1. **The input stays put.** When a stream ends, the Bluetooth or AirPlay input stays loaded and unmuted. No reload, no mute, no volume change.
2. **Same source again: no DSP work.** When the same source starts again with the same stream format, nothing happens in the DSP. Bluetooth only reattaches its bridge.
3. **The newest source wins.** A different source starting switches the input to it without muting, with one reload. This applies whether the old source is idle or still playing.
4. **Tidal Play switches back.** Pressing Play (or Resume, a playlist, an album) on the Music page while a phone input is loaded switches the input back to Tidal and plays. No separate unmute step.
5. **The safety mutes stay.** Octet still mutes when a reload or check fails, at boot, around Auto Tune, and whenever the user mutes. Automatic switches still cap the master at −18 dB, and only lower a louder setting.

### Phase 1: the input stays loaded (fixes the app switch)

**Bluetooth**
- **New end path:** add `BluetoothControl.idle(generation)`, used by `AutoBluetooth` for a normal stream end ("transport left active"). Errors and disconnects that look like failures still go through `stop_auto`.
  - It reaps the bridge child (`bluealsa-aplay` exits or closes its PCM when the stream idles).
  - It leaves `bluetooth-baseline.yml` loaded and unmuted.
  - It keeps the journal, so a reboot still recovers to the profile.
  - It records `self.idle_format = session.format` and clears `session`.
- **Next stream from any paired phone** (`_start`):
  - Same rate and format as `idle_format`: skip `prepare()`.
  - Start the bridge, wait for PCM progress, and confirm the route still matches `expected`. No mute or unmute: it was never muted.
  - Different format (for example SBC 44.1 kHz then AAC 48 kHz): call a new `route.retarget_format(rate, format)`. It reloads in place without muting, as `retarget()` already does for a profile change.
- **Ownership:** `owns_audio()` must report true while the input is loaded with no stream. Expose it as `idle` in `status()` so the Music page can show "Bluetooth · waiting for the phone".
- **Optional, to compare:** `bluealsa-aplay --keep-alive=SEC`. It keeps the loopback PCM open for SEC seconds after the phone suspends, so a quick app switch never even closes the bridge. Measure this against reattaching the bridge.

**AirPlay**
- **Stop hook:** `event('stop')` keeps the route loaded and unmuted and sets `self.idle = True`. It no longer calls `end()` after the debounce.
- **Silence:** remove the end-on-1 s-silence check in `tick()`. Silence is shown, not acted on. The UI shows "AirPlay · idle".
- **Start hook with the route loaded:** if `baseline.ready(...)` still passes, mark the cast active and change nothing. If the check fails, rebuild the route. A missed start then no longer leaves you stuck: the route is already loaded, so any new audio plays.
- **Volume:** on an automatic start, change the forced `SetVolume(-18)` to a cap (only lower a setting above −18), matching Bluetooth.

**Reboot**
- Boot still mutes and recovers to the profile (Tidal input), from the journals. No change.

### Phase 2: the newest source wins (no mute)

- **Shared helper:** add `server.handover(to)`, held under `LOCK`, used by AirPlay start, Bluetooth start and Tidal Play.
  - If the other phone source holds the input (playing or idle), hand it over: stop that source's bridge or mark its cast ended, without its recovery reload.
  - Build the new source's route from the profile named in the old journal, then make one `SetConfigFilePath` + `Reload`.
  - Cap the volume at −18 dB, unless the user has unlocked higher levels and is already above it.
  - **On a failed check or reload:** mute and restore, as today.
- **Rule change:** replace the "first owner wins" refusals with the handover. These are `AirPlayController.event` (Bluetooth owns), `BluetoothControl._idle` (AirPlay owns) and `BluetoothRoute.ownership`.
- **Journals:** one input journal at a time. A handover rewrites the journal for the new source before the reload, so a crash mid-switch still recovers to the profile.
- **Tidal Play / Resume / play-playlist / play-album:** if the input isn't Tidal, call `handover('tidal')` instead of failing `require_tidal`, then play. Other Music-page controls (volume, channel EQ) no longer stop Bluetooth. Only **Switch source**, **Stop** and Auto Tune do.
- **Pops:** measure the gap and any pop from the device restart with the mic. If there's a click, wrap only the reload in CamillaDSP's volume ramp (fade down ~50 ms, reload, fade up). Never wait for readiness while muted.

### Not doing: one shared mixed input

A single mixed input (ALSA `dmix` or PipeWire) would remove the reload between
sources, but it resamples Tidal. Dropped: Tidal stays bit-exact at its own rate.

## Tests

- **`test_bluetooth_control.py`:**
  - A stream end leaves the route loaded, unmuted, journal kept, bridge reaped, `idle` true.
  - The same format restarts without a reload or mute call.
  - A new format reloads in place without a mute.
  - A failure still mutes and recovers.
- **`test_bluetooth_auto` / runtime:** a transport going idle calls `idle`, not `stop_auto`. Disconnect, owner change and failure still stop muted.
- **`test_airplay.py`:**
  - A stop or silence keeps the cast's route loaded and unmuted.
  - A start on a loaded, ready route makes no DSP calls.
  - The volume is capped, not forced to −18.
- **Handover tests:**
  - AirPlay → Bluetooth, Bluetooth → AirPlay and phone → Tidal Play each make one reload, no `SetMute(True)` on success, and the journal names the new source before the reload.
  - A failed reload mutes and restores the profile.
- **Boot:** boot recovery from each journal is unchanged (muted, profile input).
- **UI (`test_bluetooth_ui.py`, `test_airplay_ui.cjs`, `test_sources_browser.cjs`):** the idle states, and Tidal Play working while a phone input is loaded.

## Live verification (needs you present: it plays sound)

Follow [the audio deployment gates](agent-workflows/audio-deployment.md):
- Snapshot and deploy while paused.
- Then, at −18 dB or quieter, with the mic recording to time any gaps and pops:

| Test | Today | Target |
|---|---|---|
| Bluetooth: pause → play in the same app | mute + 2 reloads | no gap beyond the phone's own |
| Bluetooth: YouTube Music → Spotify | mute + 2 reloads | no gap beyond the phone's own |
| AirPlay: app switch | mute + reload, maybe stuck silent | no gap beyond the phone's own |
| Bluetooth → AirPlay, AirPlay → Bluetooth | refused | 1 reload, no mute |
| Phone idle → Tidal Play on the Music page | refused until switched and unmuted | 1 reload, plays |
| Reboot with a phone input loaded | boots muted on Tidal | unchanged |

## Docs to update

- **`pi/remote/README.md`:** Profiles and inputs, Classic AirPlay, Bluetooth "Who plays".
- **Status text:** the `app.js` source-status strings.

## Open questions

- **Tidal while a phone source is playing:** a Tidal Play takes over (newest wins). Proposed: yes.
- **Auto-resume:** should Tidal resume by itself when a phone stops? Proposed: no, as today.
