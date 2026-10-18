# Octet speaker system and hardware

Saved 2026-10-09. The target system is a stereo active panel speaker with four independently driven outputs per side: subwoofer, exciter A, exciter B and tweeter. The table distinguishes selected components, existing test equipment and choices still to be made.

## Processing and measurement hardware

| Component | Model or configuration | Status and notes |
|---|---|---|
| Host | Raspberry Pi 5, headless | Selected host for CamillaDSP and Auto Tune; remote laptop or desktop provides browser or SSH control. |
| DAC | HiFiBerry DAC8x | Eight playback outputs, all allocated to drivers; no spare loopback-reference output. The original plan lists 2 Vrms output; verify the electrical specification before using it for amplifier matching. |
| Audio interface | Focusrite Scarlett 2i2 over USB | Planned Pi capture interface. Generation is still to be recorded. Input 1 is the measurement microphone; input 2 is unused. |
| Microphone | Dayton EMM-6 | Requires 48 V phantom power from the interface. Calibration file used in the existing measurements: `EMM-6_38327.txt`; confirm it matches the physical microphone. |
| Calibration location | `measure/measurements/_calibration/EMM-6_38327.txt` | Present locally. The measurements directory is Git-ignored, so repository commits alone do not back up this file. |
| Remote computer | Windows, macOS or Linux laptop/desktop | Browser over an SSH tunnel is the documented deployment. The original plan mentioned a Mac; the agreed design is not Mac-specific. |

## Drivers and panels

Quantities below describe the planned stereo build, not a confirmed purchased inventory.

| Part | Per side | Stereo total | Details and unresolved items |
|---|---:|---:|---|
| Dayton 12-inch Reference subwoofer | 1 | 2 | Exact model, impedance and enclosure details still to be recorded. |
| Acrylic panel | 1 | 2 | 24 × 36 × 1/8 inches. Exciter mounting positions and final construction details still to be recorded. |
| Dayton EX32VBDS-4 exciter | 1 | 2 | 4 ohms in the supplied plan; independent amplifier/DAC channel. Confirm datasheet frequency and power limits. |
| Dayton EX30HESF2-4 exciter | 1 | 2 | 4 ohms in the supplied plan; independent amplifier/DAC channel. Confirm datasheet frequency and power limits. |
| Dayton PRO Neo-8 compression driver | 1 | 2 | Planned tweeter on the H6512 waveguide. Original notes list 105 dB sensitivity, 20 W RMS and a 2.5 kHz recommended crossover; these remain source-plan figures to verify against the exact part datasheet. |
| Dayton H6512 waveguide | 1 | 2 | Confirm cutoff, compatibility and final mounting. |

The original proposal also mentioned an approximately 1.86 kHz Neo-8 resonance from a retailer. It is unverified and is not a protection-frequency setting. A tweeter series capacitor was considered but not selected; the archived forum-derived 33 µF idea is not a finalized component value.

## Amplifiers

The original plan records an owned **Fosi V3** for initial testing. Final amplifier purchases, quantities, power supplies and channel assignments remain open.

| Driver group | Recorded option | Decision status |
|---|---|---|
| Tweeters | Test the Fosi V3 first; Topping PA5 II was a candidate for low noise | Final choice pending listening/noise checks and gain matching. |
| Exciters | TPA3255-class amplification | Exact amp models, supply voltages and assignment pending. Each exciter needs an independent channel. |
| Subwoofers | TPA3255-class stereo amp or plate amps with crossover bypassed | Final choice depends on the sub model and load. |

Planned requirements are analog level adjustment, suitable input sensitivity for the DAC, no unwanted tone processing, no sweep-disrupting auto-standby, and appropriate DC/short protection. Shared volume controls must be reflected in the profile's `amp_group` settings.

The example profile groups matching driver roles across left and right (`sub`, `a`, `b`, `tweeter`). The original proposal also considered an exciter stereo amp per side. Set the groups to the actual amplifier wiring once chosen; these two control arrangements are not interchangeable.

## Current output map

Updated 2026-10-17 from the user's current wiring. Role A is EX30HESF2-4;
role B is EX32VBDS-4. Odd outputs are left, even outputs right.

| DAC output | Profile ID | Destination |
|---:|---|---|
| 1 | `L_a` | Left EX30HESF2-4 |
| 2 | `R_a` | Right EX30HESF2-4 |
| 3 | `L_b` | Left EX32VBDS-4 |
| 4 | `R_b` | Right EX32VBDS-4 |
| 5 | `L_sub` | Left sealed Dayton 12-inch Reference kit |
| 6 | `R_sub` | Right sealed Dayton 12-inch Reference kit |
| 7 | `L_tweeter` | Left PRO Neo-8 / H6512 |
| 8 | `R_tweeter` | Right PRO Neo-8 / H6512 |

EX30 uses a Fosi V3 with 36 V / 5 A supply; EX32 uses a TPA3116 board
with 24 V / 6 A supply. The user requested Fosi V3 be assumed for subs and
tweeters. These assignments do not establish measured gain matching or limits.
Microphone channel `0` means physical Scarlett input 1. The old three-way
CamillaDSP placeholder uses a different output map.

## Pi playback stack

The existing [Pi notes](../pi/README.md) record Raspberry Pi OS based on Debian 13 trixie, Tailscale hostname `octet`, SSH as `octet@octet`, and CamillaDSP 4.1.3 under `/opt/camilladsp`. Those notes describe reconstructed configuration files that were not checked against the live Pi when saved; confirm OS, kernel and installed versions on the device.

Normal playback follows Tidal/local files → MPD with upmpdcli → ALSA loopback → CamillaDSP → DAC8x → amplifiers → drivers. Auto Tune temporarily owns the playback device during measurement. See the [operating guide](../measure/docs/AUTO_TUNE.md) for service setup and activation behavior.

## Existing speaker measurement setup

The **Criton CSS-1TDX** recordings are a separate speaker measurement set used to exercise Octet's room-PEQ analysis. They do not establish the active panel system's crossover settings or wiring.

- Recorded player: WiiM Pro, identified as `WiiM Pro-C09E` in measurement metadata.
- Microphone calibration: `EMM-6_38327.txt`.
- Latest analyzed captures: `criton_css1tdx_2`, `criton_css1tdx_pos2`, `criton_css1tdx_pos3d` under `measure/measurements/`.
- User confirmed EQ was off, playback volume varied slightly, and earlier position-3 recordings had playback failures. Analysis aligned the usable responses over 300–3,000 Hz before averaging.
- Both speakers played together. The recommended shared stereo PEQ was 50 Hz / -9 dB / Q 1.38, 108 Hz / -3.9 dB / Q 2.65, and 179 Hz / +3 dB / Q 2.67, with -3 dB preamp. This is a predicted starting preset, not an applied or acoustically verified result.

## Information to add when finalized

Record the subwoofer SKU and impedance; final amplifier models, quantities and supplies; Scarlett generation; confirmed microphone serial; exciter A/B model assignments and mounting positions; actual output wiring; measured driver operating limits; enclosure details; and whether tweeter hardware protection is fitted.

The [current phase 2 plan](PHASE_2_PLAN.md), [original proposal](archive/PHASE_2_ORIGINAL_PLAN.md), [speaker profile](../pi/speaker.example.yaml) and [Pi notes](../pi/README.md) provide the design history and configuration references.
