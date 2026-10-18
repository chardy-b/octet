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

The user identified the preset with a **110 Hz high-pass** as EX30HESF2-4,
outputs 1/2. Its complete `dml2_L` export is preserved in
`ex30-measured-eq.txt`; both outputs use the same nine filters and -3.1 dB
exported headroom. The previous stereo left/right interpretation was incorrect.

The user selected the complete `dml2_R` export for EX32VBDS-4, outputs 3/4.
It is preserved in `ex32-measured-eq.txt`; both outputs use identical ten-filter
EQ (120 Hz high-pass and 2.85 kHz low-pass included) and -2.9 dB exported headroom.
The earlier 42.7 Hz / 298 Hz recollection did not match a located export and
is not used. The retired stereo-side `exciter-panel-eq.yml` preset must not be
installed; model-specific correction is saved in `channel-dsp.yml` instead.

All four outputs retain the temporary 500 Hz–5 kHz LR4 audition band. Outputs
1/2 retain their existing -12 dB audition attenuation and -3.1 dB exported
headroom. At the user's request, outputs 3/4 have **no extra -12 dB attenuation**;
only their -2.9 dB exported preamp remains, in addition to master volume.
Polarity controls remain independent. Outputs 5–8 remain silent.

The **DSP** page at `/dsp` enables measured-EQ switches only for outputs with
assigned correction. Unmeasured outputs still expose polarity, level and mute.
The -12 dB attenuation is an audition setting, not a calibrated power limiter.

For verified measurements, `build_panel_eq.py BASE --ex30 EX30_EXPORT
--ex32 EX32_EXPORT --output PRESET` creates correction by exciter model after
the stereo-to-output split. Either model argument may be omitted to leave
that model unmeasured. `channel_config` gives each output an independent
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
current saved exciter setup with independent polarity controls. The installer
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
