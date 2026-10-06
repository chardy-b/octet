# Octet

A CamillaDSP speaker system on a Raspberry Pi 5 with a HiFiBerry DAC8x, plus the tools used to measure and EQ the speakers.

- `measure/` is the measurement and EQ tool, run on the Windows PC (`SONATA`) with an Audient iD14 and a Dayton EMM-6 mic.
- `pi/` holds the configuration of the Pi: CamillaDSP, MPD and upmpdcli (Tidal). See `pi/README.md`.

## Measuring

```
cd measure
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pytest -q tests          # 42 tests, no hardware needed
```

Run commands as `.venv\Scripts\python -m octetmeasure <command>`:

| Command | What it does |
|---|---|
| `devices`, `check` | list audio devices; show input levels for setting mic gain (use the "above 60 Hz" numbers) |
| `renderers` | find DLNA players on the network, such as the WiiM |
| `clock --play-dlna wiim --save` | measure the player's clock offset against the recorder with a 1 kHz tone, once per player |
| `record NAME --play-dlna wiim --fs 44100 --cal emm6_38327.txt` | play a sweep on a DLNA player (`--channel left\|right\|both`, `--invert right`) and record it |
| `analyze NAME` | response, phase, impulse response, signal-to-noise by band |
| `export NAME ...` | write a frequency response `.txt` and impulse response `.wav` that REW can import |
| `peq NAME ...` | fit up to 10 EQ filters (peaking, plus protective high-pass and low-pass) toward a Harman-style target |

Measurements are stored in `measure/measurements/` (raw recording, metadata, results). They are large and not in git.

## Things worth knowing

- A player and the recorder run on different clocks. At tens of ppm this blurs the top of a swept-sine measurement, so the offset is measured with a tone and corrected in analysis. The stored offset applies only to the player it was measured for.
- The Harman-style target is an approximation, not the published curve. No single numeric room target exists.
- A DML panel's response changes with mic position. Fit EQ from several positions (`peq` accepts several measurements and averages them).
- Levels are relative to digital full scale, not calibrated SPL.
- `measure/tools/` holds the audit and diagnostic scripts used to check the analysis. `tools/audit.py` is the independent check of the filter maths.
