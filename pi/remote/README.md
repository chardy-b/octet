# Octet remote music controls

Runs on the Pi at **http://100.100.177.81:8767**, bound only to its Tailscale address.

### HDMI projection and remote selection

`/visualizer` controls the Pi's selected artwork. Theme, palette, halo, response,
video framing, eclipse lighting/speed, playback position and pause state are
shared through `/api/visualizer/settings` and `/api/control/visualizer`, with the
same host/origin checks as the music controls. Settings are validated and saved
atomically in `/var/lib/octet-remote/visualizer.json`. A display heartbeat tells
the controller when the HDMI browser has applied the latest settings. Its
`display.view` report includes the actual theme, viewport dimensions, renderer,
whether the artwork fills that viewport, and the count of visible controls.
This verifies the kiosk browser even when HDMI is unplugged; it does not verify
a physical projector image.

`/visualizer/display` is the artwork-only view: a full viewport with black
surroundings, no navigation, controls, cursor or playback overlays. The Pi's
existing auto-login desktop starts `octet-visualizer.service` through the XDG
autostart entry. Chromium runs in Wayland kiosk mode with a dedicated profile;
the launcher waits for both the compositor and web service and restarts after
a browser failure. Plug the projector/display into either active HDMI output.

For visualizer-only updates, copy this directory to a staging folder on Octet
and run `sudo python3 install-visualizer.py` there. It backs up replaced files,
updates the web app and media, installs the per-user kiosk service/autostart,
and restarts only the web server and kiosk. It preserves music/DSP configuration
and their running services. This assumes Octet's existing `octet` desktop user,
UID 1000, automatic graphical login and Tailscale address.
Provides Tidal track/album search, favorites, saved playlists, queue, playback, seeking, DSP volume,
and stop/mute. Tidal credentials stay on the Pi and use the existing upmpdcli login.
No external web hosting or browser plugins are needed. The server uses Python 3
and the existing `tidalapi` package. The installer also uses the existing PyYAML.

The Music player displays title, artist, album and cover art for Tidal. For
AirPlay/live inputs it reads the Pi's Wi-Fi-only now-playing publisher through a
bounded same-host bridge (`/api/now-playing` and its artwork proxy), so the
Tailscale browser does not need direct Wi-Fi access. Stale or absent metadata
falls back to the input name; this display never changes playback.

## Audio sources

The **Speaker output levels** panel shows all eight digital playback channels
after speaker DSP, with output numbers and the configured labels (or Octet's
EX30, EX32, subwoofer and tweeter left/right assignments). Each has RMS, peak hold,
near-clipping and latched clip indicators. Output clip indicators have their own
reset button. These are CamillaDSP playback meters, not amplifier power or
acoustic SPL measurements. They share the input-meter sampler and polling request;
stalled, stopped or missing output readings show unavailable rather than old levels.

The **Input levels** panel shows every available analogue USB input, including
unselected inputs while Tidal is playing. RMS bars, a one-second peak marker and
numeric peak/RMS readings are measured before speaker volume and EQ, so they also
work with the outputs muted. **Signal** means a peak above −80 dBFS; **Near clip**
warns at −3 dBFS. The red **CLIP** indicator is triggered within 0.01 dB of digital
full scale and stays lit until **Reset clip indicators** is pressed. This detects
digital full-scale peaks; distortion earlier in an analogue source can occur
without reaching digital full scale.

Visible Music pages share one meter sampler. For the interface selected in the
DSP, it reads CamillaDSP's raw capture levels for all analogue inputs, independently
of the mono/stereo routing. Other interfaces use an in-memory `arecord` reader;
no audio is saved or sent to the browser. Capture readers release before a source
change or Auto Tune start and stop within three seconds of the last visible viewer.
Busy, disconnected and stale inputs show unavailable instead of old signal values.

The Music page's **Audio source** selector switches between Tidal and connected
USB capture inputs. With the current Audient iD14, choose **Inputs 1 / 2 · stereo**,
**Input 1 · mono**, or **Input 2 · mono**. Mono feeds both sides of the speaker
pipeline. HDMI ports and the internal ALSA loopback are not offered as line inputs.
The iD14's eight optical capture channels are excluded until a separate external
digital clock setup is implemented; they are not additional analogue sockets.

Choose a source and press **Switch source**. Switching pauses Tidal, retains its
queue and position, and leaves the output muted. For a line input, press **Unmute**
when ready. For Tidal, press **Play** to resume. Input 1 may have a live microphone
connected, so start at an appropriate listening level to avoid feedback.
The same crossover, EQ, polarity, output mutes and master level apply to all
sources. Track transport is hidden while a physical input is selected; browsing
and adding tracks to the Tidal queue remain available.

Source switching works with the **Output DSP controls** preset and with an approved
Auto Tune baseline (see [Manual source selection on an approved baseline](#manual-source-selection-on-an-approved-baseline-wil-250)).
It adds a source mixer before the existing stereo-to-eight-output pipeline and
captures all ten iD14 channels in the hardware's native format. USB inputs use asynchronous
resampling and rate adjustment to follow the DAC8x clock. Returning to Tidal
restores its original capture and clock settings. The source is saved in the
existing DSP preset and survives service restarts; boot remains muted. Each
candidate is checked by CamillaDSP before activation. A failed activation restores
the previous preset and stays muted. A disconnected source is shown as unavailable.

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

## Visualizer

Open **Visualizer** in the navigation, or `/visualizer`. The first theme,
**turell 1**, is a fixed colored disc and a soft halo inside a large circular
light field. Everything outside the outer circle is pure black.

The **Theme** selector also offers **turell 2 · Sky** (moving daytime sky inside
a fixed circular aperture), **turell 3 · Night** (a night-sky timelapse),
**turell 4 · Eclipse** (a fixed black disc and luminous corona), **Well** (four
square stone ledges opening onto stars), **turell 5 · Solar** (real solar
surface footage with a modeled eclipse, corona), **Overlap** (three translucent discs
holding sky, water and night), and **Water · Underwater** (a maximum-size circular
water movie with subtle refraction, cyan light and drifting caustic highlights).
The same selector is available in fullscreen, and the choice is remembered on
this browser. Switching themes keeps the live audio connection or simulation
running. These themes use WebGL with analytic 3D intersections, beveled
edges, soft fill/rim lighting, subtle parallax and slow independent movement.
Sky, water and stars are actual muted 4K video textures at their native frame
rates: 29.97, 60 and 30 fps. Source frames are remuxed without another lossy
encode. Solar's surface texture uses an unobscured 1080p/30 section of the
broadcast; the Moon and corona update at the renderer's cadence.
Only the videos used by the visible theme play; hidden pages pause them.
Reduced-motion preferences freeze the video and geometric motion. Devices
without WebGL show simplified geometric versions. See `visualizer-media/README.md`.
**Full screen** expands the artwork; **Esc** or **Exit full screen** returns to
the page. Move the pointer or tap to reveal the fullscreen controls. Browsers
without the Fullscreen API use a view that fills the browser window instead.

The same-origin `/api/visualizer` event stream supplies live pre-DSP input RMS
and peak levels at up to 15 Hz, using CamillaDSP's read-only
`GetSignalLevelsSince` and `GetState` commands. Source energy expands the halo,
peaks brighten it, and accumulated energy gently affects color-cycle speed.
This is envelope-based visualization, not frequency spectrum analysis. It works
with whatever audio reaches CamillaDSP, independently of listening volume or
output EQ; the source can still animate while the output is muted.
Live Pi audio continues to play on the Pi; no microphone permission is required.

Each visible viewer has one control connection; hiding or leaving the page
closes the stream. Silence settles to a still image, stale/disconnected data
fades out, and the browser reconnects automatically. Reduced-motion preferences
soften size/brightness changes and color movement. The disabled MPD FIFO stays
disabled, avoiding an additional playback timing clock. The installer copies
the visualizer scripts, styles and local media alongside the existing pages.

**Simulate music** plays the selected local MP3: **EDM Detection Mode**, an
electronic dance track, or Satie's solo-piano **Gymnopédie No. 1**, both by/
performed by Kevin MacLeod under CC BY 3.0. Track credits and license links are
shown beside the player. Native controls provide play/pause, seeking and volume;
the track selector and pause button are also available in fullscreen. Music
continues across theme changes and stops when leaving the page.

The music actually heard in the browser is analyzed with Web Audio: RMS/peak,
30–200 Hz bass, 200 Hz–3 kHz mids, 3–14 kHz highs, and spectral centroid. Smoothed
frequency balance changes the palette and surface lighting. Bass/mid/high meters
show the response. The local mode closes the Pi meter stream; **Stop simulation**
pauses the sample and reconnects to the Pi. The Pi feed remains envelope-based
and does not claim to supply frequency bands. Local music needs no Pi connection.

The inner disc stays fixed in size. Two-stage envelope smoothing gives the halo
gentle inertia, with layered atmospheric light and a tighter corona. OKLab
palette interpolation and subtle static dithering keep long color transitions
smooth. Both canvas backing surfaces allow 16 megapixels and 8192 pixels per
axis, supporting a full UHD/4K projection without the previous downscaling.

**Color cycle** shows all twelve color relationships as clickable circular
previews, including saturated magenta/periwinkle, magenta/ultraviolet and
lagoon/orchid studies drawn from the supplied references. Three aligned bands
show the field, halo and center colors throughout the entire closed cycle.
Dragging the scrubber holds an exact intermediate blend; **Resume cycle**
continues from that position. The compact scrubber also works in fullscreen.
Clicking a preview or choosing a named palette holds that pair. RGB/hex swatches
show the current field, center rim, halo and outer-edge tint.

The field, inner disc and halo all transition together; the inner disc can be
teal, apricot, amber, celadon, lavender, vermilion, pale lemon and midnight, as
well as the original periwinkle. Colors are remembered and also selectable in
fullscreen. Sky, Night and Solar use the paired field/halo while preserving the movie
inside the aperture.

**Background video** provides an independent timeline, zoom and play/pause for
Sky, Night and Lunar, beside the artwork and in fullscreen. Scrubbing updates
paused video too, without seeking or pausing the music. Zoom (50–300%) changes
sampling inside the fixed circular aperture and is remembered per theme.

### Hybrid Solar

Solar uses `sun.mp4`, an unobscured, original-cadence 18-second clip from the
beginning of the source broadcast, rather than its damaged eclipse sequence.
A stabilized source crop retains the real filaments, active regions and fine
surface detail. The Moon, its uneven limb, corona, chromosphere, illustrative
prominence loops and additional restrained sunspot groups are generated in a
separate shader. Solar has an independent 12-minute eclipse clock, not a video
seek: the path compresses ingress/egress and provides at least four minutes of
totality at 1×. Quarter speed extends that to at least 16 minutes.
This is an illustrative installation, rather than an ephemeris reconstruction.

**Eclipse mode** defaults to **Realism**: the Moon's coverage progressively
darkens the ambient field and eclipsed side of the inner halo. At totality the
colored glow recedes, revealing white coronal streamers and pink prominences.
**Artistic · colored field** retains more ambient color through totality. Both
modes use the same contact geometry and speed control; timing is stretched for
the installation rather than following a real observer's ephemeris.

**Solar color** compares **Natural · source color** with **Yellow → red cycle**;
bright yellow, amber and deep-red holds are also available. Natural preserves
the recording's filtered orange palette. The other choices grade the same
surface detail; they do not model a cooling Sun. Near contact the remaining
light resolves into beads and a diamond-ring bloom. The white corona and
pink-red chromosphere emerge as the photosphere is covered, following
[NASA's eclipse guide](https://science.nasa.gov/feature/solar-eclipse-guide/).
The Sun's apparent zoom (50–300%) scales the whole eclipse view, independently
of music. The large outer circle and black surroundings stay fixed.

**Full Sun**, **Diamond ring**, and **Totality** jump to useful comparison points.
Play/pause, scrubbing, quarter/half/normal/double/quadruple speed, treatment,
zoom and lighting controls also work in fullscreen. Eclipse speed does not
accelerate the real surface clip or coronal motion. Hiding the page,
reduced-motion preferences, and pausing the eclipse hold all solar motion.

During totality a brighter white inner corona extends into broad asymmetric
streamers and a long, soft falloff. Low-amplitude angular bending, outward
filament flow and slow exposure breathing add continuous subtle motion. This
animation has its own elapsed-time clock, so changing eclipse speed or scrubbing
the Moon does not make the corona jerk or race. The black lunar disc and the
outer circular boundary stay fixed in size.

Solar and the other WebGL themes cache separate shader programs. Surface shading
runs once per pixel; extra samples are confined to geometric silhouettes, and
Solar uses analytic pixel-width antialiasing. Video uploads occur only on decoded
frames. **Performance** shows render-loop cadence, native video cadence and
browser-reported dropped frames; it does not measure the host display refresh.

**Inner halo** compares the curated pairing, a lighter/desaturated bridge
between the field and center, and a pale hue opposite the center. Curated violet
works especially well between magenta and blue; soft violet bridges teal and
orchid, while amber/cream separates lime from periwinkle. Context and luminance
contrast matter alongside hue: a saturated complementary band can overpower
the quiet composition. **Outer edge glow** independently controls a broader
wash and narrower rim inside the outer circle. Outside remains pure black.

The central disc has an off-center radial gradient: a warmer, brighter core
transitions to a deeper, cooler rim, with a sharp silhouette and no beat-driven
scaling. Palette interpolation includes the core color so it stays coordinated
through scrubbing and cycling. The ultraviolet study uses a magenta-violet core
and electric-violet rim inspired by the user's second reference.

### Constructing the reference gradients

Keep geometry, spatial light falloff and time-based color changes independent.
For any shape, evaluate its signed distance `d` (zero on the boundary), then
layer `exp(-(d / width)^2)` light bands at different widths and intensities.
A narrow saturated band and a much wider, weaker wash suggest light scattering
without distorting the shape. A sharp interior mask preserves the flat aperture.
The same model works for circles, rounded rectangles and triangles, using the
corresponding distance function. The current implementation applies this idea
to the circular inner halo and outer-edge wash; the reference's rectangle and
triangle are design directions, not new selectable themes.

The blue/magenta reference suggests a blue field with a violet transition to
a magenta surround; the teal/orchid reference suggests a violet boundary around
a pale orchid plane against a green-blue field. These are interpretations of
the supplied images, not measurements of the original installations. Store
field, center rim, center core, halo and edge as separate colors, interpolate
them perceptually, and use smoothstep easing for slow movement through time.
Music can gently affect halo width/brightness while geometry stays still.

The palette studies draw on [UT Austin's description of The Color Inside](https://www.youtube.com/watch?v=y4XO0YJI2_k):
pink, purple, white, green and yellow light, including teal sky against saturated
watermelon illumination. [Landmarks](https://landmarks.utexas.edu/artwork/color-inside)
describes how the surrounding light alters perceived sky color. The
[Albers Foundation](https://www.albersfoundation.org/alberses/teaching/interaction-of-color)
provides the contextual-color principle behind the paired compositions; warm/cool
and complementary relationships guide the additional studies. RGB values are
digital approximations, not Turrell's lighting specifications. Perceptual OKLab
interpolation follows the [CSS Color 4](https://www.w3.org/TR/css-color/)
approach for smooth gradients.

### Turell 1 music response preview

The Music response panel offers Original, Bass Bloom, Gradient Depth, Outer Rim,
Treble Shimmer, and Combined. Combined layers bloom, depth and rim; shimmer stays
an independent comparison. Subtle, Balanced and Strong scale the light response.
Both circle radii remain constant. These choices persist and synchronize with the
fullscreen controls. Play preview music uses the selected electronic or classical
MP3; pausing lets the light settle. Selecting a response does not start audio.

New responses read the local analyser before the slower artwork smoothing:
bass opens the halo, overall dynamics increase core/rim gradient contrast,
a slower envelope breathes the outer edge, and upper-frequency energy lights a
soft narrow inner halo. Attack/release smoothing avoids random flicker, and
reduced-motion preferences attenuate the response. Original retains the previous
slow halo. The Pi feed currently carries overall RMS/peak dynamics, so live Pi
responses use those envelopes; separate bass/treble behavior requires local sample
playback. The interface explains this distinction.

Turell Lunar plays a locally stored 4K, 25 fps lunar-phase simulation from Solid
Screen. A fixed square crop fits the lunar disc to the circular aperture. The
source video audio is muted; Octet music drives the surrounding light.

### Proposed installation: Reclaimed

A restrained brutalist garden viewed from below: three enormous concrete beams
frame one offset opening to daylight. Deep reveals, board-formed grain, chamfered
edges and contact shadows make the ceiling read as architecture. The camera
stays still. Most of the image remains calm, pale concrete and black negative
space, with greenery concentrated along seams.

Vines follow predetermined branching curves through cracks, then produce small
paired leaves. Ferns unfold at a few ledges; occasional white or pale yellow
flowers open in the light. Growth unfolds over minutes, with older growth
receding in shadow before a gentle loop. Avoid covering the whole composition
or creating a rapidly changing jungle.

Rendering direction: instanced leaf meshes along spline stems, progressive
curve reveal, concrete normal/roughness maps, ambient occlusion and leaf contact
shadows. Translucent leaf lighting and one slowly drifting cloud shadow provide
depth. Sustained musical energy can slightly accelerate growth and warm the
daylight; individual beats should never move or resize the concrete structures.
This concept is not yet a selectable theme.

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

Expand **Always active** on any assigned output to edit attenuation (−60 to 0 dB),
high-pass and low-pass frequencies in numeric text fields, then press **Save output**.
Values apply only when saved, independently for that output, even when its original
filters were shared with another channel. Filter types, slopes, Q, measured EQ,
polarity and channel mute are preserved. Frequencies must be above zero and below
half the DSP sample rate, with high-pass below low-pass. Outputs without a level
filter get a separate attenuation stage when a nonzero value is saved. Unsaved
entries survive status refreshes; successful saves use the same validated,
briefly muted reload and persistent configuration as the output switches.

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

### Stream startup prebuffer

Progressive Tidal audio is served through a bounded proxy instead of an HTTP
redirect. The endpoint reads the first 1 MiB before releasing audio to MPD, then
forwards the remaining bytes unchanged in 64 KiB chunks. Byte-range requests and
206/Content-Range responses are preserved for seeking. Audio is not written to
disk, and signed CDN URLs remain server-side. This can add a short initial wait
in exchange for more data being ready when playback starts. DASH manifests keep
the existing path. A failure after response headers closes the connection instead
of appending a JSON error to audio.

The accompanying MPD configuration uses a 16 MB decoded buffer and a bounded
128 MB input cache. The separate browser playlist cache stores metadata only.

## Automatic system tuning

The DSP page includes **Auto mode** and a **Saved system profiles** dropdown.
With the calibrated EMM-6 on iD14 input 1, the Pi runs protected per-output sweeps,
fits EQ, searches 150–500 Hz sub/panel and 3.5–5 kHz panel/tweeter handoffs,
compares delay/polarity/attenuation, then measures actual candidate sums and the
final CamillaDSP path. One fixed microphone position is used in this mode.
A successful run is named, saved and applied at −36 dB with master mute enabled.
Starting music and unmuting remain deliberate listening actions.

The independent 3.5 kHz tweeter safety high-pass cannot be lowered by Auto mode or
the manual frequency editor. Stop tones cancels direct DAC playback; Restore
before tuning recovers pre-run settings. While the tuner owns the audio devices,
music, source switching and manual DSP mutations are blocked. Saved profile
recall verifies the stored checksum and protective filters without playing tones.
The current manual configuration is also saved in the dropdown before each run.
See [Auto Tune operating guide](../../measure/docs/AUTO_TUNE.md) for deployment
paths, guided multi-position tuning and recovery.

### Saved-profile measurement graphs

Select a saved profile on the DSP page and press **View measurements**. This is
read-only and works independently of the active listening profile. Left/right
system plots compare the measured response before room EQ with the final measured
response and target shape. The output selector shows individual driver response,
predicted response with DSP, measured/predicted phase, delay, polarity and relative
attenuation. Hover or use arrow keys to read graph values. Levels are relative dB,
not calibrated SPL. Manual settings backups show an explicit no-measurements
message; they do not fabricate an acoustic result.

## Classic AirPlay (WIL-248)

Select **Octet** in an iPhone/Mac's AirPlay audio picker on the same LAN. A cast
pauses MPD without clearing its queue or position, selects the protected speaker
DSP, sets master to **−18 dB**, and unmutes only after both loopback endpoints
are RUNNING at 44100 Hz / S32_LE / stereo, capture application and hardware
pointers advance, and DSP readiness checks pass. Phone volume is Shairport software attenuation; 100% phone
volume does not raise the DSP master. Deliberate remote volume changes remain
available. Output pair mutes, crossover, protection filters and EQ are preserved.
Ending playback mutes; Tidal never resumes itself. With the classic channel profile,
AirPlay stays selected: choose Tidal explicitly and press Play to resume the queue.
With an approved Auto Tune baseline, the original baseline path and exact config
are restored on stop or failure, still muted.

The UI distinguishes selected/waiting, preparing, active, and failed AirPlay.
Manual source selection stays muted. Auto Tune excludes automatic playback;
a missing receiver, loopback, unsupported preset or failed reload stays muted.
Manual source selection works on the channel preset or, with its own journal, on an
approved baseline (WIL-250, below). Automatic AirPlay also supports a SHA-verified `TUNING.active(path)` baseline directly under
`/var/lib/octet-autotune/baselines`, provided its loaded JSON exactly equals the
baseline YAML and it uses stereo Tidal loopback at a 48 kHz DSP rate. Unsupported
configs fail closed; there is no fallback to the 37-filter channel preset.

Baseline AirPlay uses `/var/lib/octet-dsp/airplay-baseline.yml` (0640), copied from
the exact loaded config with only capture/clock routing changes. The original
baseline is never written: all 129 filters in the verified baseline, pipeline,
mixers, gains, output protections and playback device remain unchanged. The
candidate must pass CamillaDSP `--check`. A bounded 0600 `airplay-recovery.json`
journal records the original path and SHA-256, with fdatasync and directory fsync
completed before `SetConfigFilePath(candidate)` followed by `Reload`. Readiness
requires exact candidate config/path, unchanged approved baseline digest, running
DSP and advancing capture PCM before setting −18 dB and unmuting.

Stop/failure/restart mutes and restores using `SetConfigFilePath(original)` then
`Reload`; cleanup follows verification of original path, exact config, running
DSP and baseline digest. Failed recovery retains the journal and mute/retry latch.
Manual controls end baseline AirPlay and complete recovery under the shared lock
before proceeding; this deliberately narrow version does not edit baseline files
or expose ephemeral profiles as general editable presets. Auto Tune handoff uses
the same recovery gate. If Auto Tune already owns audio or has selected another
path, mute and retain recovery evidence without overwriting its configuration.
Recovery requires ownership to be released and a recognized path plus the original
approval/digest; unknown, changed, malformed or oversized state blocks playback.
An Auto Tune stop request remains available while recovery is blocked. Never delete
the journal to bypass a failed restore. Remote initialization recovers before
allowing AirPlay; a persisted route never implies a live cast.
Boot and remote restart mute; receiver loss is monitored, and remote service
exit has a mute hook. These are software safeguards: if DSP control itself is
unreachable, a mute command cannot be guaranteed until connectivity returns.

### Install or upgrade on the Pi

These commands are deployment instructions, not commands to run on a build host.
First provision Debian's classic-only `shairport-sync`, `avahi-daemon` and
`alsa-utils` packages through the normal OS maintenance process. The AirPlay
installer never installs packages. AirPlay 2 builds are deliberately rejected
pending separate capability verification; no NQPTP is needed.

Upgrade the remote from this checkout with `sudo python3 pi/remote/install.py`,
then run `sudo python3 pi/remote/install_airplay.py`. The remote installer preserves
an existing channel DSP file, including output mutes. The AirPlay installer checks
ALSA/Avahi binary capabilities using `shairport-sync -V`, resolves the Loopback
card index and `snd_aloop` driver, and verifies both directions on subdevice 1.
Open endpoints must have matching negotiated parameters; closed endpoints are
opened nonblocking in bounded helpers to negotiate the exact format without
playing or recording samples. It validates a candidate against the Pi's CamillaDSP
binary, atomically installs the reviewed receiver config and service overrides,
and restarts the remote muted. It enables disabled receiver/Avahi units, preserves
static/indirect/alias/linked enable states, starts the services, and checks each
service individually plus socket readiness. A failed activation
restores its backed-up files and service state. No MPD or DSP service configuration
is changed by the AirPlay installer.

MPD owns `hw:Loopback,0,0` → `hw:Loopback,1,0`. Shairport owns
`hw:Loopback,0,1` → `hw:Loopback,1,1`, **44100 Hz / S32_LE / stereo**. Existing
`snd-aloop pcm_substreams=2` is required; there is no `plughw`, DAC or MPD fallback.
Camilla's asynchronous input resampler converts to the 48000 Hz speaker pipeline.
Confirm the installed Shairport version honors the supplied options and reports
this PCM format during a real cast. Package/version checks and negotiation probes
are not substitutes for that acceptance test. Configuration follows the
[upstream Shairport sample](https://github.com/mikebrady/shairport-sync/blob/master/scripts/shairport-sync.conf).

`/run/octet-airplay/control.sock` is accessible only through the dedicated
`octet-airplay` group and verifies Linux peer credentials against the
`shairport-sync` UID. It accepts only `start`/`stop`; there is no HTTP hook,
password, client metadata or sudo grant. The service user can trigger the bounded
startup level but cannot submit arbitrary commands. The before-play hook prepares
the route muted and returns, allowing PCM to begin; the monitor then verifies
capture and applies the startup level. Waiting for live PCM inside the blocking
hook would deadlock playback. Hook preparation has a 20-second response deadline;
a timeout cancels automatic unmute even if a blocked preparation operation later
returns. Only one preparation worker can run at a time. Unmute also requires a
successfully acknowledged hook response.

Shairport's hooks contain no session generation. Stop events are debounced for
one second and checked against running subdevice-1 playback and progressing capture. A stop/start pair resets
the startup level; a stale stop while PCM is running is ignored. Manual override
suppresses duplicate starts until PCM goes idle. This deliberately favors staying
muted if a very rapid replacement cast cannot be distinguished from the overridden
cast: stop casting, wait a second, and cast again. Missing hooks/crashes are caught
by service and PCM monitoring; initial readiness has a ten-second deadline.
Manual controls, hook changes and Auto Tune requests use the same control lock.

### Network, rollback and acceptance

Discovery uses Avahi/mDNS on the LAN, not Tailscale discovery. Permit mDNS UDP 5353,
classic RTSP TCP 5000 and receiver UDP 6001–6010 on trusted LAN interfaces only;
do not forward these ports to the internet. AirPlay video and AirPlay 2 are out of
scope. The existing remote remains on its configured Tailscale address.

`sudo python3 pi/remote/install_airplay.py --rollback` stops the receiver and
restores managed config/overrides and enabled/active service states saved at the
**first** installation in `/var/backups/octet-airplay`. Repeated installs keep that
original rollback point. Backups and restoration preserve file ownership and mode.
Rollback verifies restored files and service states, continues independent recovery
steps after errors, and retains backups for an idempotent retry. If recovery is
incomplete it reports all failures and does not restart the receiver. It leaves
the inert system group and OS packages in place.
Keep the matching checkout to run rollback. Remote-code downgrade uses the normal
remote deployment backup separately; roll AirPlay back before removing its helper.
Review prior receiver configuration before restoring any older deployment that
may have routed audio directly to hardware.

Physical acceptance is still required: verify the actual ALSA pairing and binary
options; discover Octet from an iPhone/Mac; cast and inspect both DSP input meters;
confirm audible left/right output through protected filters; check MPD queue and
position retention, −18 dB startup at maximum phone volume, output pair mute
retention, source round trips, tuning exclusion, stop/start and service failure.
Do not claim end-to-end playback from mocked tests alone.

## Manual source selection on an approved baseline (WIL-250)

**Software-tested only; not validated on the Pi, with no physical speaker output.**
Tidal, AirPlay and USB inputs can be chosen while a SHA-verified Auto Tune baseline
(`TUNING.active(path)`, directly under `/var/lib/octet-autotune/baselines`) is the
active speaker profile. `CHANNEL_PROFILE` is not required and is never loaded as a
substitute. `source_switching_available` and a concise `source_switching_reason` in
`/api/status` say whether a manual change is safe now; the selector is disabled with
that reason for Auto Tune ownership (or unknown ownership), an unapproved, altered or
unsupported configuration, a stopped DSP, or pending recovery. The DSP page and
status keep the baseline's name and show it as a read-only profile with the input
shown beside it; they never present the route as an editable channel preset. The
channel preset keeps its existing in-place, persistent source behavior.

**The route.** `manual_source_route.py` derives a candidate from the *original*
baseline bytes (never from a previous candidate, so input stages cannot accumulate).
`audio_sources.verify_output_equivalence` requires the entire speaker side (pipeline,
filters, mixers, playback device, polarity, mutes, gains, protections, title and every
other key) to be exactly equal; only the one first `octet_source` mixer and the capture
and clock fields may differ, and CamillaDSP's parsed defaults are tolerated on the
input side only. Only a stereo Tidal-loopback original at 48 kHz feeding an explicit
eight-output DAC is accepted. `source_metadata` now rejects malformed, duplicate,
unexpected or ambiguous source records before anything changes. The baseline file is
never written. A candidate passes CamillaDSP `--check`, then a 0600 bounded
`/var/lib/octet-dsp/manual-source-recovery.json` (original path, SHA-256, a generation,
the previous generation and the source) is created exclusively with fdatasync and
directory fsync **before** `SetConfigFilePath(candidate)` and `Reload`. Selection mutes
and pauses MPD without clearing it, and success stays **muted**: Tidal needs Play and
a line input needs Unmute. The route returns to the original path and exact config
(muted, DSP running, mute read back) on a failed reload, wrong loaded config, lost
mute or when Tidal is selected; only then are candidates and the journal deleted.
A second manual switch writes a new generation and keeps the previous one in the
journal until the new one is verified. Selecting the current source is a no-op. A
restart does not keep a manual route: startup recovers the baseline, muted.

**One owner at a time.** The manual journal, `airplay-recovery.json` and
`bluetooth-recovery.json` are mutually exclusive, together with the existing
cross-process `audio.lock` lease and `server.LOCK`. Startup order is: confirm mute,
manual recovery, AirPlay recovery, Bluetooth recovery. A manual journal beside either
other journal, an active AirPlay or Bluetooth owner, an Auto Tune owner, an unknown
owner, a foreign candidate path, or a changed approval/digest keeps output muted,
retains every file and refuses recovery, source changes and unmute until an operator
resolves it. AirPlay and Bluetooth recovery refuse over a manual journal for the same
reason. Manual selection first stops and restores an automatic AirPlay or Bluetooth
owner (muted); if that fails it stops there. A fresh AirPlay cast restores a manual
route before it prepares its own baseline route; Bluetooth start is refused while a
manual route is loaded. `approved()` recognises a candidate path only for this
process's journaled route with exact loaded content. Auto Tune actions restore the
manual route first. Never delete a journal to bypass a failed restore; a retry runs
on the next control (for example Mute).

**Not covered.** CamillaDSP has no compare-and-set, so a non-cooperative websocket
client (CamillaGUI) is detected by readback, not prevented. Software tests use a
synthetic 129-filter baseline and a simulated CamillaDSP; they do not show that any
real Pi baseline can be routed, that USB capture opens, or that sound reaches the
speakers. Deployment, restarts and attended low-level listening with graph, PCM and
rollback checks remain separate approved steps.

## Bluetooth A2DP manual receiver (WIL-249)

**Not yet hardware-validated. Disabled by default.** No phone has played through
this path on the Pi. The Pi's loopback sub2 has been provisioned and tested
(operator-reported; not re-observed from the software sessions). See the
[manual receiver notes](../../docs/superpowers/specs/2026-10-27-bluetooth-manual-receiver.md)
for exactly what is and is not verified.

`python3 pi/remote/bluetooth_preflight.py` performs a read-only prerequisite check
and prints JSON (exit 0 for prerequisites present, 1 for blockers). It requires
working BlueALSA daemon/player binaries and idle, exact snd_aloop subdevice-2
playback/capture endpoints. MPD keeps subdevice 0 and AirPlay keeps subdevice 1.
A two-substream `snd-aloop` intentionally fails this check. The Pi now has the
third substream, but `install/setup.sh` still writes `pcm_substreams=2`; do not
re-run it on the Pi until that is reconciled.

`sudo python3 install_bluetooth.py` is a dry run; `--apply` refuses on any preflight
blocker. It installs only a **disabled** `/etc/octet/bluetooth.json`
(root:octet-bluetooth 0640, never overwritten), an octet-remote drop-in for the
root-only `/run/octet-bluetooth/control.sock`, and `octet-bluealsa.service`. That
unit is an A2DP-sink-only daemon with no `[Install]` section, so it cannot be
enabled and never starts at boot. Unit enable/active state is recorded before and
re-read after; any change is reported, never altered. It never restarts
octet-remote, enables, starts, stops or masks any unit, enables discoverability or
changes audio. `--rollback` restores the backed-up files and likewise leaves unit
state alone; it refuses while octet-bluealsa is running. This describes a
disabled installation only, **not** an authorization to activate the receiver.

**BLOCKED — Bluetooth playback, pending attended physical acceptance:** On the
first phone connection, BlueALSA transport churn coincided with WirePlumber
link failures. PipeWire opened Loopback playback sub0 (reserved for MPD);
CamillaDSP then logged ALSA capture errno 77 and became Inactive. No Bluetooth
bridge was started or unmuted. This correlation is **not** proof of the capture
failure's cause.

An initial muted trial of `pi/install/wireplumber-bluealsa.conf` lost PipeWire's
Loopback and DAC nodes when WirePlumber restarted while MPD and CamillaDSP held
their ALSA devices. Removing the exact fragment and restarting WirePlumber
**did not** restore those nodes. A subsequent controlled recovery paused MPD,
stopped the muted CamillaDSP, verified all three reserved PCM endpoints had no
owners, and restarted the `octet` user WirePlumber with the fragment absent.
Loopback sink/source and DAC nodes returned; CamillaDSP restarted Running and
muted on the approved baseline with the Tidal queue intact. This supports an
ownership-sensitive WirePlumber enumeration boundary, not proof that the
Bluetooth collision has been fixed.

The same owner-free sequence was then used for a **revised configuration trial**:
the exact hash-checked fragment disabling only WirePlumber's `monitor.bluez` was
published with no pre-existing destination (no-replace); `octet` WirePlumber restarted
with MPD paused and CamillaDSP stopped. Loopback sink/source and DAC nodes
remained present, and CamillaDSP restarted Running/muted with the baseline
semantically identical to its approved YAML. Manual `octet-bluealsa.service`
registered its A2DP sink while policy remained disabled, without losing the
PipeWire graph, then was stopped. The fragment remains installed; Bluetooth
policy is disabled/empty and the phone untrusted/disconnected. **No phone audio
or physical speaker output was tested in this trial.**

Any future WirePlumber restart or fragment rollback must be a separately
approved muted maintenance operation: preserve the queue and baseline, pause
MPD until Loopback playback sub0 is released, stop muted CamillaDSP until its
Loopback capture and DAC playback are released, then restart the *octet user*
WirePlumber. Verify the named Loopback and DAC PipeWire nodes **before** starting
CamillaDSP with startup mute; read back DSP Running/muted and the original queue.
On failure, keep Bluetooth disabled, stop BlueALSA, remove only the exact trial
fragment **only after** verifying it is a root-owned regular 0644 file with the
reviewed SHA-256 `07bea466e0ed9f1b582a32faf57f7ee638cc6adf987b4a17bbc0464c77af1c71`.
If its identity or hash differs, refuse deletion and investigate; there is no
pre-existing fragment to restore because publication refused any destination.
Repeat the owner-free WirePlumber restart and graph checks; never retry blindly
or unmute to test. No pairing, trust, receiver start, or playback
is authorized until a new attended speaker check is approved. Passing a
no-phone service/graph trial is not end-to-end Bluetooth acceptance.

The policy has exactly one valid form for every reader: `root:octet-bluetooth`,
mode `0640`, in root-owned, non-writable, world-searchable directories. A root-only
`0600` file is refused everywhere, because octet-remote (`upmpdcli`) could not read
it and `start` would be impossible after a successful pairing.

The unvalidated controller design preserves the MPD queue, uses only the dedicated
Loopback sub2 pair and an approved Auto Tune baseline, and fails muted on source
or ownership drift. It does not make a non-cooperative CamillaDSP websocket
writer safe. The desktop graph and DSP-writer exclusion remain prerequisites
for any future physical acceptance; passing software tests does not waive them.
