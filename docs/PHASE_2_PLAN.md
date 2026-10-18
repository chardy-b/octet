# Octet phase 2 plan

Saved 2026-10-09. Phase 2 is the all-in-one Auto Tune work, recorded as package version **0.2.0** in the [changelog](../measure/CHANGELOG.md). The conversation also called this work “2.0”; it is not a package version change.

## Goal

A headless Raspberry Pi 5 measures and tunes a stereo active speaker system through all eight HiFiBerry DAC8x outputs. A remote laptop or desktop controls the process through the browser or SSH. The Pi owns playback, microphone capture, optimization, saved runs and CamillaDSP activation. Guided prompts handle amplifier knobs and microphone moves.

Each side has a subwoofer, two independently driven panel exciters and a tweeter. See [system hardware](SYSTEM_HARDWARE.md) for parts, amplifier decisions and the proposed output map.

## Agreed design

- Keep the existing Octet measurement app and CLI. Implement Auto Tune inside `measure/octetmeasure/autotune/`, sharing the existing measurement and PEQ code.
- Configure each driver's minimum and maximum test frequency, protection filter, starting level, maximum level, ramp steps and amplifier group in the speaker profile. Start quietly and ramp within those limits.
- Permit positive PEQ gains up to **+3 dB**, with attenuation for the combined filter response and output ceilings. This supersedes the original cuts-only rule. Avoid trying to fill deep, position-dependent nulls.
- Use every DAC output for a driver. Use continuous playback/capture and acoustic clock markers from a common driver to estimate capture/playback clock mismatch while preserving relative driver timing. Confirm crossover candidates acoustically.
- Take exclusive control of the DAC for measurements. Stop CamillaDSP and configured music producers before direct playback. Cancellation, failure and pause leave playback stopped.
- Keep numerical processing testable with simulated hardware. Use the same service from the browser and CLI, and retain run data across browser disconnections.

## Workflow

1. **Configure and check hardware.** Set the real audio devices, microphone calibration, output map and driver limits. Confirm the wiring before enabling physical playback. The example profile remains a starting point.
2. **Levels.** Ramp protected signals per driver, show microphone peak readings and guide amplifier adjustments. Shared amplifier controls are represented by amplifier groups. Use bounded configured levels; do not automatically raise output until distortion or damage occurs.
3. **Measure.** Keep the microphone fixed for the eight-driver continuous sequence. Extract impulses and complex responses, apply microphone calibration and estimate clock ratio from acoustic markers. Blend ungated low-frequency data with gated data where the gate has adequate frequency resolution.
4. **Flatten and tune.** Fit a small PEQ set per driver. Search crossover frequency, LR4/LR8 slope, delay, polarity and attenuation. Compare overlapping and split exciter operation. Measure the shortlisted candidates with drivers playing together and select the best actual sum.
5. **Room correction.** Collect several listening-area microphone positions, power-average the responses and fit a configurable bass-rise and treble-tilt target. Use low-frequency late energy to prioritize persistent resonances and broad filters at higher frequencies.
6. **Verify.** Return to the reference microphone position, measure the final chain and compare the measured result with the prediction. Save plots, report and candidate configuration.
7. **Apply.** Validate the generated CamillaDSP configuration, preserve the previous selection, activate the candidate and verify through the actual CamillaDSP audio path. Restore the previous selection if verification fails. Keep a last-good configuration after success.

## Configured starting points

The [example speaker profile](../pi/speaker.example.yaml) supplies editable defaults. It is not a statement of measured driver ratings or finalized wiring.

| Setting | Saved example |
|---|---|
| Sample rate | 48 kHz |
| PEQ bounds | +3 dB boost, 9 dB maximum cut setting, four requested bands |
| Room positions | Five |
| Target | 4 dB bass rise, -0.4 dB per octave treble tilt |
| Driver ramps | -48 to -24 dBFS in 3 dB steps, one second per step |
| Sub test band | 30–400 Hz |
| Exciter test bands | 150–12,000 Hz |
| Tweeter test band | 2,500–20,000 Hz |

The original proposal suggested a 150–300 Hz sub/panel crossover search and a 2–4 kHz panel/tweeter search. Those were design ideas, not permission to exceed configured driver protection limits. In particular, the current example tweeter minimum is **2.5 kHz**; the original provisional 2 kHz floor is superseded by that example until a validated hardware profile says otherwise.

## Implementation and remaining validation

The 0.2.0 implementation includes simulation, browser and CLI control, protected ramps, shared timing, measured crossover selection, room fitting, configuration export, verification and rollback. Hardware-independent tests passed during implementation. Physical Pi operation, real output routing, amplifier behavior and driver limits still require validation on the assembled system.

The [Auto Tune operating guide](../measure/docs/AUTO_TUNE.md) is the reference for installation, commands, service permissions, saved runs and recovery. The [Pi configuration notes](../pi/README.md) describe the playback stack. The older `pi/camilladsp/3way-lr4.yml` is a phase 1 placeholder with a different map; phase 2 generates a separate eight-output configuration.

## Original proposal

The [original supplied plan](archive/PHASE_2_ORIGINAL_PLAN.md) is preserved verbatim. It contains historical assumptions, unverified part specifications and superseded decisions, including cuts-only DSP, a proposed standalone package layout and distortion-onset level finding. Use this current plan and the operating guide for the accepted implementation.
