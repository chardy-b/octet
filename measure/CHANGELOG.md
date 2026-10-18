# Changelog

## 0.2.0

- Pi-hosted Auto Tune for eight outputs, with browser and CLI control, guided
  amplifier checks and microphone-position prompts.
- Per-driver protected frequency bands, bounded level ramps, output ceilings,
  immediate cancellation and a hardware setup gate.
- Continuous driver measurements with acoustic clock markers, shared timing,
  per-driver PEQ and measured crossover candidate selection.
- Spatial room correction, LF decay weighting and up to +3 dB PEQ boosts.
- Exact filter export, active-path verification, rollback and durable run files.
- Eight-driver simulation and tests for timing, routing, limits and recovery.
- Correct narrow-band sweep normalization and EQ reference-band handling.

## 0.1.0

First release.

- Browser app: record (with a live level meter and a target range), browse and compare recordings, fit an EQ, manage microphone calibration files, measure a player's clock offset.
- Players: DLNA/UPnP streamers, this computer's sound card, or a sweep file played by hand. A built-in demo mode simulates the hardware.
- Analysis: swept-sine deconvolution, microphone calibration, third-octave signal-to-noise, player clock-offset correction, windowing and smoothing options.
- EQ: up to ten filters (peaking, plus protective high-pass and low-pass) fitted toward a Harman-style target, with the predicted result.
- Export of frequency response and impulse response files that REW can import.
- Windows, macOS and Linux.
