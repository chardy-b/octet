# Octet Auto Tune (phase 2)

The Pi owns playback, microphone capture, optimization and saved runs. A browser on
Windows, macOS or Linux controls the same service. Eight physical outputs map to
left/right sub, exciter A, exciter B and tweeter. The original measurement UI and
CLI remain available.

## Try it without hardware

```sh
pip install -e '.[dev]'
octet-measure ui --demo
```

Open **Auto Tune · 8 outputs**. Review the speaker profile, start a run and follow
the prompts. Demo mode simulates independent capture/playback clocks, eight
driver responses, propagation delays, noise and room reflections. Nothing plays.

For an entire simulated run from the terminal:

```sh
octet-measure auto start --demo --unattended
octet-measure auto apply --demo
```

The default ramp and optimizer perform real calculations in demo mode. A complete
run can take several minutes. `--unattended` skips manual prompts only in demo.

## Prepare the Pi

Install Python 3.10+, PortAudio (`sudo apt install libportaudio2`) and the package
on the Pi. Connect the Scarlett and microphone to the Pi. Run:

```sh
octet-measure devices
octet-measure auto init > speaker.yaml
octet-measure ui --no-browser --host 127.0.0.1
```

From the remote computer, forward the browser port:

```sh
ssh -L 8765:127.0.0.1:8765 octet@octet
```

Then visit `http://localhost:8765/#/autotune`. The existing `--host 0.0.0.0`
option also works on a trusted network. This app does not provide authentication;
the default deployment uses the SSH tunnel. A sample persistent service lives in
`pi/systemd/octet-measure.service`.

Edit the profile in the Auto Tune page (or pass `--profile speaker.yaml` to the
CLI). The default and example profiles use the current paired wiring:

| Outputs (left/right) | Role | Driver |
|---|---|---|
| 1/2 | `a` | EX30HESF2-4 |
| 3/4 | `b` | EX32VBDS-4 |
| 5/6 | `sub` | Dayton 12-inch Reference kit, sealed |
| 7/8 | `tweeter` | PRO Neo-8 on H6512 |

Existing saved run profiles are immutable; start a new run using the updated speaker profile.
The mapping does not validate playback limits or microphone setup.

Hardware playback requires explicit device names, an existing microphone
calibration file on the Pi, and `hardware_confirmed: true` after checking the
physical output map and driver limits. Example frequencies and levels are starting
placeholders, not validated power ratings for your speakers. The bundled EMM-6
calibration is suitable only if its serial matches your microphone.

Each driver's profile specifies `min_hz`, `max_hz`, LR protection order (4 or 8),
`start_db`, `max_db`, `step_db`, `step_seconds` and `amp_group`. Output numbers are
**1–8**; microphone input channel numbers are **zero-based**. The ramp always ends
at or below `max_db`; it never searches for damage or distortion onset. Increasing
the ceiling changes both permitted test output and the generated playback cap.

The service user needs write access to its data directory and the configured
CamillaDSP state file. It also needs narrowly scoped passwordless service control
for `sudo -n systemctl stop camilladsp`, `start camilladsp` and
`is-active camilladsp`. Install these privileges through your normal Pi
administration process. Auto Tune does not install sudo rules or modify the Pi OS.
The profile's `quiesce_services` defaults to `upmpdcli` and `mpd`; stopping these
producers also needs narrowly scoped service permissions. They remain stopped
after tuning. Restart them deliberately when ready to play music. On a Pi without
those services, configure an empty list or the actual playback producers.

## Workflow

1. **Levels:** protected sweeps ramp separately on every driver, with a live mic
   peak meter. Review the per-driver readings, adjust shared amplifier controls,
   and repeat level checks if needed. Readings are relative dBFS, not absolute SPL
   or directly comparable sensitivity across disjoint frequency bands.
2. **Measure:** keep the microphone fixed. One continuous DAC buffer and capture
   contains all eight sweeps and two acoustic markers from the same driver.
   Markers estimate clock ratio and common offset; individual driver delays are
   preserved. Low-frequency response is ungated; flattening blends to gated
   response above the gate's frequency resolution.
3. **Tune:** fit per-driver PEQ, then search both overlapping and split exciters,
   LR4/LR8 slopes, crossover frequencies, delay, polarity and attenuation. Search
   uses complex responses; three candidates per side are measured to select the
   best actual sum. This is a bounded heuristic search, not a guaranteed global
   optimum or automatic directivity design.
4. **Room:** move the microphone through the requested listening-area positions.
   Measurements are power averaged and fitted to the configurable bass rise and
   treble tilt. Low-frequency late-energy measurements increase fitting priority
   for persistent modes. Broad filters avoid chasing narrow high-frequency interference.
5. **Verify:** return to the original reference position. Compare the final
   measured response with the expected room-filter change. Download the report
   and generated configuration, and inspect the before/after plots.
6. **Apply:** validate the YAML with the installed CamillaDSP binary, save the
   previous state, activate the candidate, and measure through ALSA loopback and
   the actual CamillaDSP path. A failed acoustic check restores the previous
   selection and stops playback. A successful check records a last-good config.

Positive PEQ gains are permitted up to **+3 dB**. Export reserves attenuation for
the combined filter response and adds a per-output hard sample ceiling. The
ceiling is not an excursion/thermal model; correct configured limits remain
necessary. DAC takeover stops CamillaDSP, and playback stays stopped while paused,
cancelled or failed. Normal music resumes only through an applied configuration
or deliberate manual restoration. Do not run another controller that restarts
CamillaDSP during measurement.

The saved `pi/camilladsp/3way-lr4.yml` is a phase-1 placeholder with a different
channel map. Auto Tune exports a separate eight-output configuration; do not use
the old map for the new wiring.

## Saved runs and recovery

Runs live in an `autotune` directory beside the existing measurements directory.
Each run keeps its immutable profile, raw driver recording, extracted complex
responses/IRs, candidate measurements, room measurements, final measurements,
report and CamillaDSP YAML. Browser reconnection does not stop work. A service
restart marks unfinished work as interrupted; resume repeats the last stage,
using the same microphone position. Saved positions are retained.

```sh
octet-measure auto start --profile speaker.yaml
octet-measure auto status
octet-measure auto continue
octet-measure auto resume
octet-measure auto levels     # repeat the guided level stage
octet-measure auto mute       # sends stop to the running web service
octet-measure auto apply
octet-measure auto rollback   # restore prior selection, leave playback stopped
```

Use one controller process at a time. The web service provides the immediate mute
endpoint; Ctrl+C stops a CLI-run session. The audio lease prevents simultaneous
Auto Tune runs from separate processes. An interrupted application must be rolled
back before another application attempt.

## Validation and remaining physical checks

Run `python -m pytest`. Synthetic tests cover band-limited sweep deconvolution,
profile rejection, ramps, output routing, clock drift, relative phase retention,
cancellation, resumed stages and activation failure. Hardware-independent tests
do not establish the Pi's device names, physical routing, amplifier behavior,
safe electrical limits, or real acoustic quality.

Before normal use, verify one low-level output at a time, then the full sequence,
emergency stop, microphone disconnection, failed activation and rollback on the
actual Pi. Confirm the reference-position result also holds across the listening
area. Absolute SPL, thermal protection and mechanically moving the microphone
are outside the software's capabilities.
