# Octet remote music controls

Runs on the Pi at **http://100.100.177.81:8767**, bound only to its Tailscale address.
Provides Tidal track/album search, favorites, saved playlists, queue, playback, seeking, DSP volume,
and stop/mute. Tidal credentials stay on the Pi and use the existing upmpdcli login.
No external web hosting or browser plugins are needed. The server uses Python 3
and the existing `tidalapi` package. The installer also uses the existing PyYAML.

The initial `exciter-audition.yml` preset routes left/right to outputs 1/2
(EX30HESF2-4) and 3/4 (EX32VBDS-4). Outputs 5–8 receive digital silence. Both
exciter types play a temporary 500 Hz–3.5 kHz band with LR4 filters. There is no
fixed audition attenuation. The standard main-fader range is -60…-18 dB. An explicit **Unlock
higher levels** button extends it to 0 dB, without changing the current volume.
Relocking clamps a higher current setting to -18 dB. These are provisional
audition settings, not measured crossover or power limits. Physical L/R and
polarity remain to be confirmed. The app permits the audition preset and the
independent output preset described below. Other configurations block playback
and unmute through these controls.

## Tidal playlists

The Music page has **Favorite tracks** and **Playlists** tabs. Playlists includes
both created and favorited Tidal playlists, with **Load more playlists** for
libraries larger than 50. Each playlist has **Play all**; its title opens the
complete track list, including playlists longer than 100 tracks.

Selecting a playlist track loads the whole playlist and starts at that track.
Subsequent songs play in order automatically. **Play all** starts at the first
available track. Unavailable tracks are omitted with a count shown in the
playlist details. The queue uses fresh stream resolution for each track.

Playlist playback replaces the current queue only after Tidal metadata has
loaded and MPD has accepted the new entries. It turns shuffle, single-track,
repeat and consume modes off for ordered playback. Existing per-channel mute,
EQ, polarity and listening volume are preserved; master mute is released when
playback starts. Browsing playlists alone does not affect current music.

`GET /api/playlists?offset=0` pages through the library; `GET /api/playlist?id=UUID`
loads all tracks. `POST /api/control/play-playlist` accepts `id`, optional zero-based
`position`, and optional `track_id` to detect a changed playlist. Metadata is
cached briefly for consistent selection. Playlists up to 10,000 tracks are
supported. Tidal credentials remain on the Pi.

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

All four outputs retain the temporary 500 Hz–3.5 kHz LR4 audition band. There
is **no extra -12 dB attenuation on any assigned output**. Outputs 1/2 retain
only their -3.1 dB exported preamp, and outputs 3/4 retain only -2.9 dB, in
addition to master volume. These are not calibrated power limits.

The **DSP** page at `/dsp` has a **Mute channel / Unmute channel** button for
each assigned output, separate from master mute. The setting persists in the
same 0 dB output stage as polarity, without changing inversion, EQ, gain or
another channel's mute. Restoring an individual channel leaves master mute
unchanged. All eight assigned outputs have mute and polarity controls; measured EQ remains
assigned only to the exciter pairs.

## Eight-output assignment and starting bands

| Outputs | Left/right driver pair | LR4 high-pass | LR4 low-pass | Additional attenuation | Initial mute |
|---|---|---:|---:|---:|---|
| 1/2 | EX30HESF2-4, `dml2_L` | 500 Hz audition guard | 3.5 kHz audition guard | None; exported preamp -3.1 dB | Preserved |
| 3/4 | EX32VBDS-4, `dml2_R` | 500 Hz audition guard | 3.5 kHz audition guard | None; exported preamp -2.9 dB | Preserved |
| 5/6 | Dayton 12-inch Reference kit, sealed | 25 Hz | 500 Hz | -30 dB | Muted |
| 7/8 | Dayton PRO Neo-8 on H6512 waveguides | 3.5 kHz | 20 kHz | -12 dB | Muted |

Odd outputs carry left input; even outputs carry right input. The user requested
Fosi V3 amplification be assumed for both new pairs. Exact subwoofer SKU, box
volume, new amplifier supplies and knob settings are not verified. These gains
are quiet starting values, not measured level matching or calibrated power
limits. New output mutes are persisted; no sound is started automatically.
Existing exciter coefficients, bypass, polarity and mute settings are preserved.

The 500 Hz sub low-pass meets the current 500 Hz exciter audition high-pass;
it is a provisional woofer/panel transition, not a final 80–120 Hz subwoofer
crossover. The tweeter 3.5 kHz high-pass meets the exciter 3.5 kHz low-pass
and matches the 3.5 kHz crossover recommendation in the
[PRO Neo-8 datasheet](https://www.daytonaudio.com/images/resources/PRO%20Neo-8%20Specification%20Sheet.pdf).
The [H6512 page](https://www.daytonaudio.com/product/966/h6512-6-1-2-x-12-waveguide-1-threaded)
lists 1 kHz waveguide cutoff, which is not the driver's safe crossover.

Mute, polarity and measured-EQ switches do not bypass sub/tweeter crossover or
attenuation stages. Future acoustic measurements should set the actual
crossover, relative gain, delay, polarity and subwoofer excursion limits.

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

### Browser playlist cache

The browser caches the complete playlist library and the twelve most recently
loaded playlist track lists in localStorage. Lists appear immediately from cache;
entries older than five minutes refresh in the background. Refresh playlists
forces an update. Cached data remains visible if Tidal cannot be reached.
The last library view and opened/played playlist are restored after a reload or
return from DSP, without starting playback. Playlist library display reverses
Tidal's returned order across the complete paginated collection. Track order
inside each playlist is unchanged. Only playlist metadata is cached, never
audio, credentials or stream URLs. Storage failures fall back to normal loading.

The saved listening crossover is 3.5 kHz LR4 between outputs 1–4 and 7/8,
with -12 dB tweeter attenuation. The EX32 measured preset retains its separate
2.85 kHz low-pass. Fresh installations still start the sub/tweeter pairs muted.
