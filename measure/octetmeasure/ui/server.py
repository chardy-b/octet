"""The local web server behind the browser app."""
import asyncio
import io as iolib
import json
import platform
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__, calibration, levels, paths, rew_export, service, store
from ..audio import DemoIO, RealIO
from ..session import RecordConfig
from .jobs import Bus, Busy, JobManager

STATIC = Path(__file__).parent / "static"
SETTING_KEYS = {"in_device", "out_device", "renderer", "player", "mic_ch", "channels", "fs", "duration", "f1", "f2",
                "level", "channel", "invert", "trigger", "theme", "ref", "eq_options", "an_smoothing", "an_window", "an_phase"}


def create_app(demo: bool = False, io=None) -> FastAPI:
    io = io or (DemoIO(mic_gain_db=-30.0) if demo else RealIO())
    bus = Bus()
    jobs = JobManager(io, bus)
    from ..autotune.runner import Runner
    auto = Runner(store.ROOT.parent / 'autotune', demo=io.name == 'demo', emit=bus.publish)
    jobs.external_busy = lambda: auto.running

    @asynccontextmanager
    async def lifespan(app):
        bus.loop = asyncio.get_running_loop()
        yield
        jobs.cancel(wait=True)
        if auto.running:
            auto.mute()
        if auto.thread:
            await asyncio.to_thread(auto.thread.join, 10)

    app = FastAPI(title="Octet Measure", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)
    app.state.io, app.state.jobs, app.state.bus = io, jobs, bus
    app.state.autotune = auto

    @app.get('/api/auto')
    def auto_status():
        return auto.status()

    @app.get('/api/auto/profile')
    def auto_profile():
        return dict(yaml=auto.profile())

    @app.put('/api/auto/profile')
    def auto_save_profile(body: dict = Body(...)):
        if auto.running:
            raise Busy('Stop Auto Tune before editing the profile.')
        return auto.set_profile(str(body.get('yaml', '')))

    @app.post('/api/auto/{action}')
    def auto_action(action: str):
        if action == 'mute':
            jobs.cancel()
            return auto.mute()
        with jobs.lock:
            if jobs.thread is not None and jobs.thread.is_alive():
                raise Busy('Stop the current recording or meter before starting Auto Tune.')
            auto.start(action)
        return auto.status()

    @app.get('/api/auto/artifacts/{name}')
    def auto_artifact(name: str):
        if name not in ('report.json', 'camilladsp.yml') or not auto.state.get('id'):
            raise HTTPException(404, 'No such artifact')
        path = auto.directory / name
        if not path.is_file():
            raise HTTPException(404, 'This artifact is not ready yet')
        return FileResponse(path, filename=name)

    @app.get('/api/auto/results')
    def auto_results():
        import numpy as np
        result = {}
        if auto.state.get('id'):
            for side in ('L', 'R'):
                path = auto.directory / f'final-{side}.npz'
                if path.is_file():
                    with np.load(path) as data:
                        result[side] = {k: data[k].tolist() for k in ('freq','before','after')}
        return result

    @app.exception_handler(ValueError)
    async def _bad_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def _missing(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Busy)
    async def _busy(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ------------------------------------------------------------ general
    @app.get("/api/info")
    def info():
        ppm = store.get_clock_ppm()
        try:
            clock_info = json.loads(store.CLOCK_FILE.read_text())
        except (OSError, ValueError):
            clock_info = None
        zones = [dict(id=z[0], lo=z[1], hi=z[2], label=z[3], tone=z[4]) for z in levels.ZONES]
        return dict(version=__version__, demo=io.name == "demo", platform=platform.system(), python=platform.python_version(),
                    zones=zones, target=list(levels.TARGET), target_centre=levels.TARGET_CENTRE,
                    data_dir=str(store.ROOT), settings_file=str(paths.config_path()), clock=clock_info,
                    clock_ppm=ppm, active_cal=calibration.active_name(), settings=_settings())

    def _settings() -> dict:
        return {k: v for k, v in paths.load_config().items() if k in SETTING_KEYS}

    @app.post("/api/settings")
    def save_settings(body: dict = Body(...)):
        paths.save_config(**{k: v for k, v in body.items() if k in SETTING_KEYS})
        return _settings()

    @app.get("/api/devices")
    def devices():
        try:
            return io.devices()
        except RuntimeError as e:                     # no audio library: say so plainly instead of failing
            raise HTTPException(503, str(e))

    @app.get("/api/renderers")
    def renderers():
        return io.renderers()

    # ------------------------------------------------------------ calibration
    @app.get("/api/calibrations")
    def cal_list():
        return dict(files=calibration.listing(), active=calibration.active_name())

    @app.get("/api/calibrations/{name}")
    def cal_info(name: str):
        if name not in calibration.names():
            raise FileNotFoundError(f"No calibration file named {name!r}")
        return calibration.info(name)

    @app.post("/api/calibrations")
    def cal_import(body: dict = Body(...)):
        name = calibration.save(str(body.get("name") or "calibration.txt"), str(body.get("text") or ""))
        if body.get("activate", True):
            calibration.set_active(name)
        return calibration.info(name, max_points=2) | dict(active=calibration.active_name() == name)

    @app.post("/api/calibrations/active")
    def cal_active(body: dict = Body(...)):
        calibration.set_active(body.get("name") or None)
        return dict(active=calibration.active_name())

    @app.delete("/api/calibrations/{name}")
    def cal_delete(name: str):
        calibration.delete(name)
        return dict(ok=True)

    # ------------------------------------------------------------ measurements
    @app.get("/api/measurements")
    def measurements():
        return service.list_measurements()

    @app.get("/api/measurements/{name}/analysis")
    def analysis_for(name: str, smoothing: float = 12.0, window_ms: float | None = None, cal: str = "active",
                     clock: str = "stored"):
        return service.analyze(name, service.AnalysisOptions(smoothing, window_ms, cal, clock))

    @app.get("/api/measurements/{name}/summary")
    def summary_for(name: str):
        service.validate_existing(name)
        return service.summary(name)

    @app.patch("/api/measurements/{name}")
    def edit(name: str, body: dict = Body(...)):
        new = name
        if body.get("note") is not None:
            service.set_note(name, str(body["note"]))
        if body.get("name") and body["name"] != name:
            new = service.rename(name, str(body["name"]))
        return dict(name=new)

    @app.delete("/api/measurements/{name}")
    def remove(name: str):
        service.delete(name)
        return dict(ok=True)

    @app.get("/api/measurements/{name}/rew.zip")
    def rew_zip(name: str, cal: str = "active", clock: str = "stored", window_ms: float | None = None):
        service.validate_existing(name)
        rec, sw, meta = store.load_recording(name)
        c, cname = service.resolve_cal(cal)
        with tempfile.TemporaryDirectory() as tmp:
            out = rew_export.export_rew(name, rec, sw, Path(tmp), mic_ch=meta.get("mic_ch", 0), cal=c,
                                        cal_name=cname or "", gate_ms=window_ms, note=meta.get("note", ""),
                                        clock_ppm=service.resolve_clock(clock, meta))
            buf = iolib.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(out["fr"], out["fr"].name)
                z.write(out["ir"], out["ir"].name)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}_rew.zip"'})

    @app.get("/api/measurements/{name}/recording.wav")
    def raw_wav(name: str):
        service.validate_existing(name)
        return FileResponse(store.folder(name) / "recording.wav", media_type="audio/wav", filename=f"{name}.wav")

    # ------------------------------------------------------------ EQ
    @app.post("/api/peq")
    def peq(body: dict = Body(...)):
        return service.fit_peq(list(body.get("names") or []), service.PeqOptions.from_dict(body.get("options") or {}))

    @app.post("/api/peq/target")
    def peq_target(body: dict = Body(default={})):
        return service.target_curve(service.PeqOptions.from_dict(body.get("options") or {}))

    # ------------------------------------------------------------ clock
    @app.get("/api/clock")
    def clock_get():
        try:
            return json.loads(store.CLOCK_FILE.read_text())
        except (OSError, ValueError):
            return None

    @app.post("/api/clock")
    def clock_set(body: dict = Body(...)):
        store.set_clock_ppm(float(body["ppm"]), player=str(body.get("player") or ""), source="set by hand")
        return clock_get()

    @app.delete("/api/clock")
    def clock_clear():
        store.CLOCK_FILE.unlink(missing_ok=True)
        return dict(ok=True)

    # ------------------------------------------------------------ jobs
    @app.get("/api/job")
    def job_status():
        return jobs.status()

    @app.post("/api/job/{kind}/start")
    def job_start(kind: str, body: dict = Body(default={})):
        if kind not in ("monitor", "record", "noise", "clock"):
            raise HTTPException(404, "Unknown job")
        cfg_d = dict(body.get("config") or body)
        if kind == "record" and cfg_d.get("kind") not in ("sweep", "level_test"):
            cfg_d["kind"] = "sweep"
        cfg = RecordConfig.from_dict(cfg_d)
        if kind == "clock":
            cfg.repeat = max(1, min(cfg.repeat, 5))
        jobs.start(kind, cfg, **({"save": bool(body.get("save", True))} if kind == "clock" else {}))
        return jobs.status()

    @app.post("/api/job/cancel")
    def job_cancel():
        return dict(cancelled=jobs.cancel())

    @app.get("/api/events")
    async def events(request: Request):
        q = bus.subscribe()

        async def gen():
            yield f"data: {json.dumps(dict(type='status', **jobs.status()))}\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        e = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {json.dumps(e)}\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/sweep.wav")
    def sweep_wav(request: Request):
        data = service.sweep_wav_bytes(dict(request.query_params))
        return Response(data, media_type="audio/wav", headers={"Content-Disposition": 'attachment; filename="sweep.wav"'})

    @app.post("/api/demo/gain")
    def demo_gain(body: dict = Body(...)):
        if io.name != "demo":
            raise HTTPException(404, "Only available in demo mode")
        io.mic_gain_db = float(max(-60.0, min(20.0, float(body["db"]))))
        return dict(db=io.mic_gain_db)

    # ------------------------------------------------------------ the app itself
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def serve(host: str = "127.0.0.1", port: int = 8765, demo: bool = False, open_browser: bool = True) -> None:
    import threading
    import webbrowser

    import uvicorn

    app = create_app(demo=demo)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"Octet Measure {__version__} {'(demo mode, simulated hardware) ' if demo else ''}is running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
