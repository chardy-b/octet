// Record: choose a microphone and a player, set the level against the target range, then record.
import { LevelTimeline } from "../charts.js";
import { LevelMeter, adviceBox, adviceFor } from "../meter.js";
import { MINUS, api, app, bus, clear, confirmDialog, fmt, h, qs, setSetting, toast } from "../util.js";

const DEFAULTS = { player: "dlna", fs: 44100, duration: 10, f1: 60, f2: 20000, level: -18, channel: "both", invert: "none", mic_ch: 0, trigger: "" };

export async function mount(root) {
  const S = { ...DEFAULTS, ...app.settings };          // the form's state; saved as you go
  const offs = [];
  let measurements = [];
  let devError = "";
  let devices = app.devices || (app.devices = await api("/api/devices").catch((e) => { devError = e.message; return []; }));
  const info = app.info, zones = info.zones, centre = info.target_centre;
  const inputs = devices.filter((d) => d.inputs > 0), outputs = devices.filter((d) => d.outputs > 0);
  try { measurements = await api("/api/measurements"); } catch { /* empty */ }

  const save = (k, v) => { S[k] = v; setSetting(k, v); };
  const nextName = () => { let i = measurements.length + 1; while (measurements.some((m) => m.name === `Measurement ${i}`)) i++; return `Measurement ${i}`; };
  S.name = nextName();
  const inDev = () => inputs.find((d) => d.name === S.in_device) || inputs.find((d) => d.default_input) || inputs[0];
  if (inDev()) S.in_device = inDev().name;
  const nCh = () => Math.max(1, Math.min(inDev()?.inputs || 2, 8));

  // ------------------------------------------------------------ pieces
  const meterHost = h("div");
  const meter = LevelMeter({ host: meterHost, zones, centre });
  const timelineHost = h("div", { style: { marginTop: "14px" } });
  const timeline = LevelTimeline({ host: timelineHost, zones, windowS: 30 });
  const adviceHost = h("div"), noiseHost = h("div", { class: "small muted", style: { marginTop: "8px" } });
  const resultHost = h("div");
  const progressBar = h("i"), statusLine = h("div", { class: "small muted" }), startBtn = h("button", { class: "btn primary big", type: "button" });
  const stopBtn = h("button", { class: "btn danger big", type: "button", text: "Stop", hidden: true, onclick: () => api("/api/job/cancel", { method: "POST" }) });
  const clockHost = h("div", { class: "small", style: { marginTop: "10px" } });
  const playerHost = h("div", { style: { marginTop: "12px" } });
  const monitorBtn = h("button", { class: "btn", type: "button" }), noiseBtn = h("button", { class: "btn", type: "button", text: "Check room noise" }), testBtn = h("button", { class: "btn", type: "button", text: "Test sweep" });
  let noiseWindow = [];

  const cfg = (extra = {}) => ({
    name: S.name, player: S.player, renderer: S.renderer || "", in_device: S.in_device || "", out_device: S.out_device || "", fs: +S.fs,
    mic_ch: +S.mic_ch, channels: nCh(), duration: +S.duration, f1: +S.f1, f2: +S.f2, level: +S.level, channel: S.channel, invert: S.invert,
    trigger: S.trigger || null, ref: !!S.ref && nCh() > 1, note: S.note || "", ...extra,
    ...(info.demo ? { player: "demo" } : {}),
  });

  // ------------------------------------------------------------ form controls
  const select = (key, options, onchange) => {
    const el = h("select", { id: "f-" + key, onchange: (e) => { save(key, e.target.value); onchange?.(); } },
      options.map(([v, label]) => h("option", { value: v, text: label, selected: String(S[key]) === String(v) })));
    return el;
  };
  const numField = (key, label, attrs = {}, hint) => h("div", { class: "field" },
    h("label", { for: "f-" + key, text: label }),
    h("input", { id: "f-" + key, type: "number", value: S[key], ...attrs, oninput: (e) => save(key, e.target.value === "" ? "" : +e.target.value) }),
    hint ? h("span", { class: "hint", text: hint }) : null);

  const deviceField = h("div", { class: "field" }, h("label", { for: "f-in_device", text: "Microphone input" }),
    h("select", { id: "f-in_device", onchange: (e) => { save("in_device", e.target.value); renderChannels(); } },
      inputs.length ? inputs.map((d) => h("option", { value: d.name, text: d.name + (d.default_input ? " (default)" : ""), selected: d.name === S.in_device })) : [h("option", { text: "No input devices found" })]),
    h("span", { class: "hint", text: "The sound card or interface your measurement mic is plugged into." }));
  const chanHost = h("div"), refHost = h("div");
  function renderChannels() {
    const n = nCh();
    if (+S.mic_ch >= n) save("mic_ch", 0);
    clear(chanHost).append(h("div", { class: "field" }, h("label", { for: "f-mic_ch", text: "Microphone is on input" }),
      h("select", { id: "f-mic_ch", onchange: (e) => save("mic_ch", +e.target.value) },
        Array.from({ length: n }, (_, i) => h("option", { value: i, text: `Input ${i + 1}`, selected: +S.mic_ch === i }))),
      h("span", { class: "hint", text: n > 1 ? "Some interfaces pair inputs 1 and 2 in one device." : "This device has one input." })));
  }

  const clockLine = () => {
    clear(clockHost);
    if (S.player !== "dlna" && !info.demo) return;
    const c = info.clock;
    if (c && (!c.player || (S.renderer || "").toLowerCase().includes(String(c.player).toLowerCase()) || String(c.player).toLowerCase().includes((S.renderer || "x").toLowerCase()))) {
      clockHost.append(h("span", { class: "chip good", text: "Clock offset measured" }), " ", h("span", { class: "muted", text: `${fmt(c.ppm, 1)} ppm for ${c.player || "this player"}. It is corrected in the analysis.` }));
    } else {
      clockHost.append(h("span", { class: "chip warn", text: "Clock offset not measured" }), " ",
        h("span", { class: "muted", text: "Streaming players run on their own clock, which slightly blurs the top of a sweep. " }),
        h("a", { href: "#/settings", text: "Measure it once in Settings" }), h("span", { class: "muted", text: " (about a minute)." }));
    }
  };

  async function scan(btn) {
    btn.disabled = true; btn.textContent = "Searching…";
    try { app.renderers = await api("/api/renderers"); } catch (e) { toast(e.message, "bad"); }
    btn.disabled = false; btn.textContent = "Search again";
    renderPlayer();
  }

  function renderPlayer() {
    const pick = (v, title, desc) => h("label", { class: "choice" },
      h("input", { type: "radio", name: "player", value: v, checked: S.player === v, onchange: () => { save("player", v); renderPlayer(); clockLine(); } }),
      h("div", {}, h("b", { text: title }), h("span", { text: desc })));
    const box = h("div", { class: "choices" },
      pick("dlna", "Network player", "A WiiM, or any streamer that supports DLNA or UPnP."),
      pick("local", "This computer", "Play the sweep through a sound card connected to this computer."),
      pick("manual", "Something else", "A Raspberry Pi, a phone, a CD player: you start the sweep yourself."));
    clear(playerHost).append(h("div", { class: "lab small", style: { fontWeight: 600, color: "var(--ink2)", marginBottom: "6px" }, text: "How will the sweep be played?" }), box);
    const detail = h("div", { style: { marginTop: "12px" } });
    if (S.player === "dlna" || info.demo) {
      const list = app.renderers;
      const scanBtn = h("button", { class: "btn", type: "button", text: list ? "Search again" : "Search the network" });
      scanBtn.onclick = () => scan(scanBtn);
      if (list && list.length && !list.some((r) => r.name === S.renderer)) save("renderer", list[0].name);
      detail.append(h("div", { class: "row" },
        h("div", { class: "field", style: { flex: "1 1 260px" } }, h("label", { for: "f-renderer", text: "Player" }),
          h("select", { id: "f-renderer", onchange: (e) => { save("renderer", e.target.value); clockLine(); } },
            list && list.length ? list.map((r) => h("option", { value: r.name, text: `${r.name} (${r.ip})`, selected: r.name === S.renderer })) : [h("option", { value: "", text: list ? "None found" : "Search to see players" })])),
        h("div", { style: { alignSelf: "flex-end" } }, scanBtn)));
      if (list && !list.length) detail.append(h("p", { class: "hint", style: { marginTop: "8px" }, text: "No players found. Check that it is switched on and on the same Wi-Fi or network as this computer, and that the router does not isolate devices. Guest networks often do." }));
    } else if (S.player === "local") {
      detail.append(h("div", { class: "field" }, h("label", { for: "f-out_device", text: "Output" }),
        h("select", { id: "f-out_device", onchange: (e) => save("out_device", e.target.value) },
          outputs.map((d) => h("option", { value: d.name, text: d.name + (d.default_output ? " (default)" : ""), selected: d.name === S.out_device || (!S.out_device && d.default_output) })))));
    } else {
      detail.append(h("div", { class: "grid2" },
        h("div", { class: "field" }, h("label", { for: "f-trigger", text: "Command to start playback (optional)" }),
          h("input", { id: "f-trigger", type: "text", value: S.trigger || "", placeholder: "for example: ssh pi@raspberrypi mpc play", oninput: (e) => save("trigger", e.target.value) }),
          h("span", { class: "hint", text: "Runs a moment after recording starts. Leave empty to start playback yourself." })),
        h("div", { class: "field" }, h("span", { class: "lab", text: "Sweep file" }),
          h("a", { class: "btn", href: "/api/sweep.wav?" + qs({ fs: S.fs, duration: S.duration, f1: S.f1, f2: S.f2, level: S.level, channel: S.channel, invert: S.invert }), text: "Download the sweep as a WAV" }),
          h("span", { class: "hint", text: "Play this file on your own player. Recording lasts 15 seconds longer than the sweep." }))));
    }
    playerHost.append(detail);
    if ((S.player === "dlna" || info.demo) && !app.renderers) scan(detail.querySelector("button"));
  }

  // ------------------------------------------------------------ level section
  const setIdle = () => { noiseWindow = []; };
  function noiseNote() {
    if (!noiseWindow.length) return;
    const s = [...noiseWindow].sort((a, b) => a - b), q = s[Math.floor(s.length * 0.2)];
    if (q > -45) { noiseHost.textContent = "Room noise: waiting for a quiet moment…"; return; }
    const r = q < -65 ? ["good", "quiet room"] : q < -55 ? ["ok", "some background noise"] : ["warn", "noisy"];
    clear(noiseHost).append("Room noise ≈ ", h("b", { class: "num", text: `${fmt(q, 0)} dBFS` }), " ", h("span", { class: "chip " + r[0], text: r[1] }));
  }

  // The live meter does not block anything: starting a test or a recording simply replaces it.
  function setRunning(running) {
    startBtn.hidden = running; stopBtn.hidden = !running;
    [noiseBtn, testBtn].forEach((b) => (b.disabled = running));
  }
  monitorBtn.onclick = () => (app.job.running && app.job.kind === "monitor"
    ? api("/api/job/cancel", { method: "POST" })
    : api("/api/job/monitor/start", { method: "POST", body: cfg() }).then(() => { timeline.reset(null); meter.reset(); setIdle(); clear(adviceHost); }).catch((e) => toast(e.message, "bad")));
  noiseBtn.onclick = () => api("/api/job/noise/start", { method: "POST", body: cfg() }).then(() => { timeline.reset(4); clear(adviceHost); clear(resultHost); }).catch((e) => toast(e.message, "bad"));
  testBtn.onclick = () => api("/api/job/record/start", { method: "POST", body: cfg({ kind: "level_test", save: false }) }).then(() => { timeline.reset(null); clear(adviceHost); clear(resultHost); }).catch((e) => toast(e.message, "bad"));

  function syncJob() {
    const j = app.job, listening = j.running && j.kind === "monitor", busy = j.running && !listening;
    setRunning(busy);
    monitorBtn.disabled = busy;
    monitorBtn.textContent = listening ? "Stop listening" : "Listen";
    startBtn.textContent = "Start recording";
    statusLine.textContent = busy ? (j.message || "Working…") : "";
  }

  // ------------------------------------------------------------ form: the sweep
  const estimate = h("span", { class: "hint" });
  const estimateText = () => {
    const extra = { dlna: 16, demo: 9, local: 6, manual: 15 }[info.demo ? "demo" : S.player];
    estimate.textContent = `About ${Math.round(+S.duration + extra)} seconds in total, including the player's start-up.`;
  };
  const levelOut = h("span", { class: "num small", text: `${MINUS}${Math.abs(S.level)} dBFS` });
  const sweepCard = h("div", { class: "card" },
    h("header", {}, h("h2", {}, h("span", { class: "step", text: "3" }), "The sweep")),
    h("div", { class: "grid2" },
      h("div", { class: "field" }, h("label", { for: "f-name", text: "Name" }),
        h("input", { id: "f-name", type: "text", value: S.name, maxlength: 80, oninput: (e) => { S.name = e.target.value; } }),
        h("span", { class: "hint", text: "For example: Left speaker, sofa position." })),
      h("div", { class: "field" }, h("span", { class: "lab", text: "What to measure" }),
        (() => {
          const seg = h("div", { class: "seg", role: "group", "aria-label": "Channels" });
          [["both", "Both"], ["left", "Left only"], ["right", "Right only"]].forEach(([v, t]) =>
            seg.append(h("button", { type: "button", "aria-pressed": S.channel === v, text: t, onclick: () => { save("channel", v); seg.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === t))); } })));
          return seg;
        })(),
        h("span", { class: "hint", text: "Left only and right only play the sweep on one channel, to measure each speaker or exciter alone." }))),
    h("div", { class: "grid3", style: { marginTop: "14px" } },
      h("div", { class: "field" }, h("label", { for: "f-level", text: "Sweep level" }),
        h("div", { class: "row" }, h("input", { id: "f-level", type: "range", min: -40, max: -6, step: 1, value: S.level, style: { flex: 1 }, oninput: (e) => { save("level", +e.target.value); levelOut.textContent = `${MINUS}${Math.abs(S.level)} dBFS`; } }),
          levelOut),
        h("span", { class: "hint", text: "How loud the file is. Set the actual loudness with the volume on your amp." })),
      numField("duration", "Length (seconds)", { min: 4, max: 30, step: 1 }, "Longer sweeps give a cleaner result."),
      h("div", { class: "field" }, h("label", { for: "f-fs", text: "Sample rate" }),
        select("fs", [[44100, "44,100 Hz"], [48000, "48,000 Hz"], [96000, "96,000 Hz"]], estimateText),
        h("span", { class: "hint", text: "Use the rate your player runs at. WiiM: 44,100 Hz." }))),
    h("details", { class: "fold", style: { marginTop: "10px" } },
      h("summary", { text: "More options" }),
      h("div", { class: "grid3", style: { marginTop: "10px" } },
        numField("f1", "Lowest frequency (Hz)", { min: 10, max: 2000 }, "Start higher to protect small drivers."),
        numField("f2", "Highest frequency (Hz)", { min: 1000, max: 40000 }),
        h("div", { class: "field" }, h("label", { for: "f-invert", text: "Flip polarity of" }),
          select("invert", [["none", "Nothing"], ["left", "Left channel"], ["right", "Right channel"], ["both", "Both"]]),
          h("span", { class: "hint", text: "Test whether two drivers are wired out of phase." }))),
      h("div", { class: "field", style: { marginTop: "12px" } }, h("label", { for: "f-note", text: "Note" }),
        h("textarea", { id: "f-note", maxlength: 500, placeholder: "Mic position, room, anything you want to remember", oninput: (e) => { S.note = e.target.value; } }))),
    h("div", { style: { marginTop: "10px" } }, estimate));

  // ------------------------------------------------------------ assemble
  const gainOut = h("span", { class: "num small", text: `${MINUS}30 dB` });
  const demoBox = info.demo ? h("div", { class: "banner" },
    h("div", {}, h("b", { text: "Demo mode. " }), "The microphone, speakers and player are simulated. Move the slider to change the simulated mic gain and watch the meter."),
    h("div", { class: "row" }, h("span", { class: "small", text: "Mic gain" }),
      h("input", { type: "range", min: -45, max: 6, step: 1, value: -30, style: { width: "180px" }, "aria-label": "Simulated mic gain", oninput: (e) => { gainOut.textContent = `${fmt(+e.target.value, 0)} dB`; api("/api/demo/gain", { method: "POST", body: { db: +e.target.value } }); } }),
      gainOut)) : null;

  const meterCard = h("div", { class: "card" },
    h("header", {}, h("h2", {}, h("span", { class: "step", text: "2" }), "Set the level"),
      h("div", { class: "row" }, monitorBtn, noiseBtn, testBtn)),
    h("p", { class: "small muted", style: { marginBottom: "10px", maxWidth: "70ch" } },
      "Aim for the recorded sweep to peak between ", h("b", { text: `${MINUS}20 and ${MINUS}6 dBFS` }), ". That is loud enough to measure cleanly with room to spare. ",
      "Press ", h("b", { text: "Test sweep" }), " to play a short sweep and see where it lands, then adjust the gain on your interface or the volume on your amp. ",
      "Press ", h("b", { text: "Listen" }), " to watch the live level any time."),
    meterHost, timelineHost, noiseHost, adviceHost);

  const setupCard = h("div", { class: "card" },
    h("header", {}, h("h2", {}, h("span", { class: "step", text: "1" }), "Microphone and player")),
    h("div", { class: "grid2" }, deviceField, chanHost),
    playerHost, clockHost);

  const bar = h("div", { class: "actionbar" },
    startBtn, stopBtn,
    h("div", { class: "grow" }, h("div", { class: "progress", "aria-hidden": "true" }, progressBar), statusLine));
  startBtn.onclick = async () => {
    const name = (S.name || "").trim();
    if (!name) { toast("Give the measurement a name first.", "bad"); document.getElementById("f-name").focus(); return; }
    if (measurements.some((m) => m.name === name) && !(await overwrite(name))) return;
    try {
      await api("/api/job/record/start", { method: "POST", body: cfg({ kind: "sweep", save: true, name }) });
      timeline.reset(null); meter.reset(); clear(adviceHost); clear(resultHost);
    } catch (e) { toast(e.message, "bad"); }
  };
  async function overwrite(name) {
    return confirmDialog({ title: "Replace this measurement?", body: `There is already a measurement called “${name}”. Recording again will replace it.`, ok: "Replace", danger: true });
  }

  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Record a measurement" }),
      h("p", { text: "Play a test sweep through your speaker, record it with a measurement microphone, and see how the speaker behaves." }))),
    demoBox, h("div", { class: "stack" }, setupCard, meterCard, sweepCard, resultHost), bar);
  renderChannels(); renderPlayer(); clockLine(); estimateText(); syncJob();
  if (!inputs.length) { app.devices = null; root.prepend(h("div", { class: "banner bad" }, devError || "No audio input device was found. Connect your microphone or interface and reload this page.")); }

  // ------------------------------------------------------------ live events
  const micOf = (e) => e.ch[Math.min(e.mic ?? 0, e.ch.length - 1)];
  offs.push(bus.on("level", (e) => {
    const m = micOf(e);
    meter.update(m);
    timeline.push(e.t, m.band_peak, m.band_rms);
    if (e.job === "monitor" || e.job === "noise") { noiseWindow.push(m.band_rms); if (noiseWindow.length > 100) noiseWindow.shift(); noiseNote(); }
    if (e.total) progressBar.style.width = `${Math.min(100, (e.t / e.total) * 100)}%`;
  }));
  offs.push(bus.on("state", (e) => {
    syncJob();
    if (e.state === "playing") timeline.mark(timelineNow(), "sweep starts");
    if (e.state === "recording" && e.job !== "monitor") { progressBar.style.width = "0%"; if (e.job === "record" || e.job === "clock") timeline.reset(null); }
  }));
  let lastT = 0;
  offs.push(bus.on("level", (e) => { lastT = e.t; if (e.total && timelineTotal !== e.total) { timelineTotal = e.total; timeline.reset(e.total); } }));
  let timelineTotal = null;
  const timelineNow = () => lastT;
  offs.push(bus.on("idle", () => { syncJob(); progressBar.style.width = "0%"; timelineTotal = null; }));
  offs.push(bus.on("status", syncJob));
  offs.push(bus.on("done", async (e) => {
    const r = e.result;
    syncJob(); progressBar.style.width = r.kind === "noise" ? "100%" : progressBar.style.width;
    if (r.kind === "noise") {
      clear(adviceHost).append(h("div", { class: "advice " + r.rating.tone }, h("span", { class: "chip " + r.rating.tone, text: r.rating.rating }), h("div", {}, h("b", { text: `${fmt(r.band_rms, 0)} dBFS of background noise above 60 Hz. ` }), r.rating.message)));
    } else if (r.kind === "level_test") {
      meter.update({ band_peak: r.band_peak, band_rms: r.band_rms, peak: r.peak, rms: r.peak });      // leave the sweep's peak on the meter
      clear(adviceHost).append(adviceBox(r.advice, h("div", { class: "small muted", style: { marginTop: "4px" } }, `The test sweep peaked at ${fmt(r.band_peak, 1)} dBFS above 60 Hz.`)));
    } else if (r.kind === "sweep") {
      measurements = await api("/api/measurements").catch(() => measurements);
      showResult(r);
    } else if (r.kind === "clock") {
      toast(`Clock offset ${fmt(r.ppm, 1)} ppm saved for ${r.player}.`, "good");
      app.info = await api("/api/info"); info.clock = app.info.clock; clockLine();
    }
  }));
  function showResult(r) {
    const snr = r.snr_db, tone = snr >= 40 ? "good" : snr >= 25 ? "ok" : "warn";
    clear(resultHost).append(h("div", { class: "card" },
      h("header", {}, h("h2", { text: "Saved: " + r.name }), h("span", { class: "chip " + r.advice.tone, text: r.advice.label })),
      h("div", { class: "statrow" },
        h("span", {}, "Peak ", h("b", { text: `${fmt(r.band_peak, 1)} dBFS` })),
        snr != null ? h("span", {}, "Signal-to-noise ", h("b", { text: `${fmt(snr, 0)} dB` }), " ", h("span", { class: "chip " + tone, text: snr >= 40 ? "excellent" : snr >= 25 ? "good" : "low" })) : null,
        r.clipped ? h("span", { class: "chip bad", text: "Clipped" }) : null,
        r.clock_ppm ? h("span", {}, "Clock correction ", h("b", { text: `${fmt(r.clock_ppm, 1)} ppm` })) : null),
      r.clipped ? h("p", { class: "small", style: { color: "var(--bad)" }, text: "The recording clipped. Lower the mic gain and record again." }) : null,
      snr != null && snr < 25 ? h("p", { class: "small muted", text: "The signal is close to the noise. Raise the mic gain or the volume, or reduce room noise, and record again for a cleaner result." }) : null,
      h("div", { class: "row", style: { marginTop: "12px" } },
        h("a", { class: "btn primary", href: "#/recordings/" + encodeURIComponent(r.name), text: "Open the recording" }),
        h("a", { class: "btn", href: "#/eq/" + encodeURIComponent(r.name), text: "Fit an EQ" }))));
    S.name = nextName(); const nf = document.getElementById("f-name"); if (nf) nf.value = S.name;
    toast(`Saved ${r.name}`, "good");
  }
  offs.push(bus.on("error", () => { syncJob(); progressBar.style.width = "0%"; }));
  offs.push(bus.on("player", (e) => { statusLine.textContent = `Player: ${e.label}`; }));

  return () => { offs.forEach((f) => f()); timeline.destroy(); if (app.job.running && app.job.kind === "monitor") api("/api/job/cancel", { method: "POST" }).catch(() => {}); };
}
