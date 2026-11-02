# EQ tab: a tone layer on top of any profile

## Goal

Add an **EQ** section to the DSP page that applies a gentle tone layer (bass and
treble shelves) on top of whichever saved speaker profile is playing, from any
input (Tidal, USB, AirPlay, Bluetooth). Choosing a setting must not mute, pause or
interrupt playback, like changing profile on the DSP page today.

Presets, all relative to the profile's own tuning:

| Preset | Bass shelf (105 Hz, Q 0.5) | Treble shelf (4 kHz, Q 0.5) |
|---|---|---|
| Flat | 0 dB | 0 dB |
| Warm | +2 dB | −1.6 dB |
| Warmer | +4 dB | −2.4 dB |
| Home theater | +6 dB | −2.4 dB |

Plus manual bass (0 to +6 dB) and treble (−6 to +3 dB) sliders. The user has
approved up to +6 dB of bass boost. These preset values match the offline
profiles built on the Pi on 2026-11-01 (`…-warm`, `…-warmer`, `…-theater` in
`/var/lib/octet-autotune/presets/`); keep them identical so the two can be
compared by ear.

## Where it goes

- `pi/remote/dsp_inputs.py` is the only place a configuration gets its input.
  Add the tone layer there so every input path gets it:
  - `with_input(config, source, *, tone=None, ...)` adds a tone stage when the tone
    is not flat: a `Filter` step on channels 0 and 1, placed after the
    `octet_source` mixer (USB inputs) and before the profile's own pipeline. It uses
    CamillaDSP `Biquad` `Lowshelf` and `Highshelf` filters and a `Gain` filter for
    headroom.
  - Headroom: pre-attenuate by the largest positive shelf gain, so the layer can
    never push the profile into its limiters. This makes boosted presets slightly
    quieter at the same master volume. Say so in the UI.
  - Mark the stage (for example filter names prefixed `octet_tone_` and a JSON
    `description` like the source mixer) and add `tone_of(config)`, which returns the
    tone in a loaded config.
  - `speaker_part()` must strip the tone stage, so a saved profile stays pure and
    rebuilding is idempotent.
- Tidal on a Tidal profile currently runs the profile file itself
  (`server._load_profile_input`). With a non-flat tone it must run from
  `running.yml` instead; record the tone in `running.json`
  (`{profile, input, tone}`). With a flat tone, keep today's behaviour.
- AirPlay (`airplay_baseline.routed`, `BaselineRoute.begin` and `retarget`) and
  Bluetooth (`bluetooth_contract.routed`, `BluetoothRoute.begin` and `retarget`)
  build their candidates through `with_input`, so pass the current tone. Their
  "approved profile as CamillaDSP loads it" comparisons (`as_loaded`) and recovery
  journals must stay correct. The expected config includes the tone; recovery
  restores the profile and tone that were playing.
- Persist the setting in `STATE / 'tone.json'` (`/var/lib/octet-remote`):
  `{"preset": "warm", "bass_db": 2, "treble_db": -1.6}`. Validate ranges and types.
  A missing or invalid file means Flat.

## API

- `GET /api/tone` returns the saved tone, the tone in the running config
  (`tone_of`), the preset list, and whether it can be changed now.
- `POST /api/control/tone` with `{"preset": "warm"}` or
  `{"bass_db": 3, "treble_db": -1}`:
  - Refuses while Auto Tune runs or owns audio, like `switch_profile`.
  - Limits the master to `MAX_DB` (−18 dB) first, like `switch_profile`.
  - Saves `tone.json`, then reloads in place with the same profile and input.
    Follow `server.switch_profile`: delegate to `BLUETOOTH_CONTROL` or
    `AIRPLAY_CONTROL` when they own audio, otherwise
    `_load_profile_input(..., interrupt=False)`.
  - On failure, restores the previous tone file and config, as
    `_load_profile_input` already does for profiles.
- `switch_profile` and `select_source` keep applying the saved tone, so it follows
  the user across profile and input changes.

## Auto Tune interplay

Auto Tune measures the raw drivers directly on the DAC, and verifies through its
own config, so tone never affects a measurement. When Auto Tune applies or
restores a profile, it selects the profile file without the tone. The EQ section
should then show "Tone not applied: Auto Tune loaded the profile", with one tap to
reapply. Don't add a background reapply.

## UI (`pi/remote/dsp.html`, `dsp.js`, `style.css`)

- A new **EQ** section near the top of the DSP page, styled like the page's
  existing sections. Preset buttons (Flat, Warm, Warmer, Home theater), then bass and
  treble sliders with dB readouts. The active choice is marked. Disable it while
  Auto Tune owns audio, with the reason shown.
- Note under the controls: "Applied on top of the selected profile, from any
  input. Boosted settings play slightly quieter to keep headroom."
- Keep text in the page's existing voice. No new pages.

## Tests (match existing patterns)

- `test_dsp_inputs.py` (or the existing audio-sources tests): tone stage
  added/omitted, order after the USB source mixer, headroom gain,
  `speaker_part` strips it, `tone_of` round trip, flat leaves the config unchanged.
- `test_source_switching.py`: changing tone while playing reloads in place (no mute,
  no MPD pause, master capped at −18). Tidal with a non-flat tone uses `running.yml`;
  flat goes back to the profile file. A failed reload restores the previous tone.
- `test_airplay.py` and `test_bluetooth_control.py`: retarget with a tone, and
  recovery after a crash keeps the expected config consistent.
- A browser test in the style of `test_sources_browser.cjs` for the EQ section.
- Run the whole remote suite on the Pi (see below). `test_visualizer` fails only
  because the copy excludes media.

## Working notes

- Read `AGENTS.md`, `docs/agent-workflows/web-deployment.md` and
  `docs/agent-workflows/audio-deployment.md` first.
- Tests run on the Pi:
  `tar --exclude='*.mp4' -cf - pi | ssh octet@octet 'rm -rf /tmp/octet-test && mkdir -p /tmp/octet-test && tar -xf - -C /tmp/octet-test && cd /tmp/octet-test/pi/remote && python3 -m unittest discover'`.
  Tailscale SSH sometimes prints an approval link. Give it to the user and wait;
  the links expire.
- Deploy with `python pi/remote/deploy.py` after committing. It refuses while
  music plays (ask the user to pause). It restarts octet-remote, which ends muted.
- Never unmute or start playback yourself. Ask the user to listen and report.
- When done: deploy, set the tone via the API while muted to confirm the reload,
  then ask the user to try each preset by ear.
- Out of scope: changing Auto Tune's own room-EQ target, and deleting the three
  offline profiles (the user decides once the tab works).
