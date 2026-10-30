# Octet Auto Tune (phase 2)

The Pi owns playback, microphone capture, optimization and saved runs. A browser on
Windows, macOS or Linux controls the same service. Eight physical outputs map to
left/right sub, exciter A, exciter B and tweeter. The original measurement UI and
CLI remain available.

## Auto mode in the music controls

On the deployed Pi, open the music app’s **DSP** page and use **Auto mode**.
Connect EMM-6 serial 38327 to Audient iD14 input 1 with phantom power, fix it at
the listening position, enter a profile name, confirm microphone readiness and
press **Start Auto mode**. The fixed-position workflow automatically runs levels,
selected-driver measurements, a protected untuned baseline, crossover search,
candidate measurements, one-position room EQ, stereo alignment, final verification
and active-path verification. Completed runs appear
in **Saved system profiles** and are applied muted at a main level of −36 dB.
Unmute/start music deliberately after the result is ready.

Choose the connected **Channel pairs to measure** before starting: A / EX30
(1/2), B / EX32 (3/4), subs (5/6), and tweeters (7/8). At least A or B must be
selected. Only selected pairs play or participate in crossover and EQ fitting;
unselected outputs remain silent and muted in the saved eight-output profile.
With one exciter pair, tuning searches its crossovers to the selected sub and/or
tweeter pairs. A driver with insufficient signal at the test ceiling is recorded
as skipped; checks continue through the remaining outputs. Exclusion is per
physical output, so a failed channel 3 does not discard a measurable channel 4.
Only usable drivers enter EQ and crossover fitting. Skipped outputs remain
unrouted and muted in the saved eight-output profile, with their reasons shown
on the DSP page and retained in the run and profile records. A driver that loses
signal in repeated measurements, or whose individual EQ fit fails, is also
excluded. No usable drivers, a shared microphone/stream fault, or failed acoustic
verification still prevents application. Every new run checks the selected
outputs again; skipped drivers are not silently excluded from future runs.
The last successful start remembers the pair selection. Existing saved profiles
keep their original selection; profiles predating this option include all pairs.

Auto mode takes **three captures per measurement stage** at the same microphone
position: driver sweeps, the untuned baseline, every crossover candidate, room EQ,
final responses, and active-path verification. Short level-setting checks are
single pilots. Each driver repeat is aligned using its shared acoustic markers;
complex responses are averaged coherently and magnitude responses are power averaged.
Raw repeats and per-repeat signal checks remain in the saved run. A weak repeat
cannot be hidden by averaging; an unusable driver is excluded. Three repeats improve repeatability; they do not
replace spatial measurements or guarantee a particular acoustic SNR.

After room EQ, the stereo pass records **left, right, and mono (L+R)** sweeps in
one continuous capture with common midrange timing markers. It repeats this three
times, estimates direct-arrival timing and relative acoustic polarity over
300–3000 Hz, and checks level balance and actual mono summation. It delays the
earlier speaker and attenuates the louder speaker, applying any confident overall
polarity correction to the entire speaker. Driver-to-driver alignment is retained.
Raw matching driver pairs also receive a relative polarity report; an opposite
reading is not by itself proof of a wiring fault because crossover topology and
acoustic response matter. Absolute electrical polarity cannot be established from
one microphone measurement.

The corrected stereo sequence is measured again before export, then repeated
through active CamillaDSP before publishing the profile. Ambiguous polarity,
inconsistent repeats, residual timing over 0.15 ms, level imbalance over 1 dB,
or excessive mono cancellation stops automatic application. Active-path failure
restores the previous configuration selection and stops playback. Saved reports
include the before/after stereo metrics and the common speaker corrections.
When the measurable driver roles differ between left and right, or either side
has no usable midrange driver, whole-speaker stereo alignment is explicitly
skipped. The available outputs still receive per-side acoustic verification;
the profile does not claim verified stereo alignment. A side with no usable
panel retains protected per-driver EQ without inventing a crossover to a
missing driver. A wholly unmeasurable side remains muted.

New runs measure **before tuning** with only mandatory driver protection active,
and **after tuning** with the chosen crossovers, driver EQ, room EQ and stereo
correction. They also verify the real active CamillaDSP path. Older runs contain
only a before-room-EQ curve; the viewer labels that distinction explicitly.

Automatic level checks reuse a compatible previous driver recording, including
recordings from runs that stopped on a disconnected driver. They start at the
last recorded level and play a short protected check, increasing in three-dB
steps only until `target_snr_db` is reached (24 dB by default). The complete
driver sequence also checks the target and repeats with increased levels only
for weak drivers if needed. These are conservative signal-margin estimates,
not calibrated band-by-band acoustic SNR. The target is separate from the
minimum acceptance floor and from each driver's `max_db` measurement ceiling.
At the ceiling, a driver meeting the minimum floor remains usable even if it
misses the preferred target. Its level report records that the target was not
reached; the configured minimum is never lowered automatically.
With no usable recording the check starts at the configured quiet level.
Crossover, room and active-path checks also respect these measured test levels.
The **Test attenuation (dB)** control on the DSP page lowers every driver's test
ceiling by 0–30 dB. For example, 9 dB attenuation changes a −15 dBFS measurement
limit to −24 dBFS. **Save test level** remembers the choice without playing sound;
the next Auto run uses it for level ramps, driver sweeps, summed measurements and
active DSP verification. Stop a running test before changing the setting. Auto
mode cannot increase past this reduced limit to chase its signal target; a weak
measurement fails instead. The control does not alter music volume, exported
playback gains or playback output ceilings. The selected amount is stored with
the run and saved profile as `test_attenuation_db`; older runs default to zero.
These are digital dBFS levels, not calibrated room SPL; changes in amplifier gain,
microphone gain or background noise are checked again. The guided workflow still
uses its manual ramp and review step.

Music has an independent `playback_max_db` hard sample ceiling, currently
**0 dBFS**, with no extra playback attenuation. Measurement ceilings are **−15 dBFS**; normal checks start at the
much quieter previously recorded levels. Export reserves headroom for the
playback ceiling and combined EQ response, never the measurement ceiling.
Raising a measurement ceiling therefore does not change normal music gain.
Legacy run profiles without `playback_max_db` retain their original combined
ceiling; existing saved configurations are not silently rewritten.

**Stop tones** interrupts direct playback. **Restore before tuning** recovers the
saved pre-run configuration. Music and manual DSP changes are blocked while a run
owns the audio devices, including after cancellation until settings are restored.
Profile recall validates the saved checksum, tweeter guards and output ceilings;
it does not play a new verification sweep or require the microphone.

The standalone service is `octet-autotune` on loopback port 8766, proxied through
the existing Tailscale-only music server. Code is installed in `/opt/octet-measure`,
with the dedicated Python environment `/opt/octet-tune-venv`. Runs and profile
manifests live in `/var/lib/octet-autotune`. The live speaker profile is
`/var/lib/octet-autotune/speaker.yaml`; device names and test ceilings live there.
The deployment keeps its original web files, audio configuration and state file
under `/var/backups/octet-autotune`.

The sub/panel search now extends to **500 Hz**. Tweeters on outputs 7/8 retain a
separate, mandatory **3500 Hz LR4 or LR8 safety high-pass** during raw measurements,
rendered candidates and exported playback. The music page also rejects lowering
the manual tweeter high-pass below this floor. Relative gain matching uses
attenuation only, and every tuned output ends with a hard digital sample ceiling.
These ceilings are configured bounds, not automatic amplifier/excursion ratings.

The guided multi-position workflow below remains available through the original
measurement app and CLI. Physical microphone moves are not automated.

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
the ceiling changes permitted test output. The independent `playback_max_db`
setting controls generated music playback caps. Legacy profiles lacking that
setting still use the historical combined ceiling until explicitly revised.

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

### Saved-profile measurement graphs

Select a saved profile on the DSP page and press **View measurements**. This is
read-only and works independently of the active listening profile. Left/right
system plots compare the protected untuned response (or before-room-EQ response for
older runs) with the final measured
response and target shape. The output selector shows individual driver response,
predicted response with DSP, measured/predicted phase, delay, polarity and relative
attenuation. Hover or use arrow keys to read graph values. Levels are relative dB,
not calibrated SPL. Manual settings backups show an explicit no-measurements
message; they do not fabricate an acoustic result.
