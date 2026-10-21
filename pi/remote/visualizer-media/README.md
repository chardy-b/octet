# Local audiovisual media

Frames sampled with yt-dlp and FFmpeg from the user's supplied references:

- `sky.jpg`: https://www.youtube.com/watch?v=7SuLhTF0SKk&t=32333s
  (sample begins at 08:58:53).
- `water.jpg`: https://www.youtube.com/watch?v=qVCyXG2-ecQ
  (sample begins at 00:00:10).
- `stars.jpg`: https://www.youtube.com/watch?v=Bbp1-p2FoXU
  (sample begins at 00:00:30).

All three nature videos use the highest resolution listed by YouTube on
2026-10-19: **3840 × 2160**, without scaling or baked-in cropping. Original
compressed video packets are copied into fast-start MP4 containers, with no
additional lossy encoding, frame-rate conversion or slowed playback.

| Asset | YouTube format | Codec | Native cadence | Local duration |
|---|---|---|---|---|
| `sky.mp4` | 313 | VP9 | 30000/1001 (29.97) fps | 92.06 s |
| `water.mp4` | 401 | AV1 | 60 fps | 60 s |
| `stars.mp4` | 401 | AV1 | 30 fps | 34.57 s |

Keyframe preroll can slightly extend a remuxed excerpt. Clips loop at their
boundaries; the previous baked crossfade was removed to preserve the source
video frames. All source videos already contain YouTube's compression.
Full-resolution JPEGs provide sharp still fallbacks while loading or when
reduced motion is enabled. Inactive/hidden themes pause video decoding.
The renderer retains the full video frame in its texture and crops only when
fitting the circular aperture. Both canvases allow 16,777,216 pixels and 8192
pixels per axis, so UHD/4K output is no longer downscaled by the old limit.
Actual playback smoothness depends on the display refresh rate and the device's
VP9/AV1 decoder and GPU. No YouTube connection is needed during playback.

## Hybrid Solar surface

`sun.mp4` is the first 18 seconds (00:00:00–00:00:18) of
[Solar eclipse 2026: LIVE from Spain on August 12 (FULL)](https://www.youtube.com/watch?v=8GeNWKnfWaQ),
credited by the publisher to timeanddate.com. The unobscured telescope disc has
filaments, bright active regions and atmospheric surface motion. Its original
1920 x 1080 H.264 video packets were stream-copied into a silent, faststart MP4,
without a new lossy encode or frame-rate conversion. Native cadence is 30 fps;
834,506 bytes. The 18-second excerpt loops at native speed. `sun.jpg` is its
full-frame fallback. A shader crop fits the ~868-pixel solar disc and follows
the small telescope drift. 1080p is the source-frame resolution, not a claim of
4K detail within the telescope disc. Source audio is omitted.

The hybrid mode generates the Moon, contact beads, diamond-ring bloom, corona,
chromosphere, illustrative prominence loops and additional restrained sunspot
groups in code. No NASA imagery is embedded. Contact-layer
references: [NASA's eclipse guide](https://science.nasa.gov/feature/solar-eclipse-guide/)
and [diamond-ring description](https://science.nasa.gov/resource/putting-a-ring-on-it-2017-total-solar-eclipse/).
Natural retains the original filtered source colors; yellow-to-red is an artistic
grade. The 12-minute installation cycle holds totality for at least four minutes
at 1× and is not an ephemeris calculation. Realism is the default lighting mode;
lunar coverage dims the colored halo before the white corona is revealed.

`solar.mp4` / `solar.jpg` retain the former 01:24:00–01:40:00 AV1 broadcast
excerpt as an archived comparison. The visualizer no longer loads these assets;
its source defects are documented below. The UI links to and credits the original
broadcast for the real solar texture.

### Eclipse integrity check

On 2026-10-19 the local AV1 excerpt was checked against the independently stored
full AV1 source. All 28,800 compressed image packets matched SHA-256 after
normalizing nonvisual AV1 temporal delimiters (the original has two-byte OBU
delimiters that the MP4 remux removes). Decoded one-second windows at excerpt offsets 0, 60, 210, 270
and 330 seconds matched SHA-256 hashes exactly, with no decoder errors. Visual
comparison with the separate original H.264 download shows the same blackout at
3:30, banding at totality around 4:30, and overexposed camera cut at 5:30. These
are present before the local trim and shader crop. Downloading or remuxing did
not introduce those defects. We retain the original packets rather than hiding
source problems with an additional lossy encode. The hybrid version instead uses the clean, unobscured opening footage.

## Sample music and required attribution

The original, unmodified MP3 files were downloaded from the artist's site.
Both are available under Creative Commons Attribution 3.0:
https://creativecommons.org/licenses/by/3.0/

- `edm.mp3`: **EDM Detection Mode**, Kevin MacLeod (incompetech.com).
  ISRC USUAN1500026.
  Source: https://incompetech.com/music/royalty-free/mp3-royaltyfree/EDM%20Detection%20Mode.mp3
  Details: https://www.incompetech.com/music/royalty-free/index.html?collection=18&page=2
- `classical.mp3`: **Gymnopedie No. 1**, original composition by Erik Satie;
  arrangement/performance by Kevin MacLeod (incompetech.com).
  ISRC USUAN1100787.
  Source: https://incompetech.com/music/royalty-free/mp3-royaltyfree/Gymnopedie%20No%201.mp3
  Recording/license: https://commons.wikimedia.org/wiki/File:Gymnopedie_No._1_(ISRC_USUAN1100787).mp3

The visible sample player includes the title, artist/source, and license link.
The same decoded audio is sent to the speakers and the Web Audio analyzer;
simulation does not synthesize a beat or play audio through the Pi.

## Lunar phase movie

`lunar.mp4`: first 480 seconds of [10 hours - Timelapse moon phases with music
in 4k - Moon screensaver](https://www.youtube.com/watch?v=YrK0kwqL_-E), Solid
Screen. The publisher describes it as a simulation based on lunar photographs;
it is not live telescope footage. The highest available source is 3840 x 2160,
AV1, 25 fps (format 401). Original video packets were copied into a faststart
MP4 with no additional lossy encoding or frame-rate conversion. 68,506,809 bytes.
The eight-minute excerpt loops at its boundary; no interpolation or crossfade is
baked into the original frames. Source audio is omitted. `lunar.jpg` is a native
4K fallback frame. A fixed shader crop centers the Moon and fills the aperture.
