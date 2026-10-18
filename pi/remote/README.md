# Octet remote music controls

Runs on the Pi at **http://100.100.177.81:8767**, bound only to its Tailscale address.
Provides Tidal track search, favorite tracks, queue, playback, seeking, DSP volume,
and stop/mute. Tidal credentials stay on the Pi and use the existing upmpdcli login.
No external web hosting or browser plugins are needed. The server uses Python 3
and the existing `tidalapi` package. The installer also uses the existing PyYAML.

The initial `exciter-audition.yml` preset routes left/right to outputs 1/2
(EX30HESF2-4) and 3/4 (EX32VBDS-4). Outputs 5–8 receive digital silence. Both
exciter types play a temporary 500 Hz–5 kHz band with LR4 filters and 12 dB fixed
attenuation. The standard main-fader range is -60…-18 dB. An explicit **Unlock
higher levels** button extends it to 0 dB, without changing the current volume.
Relocking clamps a higher current setting to -18 dB. These are provisional
audition settings, not measured crossover or power limits. Physical L/R and
polarity remain to be confirmed. The app permits the audition preset and the
independent output preset described below. Other configurations block playback
and unmute through these controls.

## Measurements and output controls

The old `dml2_L` / `dml2_R` EQ was retired on 2026-10-17 after the user identified
it as potentially belonging to a different speaker test. It is not assigned to
any current output. The previous `exciter-panel-eq.yml` preset and its -3.1/-2.9 dB
headroom must not be installed. Previous Git commits and Pi configuration backups
preserve those files for reference; they do not establish valid calibration.

The active `channel-dsp.yml` has no measured EQ. All four exciter outputs have
the same temporary 500 Hz–5 kHz LR4 band and -12 dB fixed attenuation, plus their
independent polarity controls. Outputs 1/2 carry left/right EX30HESF2-4; outputs
3/4 carry left/right EX32VBDS-4. Outputs 5–8 have no input and remain silent.

The **DSP** page at `/dsp` shows **No verified measured EQ assigned**. Individual
and bulk measured-EQ switches are disabled until a valid correction is assigned;
polarity, level and mute controls remain available for the wired outputs. The
-12 dB attenuation is an audition setting, not a calibrated power limiter.

For future verified measurements, `build_panel_eq.py BASE --ex30 EX30_EXPORT
--ex32 EX32_EXPORT --output PRESET` creates correction by exciter model after
the stereo-to-output split. `channel_config` gives each output an independent
bypass switch, using identical coefficients and headroom within each model's
stereo pair. Never infer model identity from an old left/right filename.

When verified EQ is assigned, the bulk button enables or disables all assigned
measured corrections while preserving temporary audition filters and polarity.
Polarity inversion is a separate 0 dB sign stage. It is a manual comparison
control and does not establish measured acoustic phase alignment.

Switch states are validated using the installed CamillaDSP binary, applied with
a brief mute, and saved in `/var/lib/octet-dsp/channel-dsp.yml`. Failed changes
restore the previous file and leave output muted. The service can write only
this shared audio-configuration directory and its own private state directory.

Copy this folder onto Octet and run `sudo python3 install.py` to install the
unmeasured exciter setup with independent polarity controls. The installer
validates the DSP preset, backs up current settings, and starts the system muted.
It installs files under `/opt/octet-remote`, the audition config under
`/opt/camilladsp/configs`, and the systemd unit under `/etc/systemd/system`.
Back up the existing DSP state before selecting the audition config. Start with
main volume -36 dB and mute enabled. Use a systemd override with `--mute` for
CamillaDSP so every boot begins muted. Start MPD stopped (MPD `restore_paused`
enabled) so its saved queue cannot start playing on boot. Then enable the web
service with `systemctl enable --now octet-remote`.

The browser talks only to the Pi; the Pi obtains short-lived Tidal stream URLs at
play time. MPD entries point back to a local stream resolver, so queued URLs do
not expire. Session refreshes are saved with mode 0600 in `/var/lib/octet-remote`.
No Tidal tokens or stream URLs are written to access logs.
