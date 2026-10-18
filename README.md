# Octet

A CamillaDSP speaker system on a Raspberry Pi 5 with a HiFiBerry DAC8x, and the app used to measure and EQ the speakers.

| Folder | What it is |
|---|---|
| [`measure/`](measure/README.md) | **Octet Measure**: a browser app (and command line) that records a loudspeaker with a measurement microphone, shows the results, and fits a parametric EQ. Windows, macOS and Linux. |
| [`pi/`](pi/README.md) | Configuration of the Raspberry Pi: CamillaDSP, MPD and upmpdcli (Tidal). |

## Try the measurement app

```
cd measure
pip install .
octet-measure ui --demo        # simulated hardware, nothing to connect
octet-measure                  # the real thing
```

See [`measure/README.md`](measure/README.md) for what it does, how to set it up on each system, and how to develop it. The tests need no hardware: `python -m pytest` in `measure/`.

## Project plan and hardware

- [Phase 2 plan](docs/PHASE_2_PLAN.md): the accepted Auto Tune design and decisions for version 0.2.0.
- [Speaker system and hardware](docs/SYSTEM_HARDWARE.md): drivers, panels, amps, Pi, DAC, microphone, proposed wiring and remaining choices.
- [Original phase 2 proposal](docs/archive/PHASE_2_ORIGINAL_PLAN.md): the supplied plan preserved verbatim; some decisions were superseded.
- [Auto Tune operating guide](measure/docs/AUTO_TUNE.md): setup, commands, verification and recovery.
