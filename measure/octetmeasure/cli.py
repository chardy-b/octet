"""Command line interface.

  python -m octetmeasure devices
  python -m octetmeasure check                     # input levels for gain staging
  python -m octetmeasure sweep-file                # WAV to play from the Pi
  python -m octetmeasure record NAME --trigger "ssh octet@octet mpc play" --ref
  python -m octetmeasure analyze NAME --cal emm6.txt --gate-ms 8
  python -m octetmeasure renderers                 # find the WiiM
  python -m octetmeasure record NAME --play-dlna wiim --fs 44100
  python -m octetmeasure export NAME [NAME ...] --cal emm6.txt   # files for REW import
  python -m octetmeasure peq NAME [NAME ...] --cal emm6.txt     # fit up to 10 PEQ filters
  python -m octetmeasure clock --play-dlna wiim --save            # measure the WiiM clock offset once
  python -m octetmeasure ui [--demo]                              # the browser app
  python -m octetmeasure list
"""
import argparse
from pathlib import Path

import numpy as np

from . import __version__, analysis, capture, clock, paths, peq as peqmod, peq_workflow, rew_export, store, upnp
from .cal import load_cal
from .sweep import make_sweep, padded_playback, stereo_playback
from .wavio import write_pcm_wav

# empty means the system default device; set "in_device" / "out_device" in ~/.octetmeasure/config.json to change it
DEFAULT_IN = paths.load_config().get("in_device", "")
DEFAULT_OUT = paths.load_config().get("out_device", "")


def _sweep_args(p):
    p.add_argument("--fs", type=int, default=48000)
    p.add_argument("--duration", type=float, default=10.0, help="sweep length, s")
    p.add_argument("--level", type=float, default=-12.0, help="sweep level, dBFS")
    p.add_argument("--f1", type=float, default=20.0)
    p.add_argument("--f2", type=float, default=20000.0)


def _make(a):
    return make_sweep(a.fs, a.duration, a.f1, a.f2, a.level)


def cmd_devices(a):
    for d in capture.list_devices():
        if a.all or d["api"] == capture.preferred_api():
            print(f"{d['index']:3d}  {d['api']:<18} in={d['inputs']:<2} out={d['outputs']:<2} {d['name']}")


def cmd_check(a):
    dev = capture.find_device(a.in_device, "input")
    print(f"Recording {a.seconds:.0f} s from {a.in_device!r} - make some noise or stay quiet to see the noise floor.")
    for c, lv in enumerate(capture.level_check(dev, a.fs, a.seconds)):
        flag = "  <-- CLIPPING" if lv["peak"] > -1 else ("  (very low)" if lv["peak"] < -60 else "")
        print(f"  ch{c}: peak {lv['peak']:6.1f} dBFS  rms {lv['rms']:6.1f}   |  above 60 Hz: peak {lv['band_peak']:6.1f}"
              f"  rms {lv['band_rms']:6.1f}{flag}")
    print("Set gain using the 'above 60 Hz' numbers; low-frequency room rumble can dominate the plain meter.")


def cmd_sweep_file(a):
    sw = _make(a)
    y = stereo_playback(sw, a.channel, a.lead, a.tail, a.invert)
    write_pcm_wav(a.out, sw.fs, y, a.bits, 2)
    print(f"Wrote {a.out}  ({len(y) / sw.fs:.1f} s, stereo, {sw.fs} Hz, {a.bits}-bit, sweep on {a.channel}, {a.level} dBFS)")
    print("Copy it to the Pi's MPD music folder, e.g.:")
    print(f"  scp {a.out} octet@octet:/tmp/ && ssh octet@octet 'sudo install -o mpd -g audio /tmp/{Path(a.out).name} /var/lib/mpd/music/ && mpc update --wait'")


def cmd_renderers(a):
    rs = upnp.discover(a.timeout)
    for r in rs:
        print(f"{r.ip:<16} {r.name}  [{r.model}]")
    if not rs:
        print("No UPnP/DLNA renderers found. Is the device on the same network as this PC?")


def cmd_record(a):
    sw = _make(a)
    in_dev = capture.find_device(a.in_device, "input")
    player = "local" if a.play_local else ("external: " + a.trigger[:60] if a.trigger else "external")
    if a.play_local:
        out_dev = capture.find_device(a.out_device, "output")
        y = padded_playback(sw, 0.5, 1.0)
        print("Playing the sweep from the PC and recording...")
        rec = capture.play_and_record(in_dev, out_dev, y, sw.fs, 2, out_channel=0)
    elif a.play_dlna:
        r = upnp.find_renderer(a.play_dlna)
        player = r.name
        d = store.folder(a.name)
        d.mkdir(parents=True, exist_ok=True)
        wav = d / "sweep.wav"
        write_pcm_wav(wav, sw.fs, stereo_playback(sw, a.channel, 2.0, 3.0, a.invert), a.bits, 2)
        srv = upnp.FileServer(wav, upnp.local_ip_for(r.ip))
        seconds = a.seconds or sw.duration + 20
        print(f"Playing on {r.name} ({r.ip}) via DLNA ({a.channel} channel), recording {seconds:.0f} s. "
              "Set the amp's volume first and turn off its EQ/loudness.")
        try:
            rec = capture.record(in_dev, seconds, sw.fs, 2, lambda: upnp.play(r, srv.url), a.trigger_delay)
        finally:
            upnp.stop(r)
            reqs = list(srv.requests)
            srv.close()
        if not reqs:
            print("WARNING: the renderer never fetched the file. Check the Windows firewall "
                  "(allow Python on private networks) and that the PC and renderer share a network.")
    else:
        seconds = a.seconds or sw.duration + 10
        print(f"Recording {seconds:.0f} s" + (f"; running trigger after {a.trigger_delay} s" if a.trigger else
                                               " - start playback now"))
        rec = capture.record(in_dev, seconds, sw.fs, 2, a.trigger, a.trigger_delay)
    peaks = 20 * np.log10(np.maximum(np.max(np.abs(rec), axis=0), 1e-9))
    band = [capture.band_levels(rec[:, c], sw.fs)[0] for c in range(rec.shape[1])]
    print("Recorded peak levels: " + ", ".join(f"ch{c} {p:.1f} dBFS (above 60 Hz: {b:.1f})"
                                               for c, (p, b) in enumerate(zip(peaks, band))))
    d = store.save_recording(a.name, rec, sw, in_device=a.in_device, ref_ch=1 if a.ref else None,
                             mic_ch=0, note=a.note, channel=a.channel, invert=a.invert, player=player)
    print(f"Saved {d}")
    if not a.no_analyze:
        _analyze(a.name, cal_path=a.cal, gate_ms=a.gate_ms, smooth=a.smooth, ref_ch=None, mic_ch=None)


def _plot(fn: str, *args) -> bool:
    """Draw a PNG with matplotlib if it is installed. The browser app does not need it."""
    try:
        from . import plotting
    except ImportError:
        print("(Plots need matplotlib: pip install matplotlib)")
        return False
    getattr(plotting, fn)(*args)
    return True


def _clock_arg(value, meta=None) -> float:
    """--clock-ppm: 'stored' (the value measured with the `clock` command, for the player that made the recording),
    'off', or a number."""
    v = str(value).lower()
    if v == "stored":
        return store.get_clock_ppm((meta or {}).get("player"))
    if v in ("off", "none"):
        return 0.0
    return float(v)


def _analyze(name, cal_path, gate_ms, smooth, ref_ch, mic_ch, clock_ppm="stored"):
    rec, sw, meta = store.load_recording(name)
    mic = meta.get("mic_ch", 0) if mic_ch is None else mic_ch
    ref = meta.get("ref_ch") if ref_ch is None else (None if ref_ch < 0 else ref_ch)
    cal = load_cal(cal_path) if cal_path else None
    ppm = _clock_arg(clock_ppm, meta)
    res = analysis.measure(rec, sw, mic_ch=mic, ref_ch=ref, post_ms=gate_ms,
                           smoothing=smooth or None, cal=cal, clock_ppm=ppm)
    d = store.folder(name)
    store.save_response_csv(d / "response.csv", res.response)
    title = f"{name}  (gate {gate_ms} ms)" if gate_ms else name
    drew = _plot("plot_result", res, d / "response.png", title)
    print(f"Arrival {res.peak / res.fs * 1000:.2f} ms into the recording"
          + (f", acoustic delay vs reference {res.delay_ms:.3f} ms ({res.delay_ms * 34.3:.1f} cm)"
             if res.delay_ms is not None else ", no timing reference"))
    if res.snr_bands is not None:
        fc, snr = res.snr_bands
        pts = []
        for f in (125, 250, 500, 1000, 2000, 4000, 8000, 16000):
            i = int(np.argmin(np.abs(fc - f)))
            if abs(np.log2(fc[i] / f)) < 0.4 and np.isfinite(snr[i]):
                pts.append(f"{f / 1000:g}k: {snr[i]:.0f}" if f >= 1000 else f"{f}: {snr[i]:.0f}")
        worst = np.nanmin(snr)
        print(f"SNR by band (Hz: dB)  {'  '.join(pts)}   | worst {worst:.0f} dB, median {res.snr_db:.0f} dB"
              + ("  (low - raise mic gain or play louder)" if worst < 30 else ""))
    if res.clipped:
        print("WARNING: recording clipped - lower the input gain or sweep level and repeat.")
    print("Calibration: " + (f"{cal_path}" if cal else "none applied (high frequencies will be uncorrected)"))
    print(f"Player clock offset corrected: {ppm:+.1f} ppm" + ("" if ppm else "  (none measured: run the `clock` command)"))
    print(f"Wrote {d / 'response.csv'}" + (f"\n      {d / 'response.png'}" if drew else ""))


def cmd_analyze(a):
    _analyze(a.name, a.cal, a.gate_ms, a.smooth, a.ref_ch, a.mic_ch, a.clock_ppm)


def cmd_export(a):
    cal = load_cal(a.cal) if a.cal else None
    for name in a.names:
        rec, sw, meta = store.load_recording(name)
        out = rew_export.export_rew(
            name, rec, sw, store.folder(name) / "rew", mic_ch=meta.get("mic_ch", 0), cal=cal,
            cal_name=Path(a.cal).name if a.cal else "", gate_ms=a.gate_ms, ppo=a.ppo, pre_ms=a.pre_ms,
            ir_ms=a.ir_ms, offset_db=a.offset_db, normalize=a.normalize, note=meta.get("note", ""),
            clock_ppm=_clock_arg(a.clock_ppm, meta))
        print(f"{name}: {out['fr']}  ({out['points']} points)")
        print(f"{' ' * len(name)}  {out['ir']}  "
              f"({out['ir_samples']} samples, main impulse at sample {out['peak_index']}"
              f"{', normalized' if out['normalized'] else ''})")
    print("In REW: File > Import > Import frequency response / Import impulse response.")


def cmd_peq(a):
    cal = load_cal(a.cal) if a.cal else None
    r = peq_workflow.fit_measurements(
        a.names, cal=cal, gate_ms=a.gate_ms, clock_for=lambda meta: _clock_arg(a.clock_ppm, meta), bass_db=a.bass_db, corner=a.corner,
        tilt=a.tilt, hp=a.hp, lp=a.lp, rolloff_db=a.rolloff_db, bands=a.bands, fmin=a.fmin, fmax=a.fmax,
        max_boost=a.max_boost, max_cut=a.max_cut, max_q=a.max_q, smooth=a.smooth)
    info, allf, pre = r["info"], r["filters"], r["preamp"]
    out = store.folder(a.names[0]) / "peq"
    out.mkdir(parents=True, exist_ok=True)
    (out / "peq_filters.txt").write_text(peqmod.apo_text(allf, pre) + "\n", encoding="utf-8")
    drew = _plot("plot_peq", info, allf, 48000.0, out / "peq.png", f"PEQ fit: {', '.join(a.names)}  (target: Harman-style)")
    print(f"Fitted {len(r['peaking'])} peaking filters{' + high-pass' if r['hp'] else ''}{' + low-pass' if r['lp'] else ''} "
          f"from {len(a.names)} measurement(s); error {info['rms_before']:.1f} -> {info['rms_after']:.1f} dB rms "
          f"between {r['f_lo']:g} Hz and {r['f_hi']:g} Hz")
    print(f"{'#':>2} {'type':<4} {'freq':>7} {'gain':>8} {'Q':>6}")
    for i, p in enumerate(allf, 1):
        gain = "   -" if p.kind in ("HP", "LP") else f"{p.gain_db:+.1f}"
        print(f"{i:>2} {p.kind:<4} {p.fc:>6g}  {gain:>7}  {p.q:>6g}")
    print(f"Largest boost in the EQ curve: {info['max_boost_db']:+.1f} dB. Suggested preamp: {pre:+.1f} dB (if the app has one).")
    print(f"Wrote {out / 'peq_filters.txt'}")
    if drew:
        print(f"      {out / 'peq.png'}")


def cmd_clock(a):
    """Measure the player-vs-recorder clock offset from a steady tone, or set / show the stored value."""
    if a.set is not None:
        store.set_clock_ppm(a.set, player=a.player, source="set by hand")
        print(f"Stored clock offset {a.set:+.2f} ppm")
        return
    if not a.play_dlna:
        print(f"Stored clock offset: {store.get_clock_ppm():+.2f} ppm  (measure it with: clock --play-dlna wiim)")
        return
    fs = a.fs
    r = upnp.find_renderer(a.play_dlna)
    in_dev = capture.find_device(a.in_device, "input")
    d = store.ROOT / "_clock"
    d.mkdir(parents=True, exist_ok=True)
    wav = d / "tone.wav"
    write_pcm_wav(wav, fs, np.concatenate([np.zeros(int(1.5 * fs)), clock.make_tone(fs, a.tone, a.seconds, a.level),
                                           np.zeros(int(1.0 * fs))]), 24, 2)
    results = []
    for k in range(a.repeat):
        srv = upnp.FileServer(wav, upnp.local_ip_for(r.ip))
        print(f"Run {k + 1}/{a.repeat}: playing a {a.tone:g} Hz tone on {r.name} at {a.level:g} dBFS for {a.seconds:g} s")
        try:
            rec = capture.record(in_dev, a.seconds + 14, fs, 2, lambda: upnp.play(r, srv.url), 1.0)
        finally:
            upnp.stop(r)
            srv.close()
        x = clock.find_tone(rec[:, 0], fs, a.tone)
        ppm, spread = clock.tone_ppm(x, fs, a.tone)
        peak = 20 * np.log10(max(np.max(np.abs(rec[:, 0])), 1e-9))
        print(f"   recorded peak {peak:.1f} dBFS, {len(x) / fs:.1f} s of steady tone: offset {ppm:+.2f} ppm "
              f"(first vs second half differ by {spread:.2f} ppm)")
        results.append(ppm)
    med = float(np.median(results))
    print(f"Clock offset (median of {len(results)}): {med:+.2f} ppm"
          + (f", runs spread {max(results) - min(results):.2f} ppm" if len(results) > 1 else ""))
    if a.save:
        store.set_clock_ppm(med, player=r.name, source=f"tone {a.tone:g} Hz via {r.name}", runs=[round(v, 2) for v in results])
        print("Stored; analyze, peq and export now correct for it (override with --clock-ppm).")
    else:
        print("Not stored. Repeat with --save to use it.")


def cmd_ui(a):
    try:
        from .ui import server
    except ImportError as e:
        raise SystemExit(f"The web interface needs extra packages ({e.name}). Install them with: pip install fastapi uvicorn")
    server.serve(host=a.host, port=a.port, demo=a.demo, open_browser=not a.no_browser)


def cmd_list(a):
    for n in store.list_measurements():
        print(n)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="octetmeasure", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"octet-measure {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    from .autotune.cli import register
    register(sub)

    p = sub.add_parser("devices", help="list audio devices")
    p.add_argument("--all", action="store_true", help="include non-WASAPI devices")
    p.set_defaults(fn=cmd_devices)

    p = sub.add_parser("check", help="show input levels, for setting gain")
    p.add_argument("--in-device", default=DEFAULT_IN)
    p.add_argument("--fs", type=int, default=48000)
    p.add_argument("--seconds", type=float, default=3.0)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("sweep-file", help="write the sweep WAV to play from the Pi")
    _sweep_args(p)
    p.add_argument("--out", default="sweep_48k.wav")
    p.add_argument("--bits", type=int, default=24, choices=[16, 24, 32])
    p.add_argument("--channel", default="both", choices=["left", "right", "both"])
    p.add_argument("--invert", default="none", choices=["none", "left", "right", "both"])
    p.add_argument("--lead", type=float, default=2.0)
    p.add_argument("--tail", type=float, default=3.0)
    p.set_defaults(fn=cmd_sweep_file)

    p = sub.add_parser("record", help="record a measurement and analyse it")
    p.add_argument("name")
    _sweep_args(p)
    p.add_argument("--in-device", default=DEFAULT_IN)
    p.add_argument("--out-device", default=DEFAULT_OUT)
    p.add_argument("--play-local", action="store_true", help="play the sweep from this PC instead of the Pi")
    p.add_argument("--play-dlna", metavar="NAME_OR_IP",
                   help="play the sweep on a DLNA renderer (e.g. 'wiim'), served from this PC")
    p.add_argument("--bits", type=int, default=24, choices=[16, 24, 32], help="WAV depth for --play-dlna")
    p.add_argument("--channel", default="both", choices=["left", "right", "both"],
                   help="which channel carries the sweep (--play-dlna)")
    p.add_argument("--invert", default="none", choices=["none", "left", "right", "both"],
                   help="flip the polarity of a channel, to test exciter phasing (--play-dlna)")
    p.add_argument("--trigger", help="shell command that starts playback, run once recording starts")
    p.add_argument("--trigger-delay", type=float, default=1.0)
    p.add_argument("--seconds", type=float, help="recording length (default: sweep + 10 s)")
    p.add_argument("--ref", action="store_true", help="input channel 2 carries the electrical timing reference")
    p.add_argument("--note", default="")
    p.add_argument("--no-analyze", action="store_true")
    p.add_argument("--cal")
    p.add_argument("--gate-ms", type=float)
    p.add_argument("--smooth", type=float, default=12, help="1/N octave smoothing, 0 = none")
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser("analyze", help="re-analyse a saved measurement")
    p.add_argument("name")
    p.add_argument("--clock-ppm", default="stored", help="player clock offset: 'stored', 'off' or a number of ppm")
    p.add_argument("--cal", help="microphone calibration file")
    p.add_argument("--gate-ms", type=float, help="window after the main impulse; excludes room reflections")
    p.add_argument("--smooth", type=float, default=12, help="1/N octave smoothing, 0 = none")
    p.add_argument("--mic-ch", type=int)
    p.add_argument("--ref-ch", type=int, help="reference channel, -1 for none")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("export", help="write REW-importable files (frequency response .txt and impulse response .wav)")
    p.add_argument("names", nargs="+")
    p.add_argument("--clock-ppm", default="stored", help="player clock offset: 'stored', 'off' or a number of ppm")
    p.add_argument("--cal", help="microphone calibration file to apply")
    p.add_argument("--gate-ms", type=float, help="window after the main impulse (default 500 ms)")
    p.add_argument("--ppo", type=int, default=96, help="frequency points per octave")
    p.add_argument("--pre-ms", type=float, default=5.0, help="impulse response lead-in before the main impulse")
    p.add_argument("--ir-ms", type=float, default=1000.0, help="impulse response length after the lead-in")
    p.add_argument("--offset-db", type=float, default=0.0, help="add to all levels (e.g. to land in REW's SPL range)")
    p.add_argument("--normalize", action="store_true", help="scale the impulse response WAV to a 0.9 peak")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("peq", help="fit a parametric EQ (up to 10 filters) toward a Harman-style target")
    p.add_argument("names", nargs="+", help="one or more measurements (several = power average over mic positions)")
    p.add_argument("--clock-ppm", default="stored", help="player clock offset: 'stored', 'off' or a number of ppm")
    p.add_argument("--cal")
    p.add_argument("--gate-ms", type=float, help="window after the main impulse (default 500 ms, in-room)")
    p.add_argument("--hp", default="auto", help="protective high-pass: auto (where output is --rolloff-db down), off, or Hz")
    p.add_argument("--lp", default="auto", help="protective low-pass: auto (only if the output really rolls off), off, or Hz")
    p.add_argument("--rolloff-db", type=float, default=10.0, help="how far below mid-band counts as 'cannot reproduce'")
    p.add_argument("--bands", type=int, default=8, help="peaking filters (the high/low-pass use two more of the 10)")
    p.add_argument("--fmin", type=float, default=100.0)
    p.add_argument("--fmax", type=float, default=16000.0)
    p.add_argument("--max-boost", type=float, default=3.0, help="largest single-filter boost, dB")
    p.add_argument("--max-cut", type=float, default=6.0, help="largest single-filter cut, dB")
    p.add_argument("--max-q", type=float, default=3.0)
    p.add_argument("--smooth", type=float, default=3.0, help="1/N octave smoothing of the response being fitted")
    p.add_argument("--bass-db", type=float, default=4.0, help="target bass rise below the corner")
    p.add_argument("--corner", type=float, default=105.0, help="target bass corner, Hz")
    p.add_argument("--tilt", type=float, default=-0.4, help="target treble tilt above 1 kHz, dB per octave")
    p.set_defaults(fn=cmd_peq)

    p = sub.add_parser("clock", help="measure the player-vs-recorder clock offset with a tone (needed for DLNA sweeps)")
    p.add_argument("--play-dlna", metavar="NAME_OR_IP", help="renderer to play the tone on, e.g. wiim")
    p.add_argument("--set", type=float, metavar="PPM", help="store an offset by hand")
    p.add_argument("--player", default="", help="with --set: the player the offset belongs to, e.g. WiiM")
    p.add_argument("--save", action="store_true", help="store the measured offset")
    p.add_argument("--repeat", type=int, default=2, help="number of tone runs")
    p.add_argument("--tone", type=float, default=1000.0)
    p.add_argument("--seconds", type=float, default=14.0)
    p.add_argument("--level", type=float, default=-24.0, help="tone level, dBFS")
    p.add_argument("--fs", type=int, default=44100)
    p.add_argument("--in-device", default=DEFAULT_IN)
    p.set_defaults(fn=cmd_clock)

    p = sub.add_parser("ui", help="open the app in your browser (recordings, calibration, new recordings, EQ)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach the app from other devices on your network")
    p.add_argument("--demo", action="store_true", help="simulated microphone and speakers, for trying the app without hardware")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("renderers", help="find UPnP/DLNA players on the network (WiiM etc.)")
    p.add_argument("--timeout", type=float, default=3.0)
    p.set_defaults(fn=cmd_renderers)

    p = sub.add_parser("list", help="list saved measurements")
    p.set_defaults(fn=cmd_list)

    a = ap.parse_args(argv)
    if a.cmd is None:                                  # no command: open the app
        a = ap.parse_args(["ui"])
    a.fn(a)
