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
polarity remain to be confirmed. The app permits the audition preset, saved
panel EQ preset, and the independent output preset described below. Other active
configurations block playback and unmute through these controls.

## Measured panel EQ and output controls

The user confirmed that `dml2_L` and `dml2_R` are **speaker panel** measurements.
The 2026-10-05 saved exports are preserved in `exciter-panel-eq.yml`, including
their -3.1/-2.9 dB preamps and HP/LP filters (the right export has a 2.85 kHz LP).
Left-panel EQ feeds outputs 1/3; right-panel EQ feeds 2/4. The polarity-inverted
measurement was not applied. Delay and physical polarity remain unverified.

The **DSP** page at `/dsp` reads the running CamillaDSP configuration. Each of the
first four outputs has its own measured-EQ bypass switch. The common panel
correction is implemented after the output split, so toggling output 1 does not
also toggle output 3. Audition HP/LP filters, 12 dB attenuation, and exported
preamp headroom remain active. Outputs 5–8 have no input and no EQ assignment.

The bulk button shows **Disable DSP for all** when every assigned output has
measured EQ enabled; otherwise it shows **Enable DSP for all**. It applies the
selected state to all assigned outputs in one validated reload, preserving
audition protection, headroom, and polarity. Each assigned output also has an **Invert polarity** button,
implemented as a separate 0 dB sign-inversion stage. It preserves EQ, level, and
other outputs' polarity. Inversion is a manual comparison control; it does not
establish measured acoustic phase alignment. The initial polarity is normal.

Switch states are validated using the installed CamillaDSP binary, applied with
a brief mute, and saved in `/var/lib/octet-dsp/channel-dsp.yml`. Failed changes
restore the previous file and leave output muted. The service can write only
this shared audio-configuration directory and its own private state directory.

Copy this folder onto Octet and run `sudo python3 install.py --panel-eq` to restore
the current measured-panel setup. Omit the flag for the basic audition preset. The installer
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
