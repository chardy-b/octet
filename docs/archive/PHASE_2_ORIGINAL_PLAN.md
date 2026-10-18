# Speaker Tuner: Project Plan

Auto-measure and tune a stereo active 4-way speaker system on a Raspberry Pi using CamillaDSP.
No code yet. This document is the thing to approve.

## 1. Goal

One command measures the speakers and writes a CamillaDSP config that provides:

- protection high-passes on every driver
- per-driver flattening
- crossovers (frequency, slope, delay, polarity, gain) chosen from measurements
- room correction toward a Harman-style in-room target

A separate guided mode sets the analog amp levels first.

## 2. Hardware

Per side (x2): sub, exciter A, exciter B, tweeter. That is 8 outputs, so every DAC8x channel is used and none is spare.

| Item | Detail |
|---|---|
| Computer | Raspberry Pi 4/5 running CamillaDSP and the tuner. Mac used only as an SSH/web console. |
| DAC | HiFiBerry DAC8x, 8 outputs, 2 Vrms, output only. |
| Mic interface | Focusrite Scarlett 2i2 (USB). In 1 = Dayton EMM-6 (48 V phantom). In 2 unused. |
| Mic | Dayton EMM-6 plus its individual calibration file (to obtain from Dayton). |
| Sub | Dayton 12" Reference (model and impedance TBD). |
| Panel | Acrylic 24" x 36" x 1/8", exciters EX32VBDS-4 and EX30HESF2-4 (4 ohm, separate outputs). |
| Tweeter | Dayton PRO Neo-8 compression driver on H6512 waveguide. Dayton lists 105 dB, 20 W RMS, 2.5 kHz recommended crossover. A retailer lists Fs about 1.86 kHz (verify). |
| Amps | One amp per driver type, each with an analog level knob (see section 8). Fosi V3 for initial testing. |

Default channel map, editable in config: 1 L sub, 2 L exciter A, 3 L exciter B, 4 L tweeter, 5 to 8 the same for R.

## 3. Key design decisions

1. **No loopback reference.** All DAC channels are used, so there is no spare output to feed back into the Scarlett. Timing is handled differently:
   - Per-driver sweeps are played back-to-back in one DAC stream and captured in one continuous recording. Offsets between sweeps are sample-exact on the DAC clock.
   - Clock drift between DAC and Scarlett over a few seconds is tens of microseconds. That is enough for a starting estimate.
   - Final delay, polarity, and gain are decided from measurements of drivers playing together, which are independent of the capture clock.
2. **DSP only cuts, never boosts.** Amp knobs do level matching, so the DAC stays near full scale.
3. **Tuner owns the DAC during measurement.** CamillaDSP is stopped, and each sweep is band-limited to its driver as the first line of protection. After tuning, the tuner writes a CamillaDSP config and restarts it.
4. **Pure functions at the core, thin hardware layer.** The DSP math is test-driven against synthetic data on the Mac, and the hardware layer is faked in tests.
5. **Crossovers are searched, not assumed.** Starting points are below, and measurement decides.

## 4. Crossover starting points and search ranges

| Boundary | Start | Search | Notes |
|---|---|---|---|
| Sub to panel | 200 Hz | 150 to 300 Hz | Thin acrylic, weak LF, modal ringing. Steep panel high-pass (LR4 or higher) to limit exciter excursion. Room-mode region, so use ungated, spatially averaged data. |
| Panel to tweeter | 2.5 kHz | 2.0 to 4.0 kHz | Hard floor on the tweeter high-pass at 2 kHz until the datasheet says otherwise. Slope LR4 or steeper. |
| Exciter A and B | both full-range | split (e.g. 0.5 to 1.5 kHz) | Auto-evaluate both strategies and keep the smoother sum. |

All protection minimums live in `speaker.yaml` and cannot be searched below.

## 5. Workflow stages (each is its own CLI command)

### Stage 0: Level calibration (`levels`)
Guided and interactive, with a live meter. It cannot turn knobs; it tells you which way to turn them.

1. **Mic gain.** Band-limited noise, show Scarlett peak, set gain so peaks sit near -12 dBFS. Never touch it again.
2. **Noise floor.** Digital silence playing, mic near each driver, report hiss. Flag the tweeter if it is above room noise.
3. **Sensitivity match.** Band-limited noise through each driver at a fixed digital level. The least sensitive driver (the panel) is the reference. Live meter shows each other driver's offset in dB; turn its amp knob down until matched.
4. **Gain rule.** No channel may need DSP gain. Warn if any would.
5. **Headroom.** Ramp a sweep in steps, find the level where harmonics rise, and set a per-driver DSP limiter below that. Exciters and tweeter get a hard cap.
6. **Save a level map.** Later stages replay a quick reference tone per driver and warn if a level has drifted.

### Stage 1: Per-driver measurement (`measure`)
- Exponential sweeps per driver, band-limited, one stream and one capture, 1 to 3 s each with gaps.
- Deconvolve each, apply the mic calibration, and store impulse responses.
- Gated response (above a few hundred Hz) for driver flattening. Ungated for the sub and the panel's low end.

### Stage 2: Driver flattening
Fit a small PEQ set per driver to its gated response within its usable band. Panels ring, so use broad filters and rely on spatial averaging.

### Stage 3: Crossover search (`tune`)
1. Estimate the starting delay near each crossover by cross-correlating band-limited impulse responses of neighbors. For the panel, do not use the impulse peak, since a DML has no clean acoustic center.
2. Test both polarities and keep the one with the larger summed level at the crossover.
3. Search crossover frequency, slope, delay, and gain (scipy optimizer). Each candidate is scored on the measured sum of the neighboring drivers playing together.
4. Score: smoothness of the summed response around the crossover, penalty for deep notches, penalty for out-of-band exciter energy.
5. Re-measure the full chain to confirm.

### Stage 4: Room stage (`room`)
- 5 to 9 mic positions around the listening spot, ungated, power-averaged, 1/6-octave smoothed (1/3 above about 1 kHz).
- Fit PEQ to the Harman-style target: cut peaks, do not boost nulls, narrow filters only below about 250 Hz, boosts capped, negative preamp for headroom.
- Use decay (waterfall) to prioritize ringing room modes. Leave dips that do not ring and move with the mic.

### Stage 5: Apply and verify (`apply`)
Write the CamillaDSP config, keep the last-good config for rollback, re-measure, and show before and after.

## 6. Modules (`src/speaker_tuner/`)

| Module | Responsibility |
|---|---|
| `signals` | Sweep and inverse filter, band-limited noise, silence |
| `ir` | Deconvolution, gating, mic calibration, FFT, fractional-octave smoothing |
| `metrics` | Band level, noise floor, harmonic distortion from the sweep, clipping detection |
| `timing` | Delay estimation, polarity test |
| `filters` | Biquad PEQ, LR crossovers, response evaluation, PEQ fitting |
| `targets` | Harman-style target generator |
| `optimize` | Crossover search and scoring |
| `safety` | Protection minimums, level ramp, abort rules |
| `camilla` | Config writer, apply, rollback |
| `audio_io` | Multichannel play and record (hardware layer, faked in tests) |
| `cli` | `levels`, `measure`, `tune`, `room`, `apply`, `status`, `mute` |

Config file `speaker.yaml`: channel map, driver band and protection minimums, max levels, mic calibration path, target curve.

CLI first. A web page for the live meter comes later, only if the terminal meter is not enough.

## 7. Safety rules

- No channel ever plays without its protection high-pass.
- First sweep at a very low level, then step up. Abort on mic clipping or an unexpected level jump.
- `mute` command silences everything at once.
- Always keep the last-good CamillaDSP config.
- Recommend a series capacitor (a forum-suggested value is about 33 uF) or an amp with soft-start muting on the tweeters, to guard the compression diaphragm against turn-on thumps. Its effect on the response gets measured with everything else.
- Amps must not auto-standby, or the start of a sweep can be missed.

## 8. Amp requirements

- Analog level knob, no tone controls or built-in DSP, no auto-standby, 2 Vrms input friendly, DC and short protection.
- **Tweeters** want the lowest noise floor and little power (a few watts is loud at 105 dB). Candidates: Topping PA5 II (vendor claims noise under 15 uV, equal to roughly 0 dB SPL at 1 m on this driver), or the Fosi V3 you own, tested first.
- **Exciters:** TPA3255-class stereo amp per side. Power ratings to be checked on the exciter datasheets.
- **Subs:** TPA3255-class stereo amp or a plate amp with its crossover bypassed.

## 9. Test strategy

- TDD with synthetic data on the Mac, no hardware needed.
- Unit tests: sweep and deconvolution recover a known impulse response, delay estimation recovers a known delay within tolerance, polarity detection on an inverted copy, PEQ fitting reduces error against a synthetic room response with known modes, crossover scoring prefers the aligned case.
- Integration tests: simulate a room by convolving with a synthetic response, run the whole `tune` and `room` pipeline against a fake `audio_io`, and assert the output response is within tolerance of the target.
- Hardware smoke tests are a manual checklist at the end of each milestone.

## 10. Milestones

- **M0:** project skeleton, `speaker.yaml`, fake audio backend, test runner.
- **M1:** signals and `ir` (sweep, deconvolve, calibrate). Measure one driver on hardware.
- **M2:** `levels` mode (Stage 0).
- **M3:** all-driver measurement stream, delay and polarity estimation (Stage 1, first half of Stage 3).
- **M4:** crossover search with sum verification and CamillaDSP config writer (Stage 3, Stage 5).
- **M5:** room stage (Stage 4).
- **M6:** safety hardening, rollback, status and reporting.

## 11. Open items

1. Datasheets: EX32VBDS-4 and EX30HESF2-4 (Fs, range, power), H6512 cutoff, Neo-8 high-pass guidance, Dayton 12" sub model and impedance.
2. EMM-6 calibration file from Dayton.
3. Final amp purchases.
4. Exciter strategy default: full-range both or split. Both get auto-evaluated unless a default is chosen.
5. Pi OS and kernel version, and Scarlett generation (Linux driver support and controls).
6. Whether the tweeter series capacitor is wanted.
