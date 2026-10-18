// Recordings: browse everything you have measured, look at it, compare it, and take it to the EQ.
import { BandBars, LineChart } from "../charts.js";
import { MINUS, ago, api, app, clear, confirmDialog, dialog, download, fDate, fmt, h, qs, setSetting, toast } from "../util.js";

const PALETTE = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];
const SMOOTHING = [[0, "None"], [24, "1/24 octave"], [12, "1/12 octave"], [6, "1/6 octave"], [3, "1/3 octave"]];
const WINDOWS = [["", "Automatic (500 ms)"], [8, "8 ms (direct sound only)"], [20, "20 ms"], [50, "50 ms"], [125, "125 ms"], [250, "250 ms"], [500, "500 ms"], [1000, "1 second"]];
const chanLabel = (m) => ({ both: "Both", left: "Left", right: "Right" }[m.channel] || "Both") + (m.invert && m.invert !== "none" ? ` · ${m.invert} flipped` : "");

function snrAlong(a) {
  const f = a.snr_f, s = a.snr;
  if (!f.length) return a.f.map(() => 60);
  return a.f.map((x) => {
    if (x <= f[0]) return s[0];
    if (x >= f[f.length - 1]) return s[s.length - 1];
    let i = 1; while (f[i] < x) i++;
    const t = (Math.log(x) - Math.log(f[i - 1])) / (Math.log(f[i]) - Math.log(f[i - 1]));
    return s[i - 1] + (s[i] - s[i - 1]) * t;
  });
}
const snrTone = (v) => (v == null ? "" : v >= 40 ? "good" : v >= 25 ? "ok" : "warn");

export async function mount(root, params) {
  let list = [], selected = new Set(params.length ? [params[0]] : []), primary = params[0] || null, tab = "response", query = "";
  const opts = { smoothing: app.settings.an_smoothing ?? 12, window_ms: app.settings.an_window ?? "", cal: "active", clock: "stored", phase: !!app.settings.an_phase };
  let cals = { files: [], active: null };
  const cache = new Map(), charts = [];
  const listHost = h("div", { class: "list", role: "list" }), detail = h("div"), search = h("input", { type: "text", placeholder: "Search recordings", "aria-label": "Search recordings", oninput: (e) => { query = e.target.value.toLowerCase(); renderList(); } });
  const disposeCharts = () => { charts.splice(0).forEach((c) => c.destroy?.()); };

  async function load() {
    [list, cals] = await Promise.all([api("/api/measurements"), api("/api/calibrations").catch(() => ({ files: [], active: null }))]);
    if (primary && !list.some((m) => m.name === primary)) { selected.delete(primary); primary = [...selected][0] || null; }
    if (!primary && list.length) { primary = list[0].name; selected = new Set([primary]); }
    renderList(); renderDetail(); fillSummaries();
  }
  async function fillSummaries() {
    for (const m of list.filter((x) => !x.summary)) {
      try { m.summary = await api(`/api/measurements/${encodeURIComponent(m.name)}/summary`); renderList(); } catch { m.summary = { error: true }; }
      if (!root.isConnected) return;
    }
  }

  // ------------------------------------------------------------ list
  function renderList() {
    clear(listHost);
    const rows = list.filter((m) => !query || (m.name + " " + (m.note || "")).toLowerCase().includes(query));
    if (!rows.length) listHost.append(h("p", { class: "muted small", style: { padding: "8px" }, text: query ? "Nothing matches your search." : "" }));
    for (const m of rows) {
      const s = m.summary, tone = s?.snr_db != null ? snrTone(s.snr_db) : "";
      const box = h("input", { type: "checkbox", checked: selected.has(m.name), "aria-label": "Select " + m.name, onclick: (e) => { e.stopPropagation(); toggle(m.name, e.target.checked); } });
      listHost.append(h("div", { class: "item", role: "listitem", tabindex: 0, "aria-current": m.name === primary ? "true" : "false", onclick: () => choose(m.name), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(m.name); } } },
        box,
        h("div", {}, h("div", { class: "t", text: m.name }),
          h("div", { class: "m" }, h("span", { class: "chip", text: chanLabel(m) }), m.player ? h("span", { class: "chip", text: m.player === "local" ? "this computer" : m.player }) : null,
            s && s.snr_db != null ? h("span", { class: "inline" }, h("i", { class: "dot " + tone }), h("span", { class: "num", text: `${fmt(s.snr_db, 0)} dB` })) : (s?.error ? null : h("span", { class: "skeleton", style: { width: "44px" } }))),
          h("div", { class: "m", text: ago(m.created) + (m.note ? " · " + m.note : "") }))));
    }
  }
  function choose(name) { primary = name; if (!selected.has(name)) selected = new Set([name]); history.replaceState(null, "", "#/recordings/" + encodeURIComponent(name)); renderList(); renderDetail(); }
  function toggle(name, on) { on ? selected.add(name) : selected.delete(name); if (!primary || !selected.has(primary)) primary = [...selected][0] || primary; renderList(); renderDetail(); }

  // ------------------------------------------------------------ analysis fetch
  const key = (n) => `${n}|${opts.smoothing}|${opts.window_ms}|${opts.cal}|${opts.clock}`;
  async function analysis(name) {
    const k = key(name);
    if (!cache.has(k)) cache.set(k, api(`/api/measurements/${encodeURIComponent(name)}/analysis?` + qs({ smoothing: opts.smoothing, window_ms: opts.window_ms, cal: opts.cal, clock: opts.clock })));
    try { return await cache.get(k); } catch (e) { cache.delete(k); throw e; }
  }

  // ------------------------------------------------------------ detail
  let renderToken = 0;
  async function renderDetail() {
    disposeCharts();
    clear(detail);
    if (!list.length) {
      detail.append(h("div", { class: "card empty" }, h("h3", { text: "No recordings yet" }), h("p", { text: "Record a sweep through your speaker and it will appear here, with its frequency response, impulse response and signal-to-noise ratio." }),
        h("a", { class: "btn primary", href: "#/record", text: "Make a recording" })));
      return;
    }
    const names = list.filter((m) => selected.has(m.name)).map((m) => m.name);
    if (!names.length) { detail.append(h("div", { class: "card empty" }, h("p", { text: "Select a recording from the list." }))); return; }
    const token = ++renderToken, m = list.find((x) => x.name === primary) || list.find((x) => names.includes(x.name));
    const compare = names.length > 1;

    const actions = h("div", { class: "row" },
      h("a", { class: "btn primary", href: "#/eq/" + names.map(encodeURIComponent).join("/"), text: compare ? `Fit an EQ to ${names.length}` : "Fit an EQ" }),
      h("button", { class: "btn", type: "button", text: "Export for REW", title: "A frequency response and an impulse response file REW can import", onclick: () => exportRew(m.name) }),
      h("button", { class: "btn ghost", type: "button", text: "Rename", onclick: () => rename(m.name) }),
      h("button", { class: "btn ghost danger", type: "button", text: "Delete", onclick: () => remove(m.name) }));
    const note = h("textarea", { "aria-label": "Note", placeholder: "Add a note: mic position, room, anything useful", maxlength: 500, onchange: (e) => api("/api/measurements/" + encodeURIComponent(m.name), { method: "PATCH", body: { note: e.target.value } }).then(() => { m.note = e.target.value; renderList(); }).catch((er) => toast(er.message, "bad")) });
    note.value = m.note || "";

    const tabs = h("div", { class: "tabs", role: "tablist" });
    const tabList = compare ? [["response", "Frequency response"]] : [["response", "Frequency response"], ["impulse", "Impulse response"], ["snr", "Signal-to-noise"], ["details", "Details"]];
    if (compare && tab !== "response") tab = "response";
    tabList.forEach(([id, label]) => tabs.append(h("button", { role: "tab", "aria-selected": String(tab === id), text: label, onclick: () => { tab = id; renderDetail(); } })));
    const body = h("div", {}, h("div", { class: "skeleton", style: { height: "300px" } }));
    detail.append(h("div", { class: "card" },
      h("header", {}, h("div", {}, h("h2", { text: compare ? `Comparing ${names.length} recordings` : m.name }), h("div", { class: "small muted", text: compare ? names.join(", ") : `${fDate(m.created)} · ${chanLabel(m)}` })), actions),
      compare ? null : note, h("div", { style: { marginTop: "12px" } }), tabs, controls(), body));

    try {
      const all = await Promise.all(names.map(analysis));
      if (token !== renderToken) return;
      clear(body);
      const ex = all.find((a) => a.name === m.name) || all[0];
      if (tab === "response") responseTab(body, all, ex, compare);
      else if (tab === "impulse") impulseTab(body, ex);
      else if (tab === "snr") snrTab(body, ex);
      else detailsTab(body, ex, m);
    } catch (e) {
      clear(body).append(h("div", { class: "banner bad" }, "Could not analyse this recording: " + e.message));
    }
  }

  function controls() {
    const sel = (label, id, options, cur, on) => h("div", { class: "field", style: { minWidth: "150px" } }, h("label", { for: id, text: label }),
      h("select", { id, onchange: (e) => on(e.target.value) }, options.map(([v, t]) => h("option", { value: v, text: t, selected: String(cur) === String(v) }))));
    const calOpts = [["active", cals.active ? `Active (${cals.active})` : "Active (none selected)"], ["none", "None"], ...cals.files.map((c) => [c.name, c.name])];
    return h("div", { class: "row", style: { alignItems: "flex-end", margin: "4px 0 12px", gap: "12px" } },
      tab === "response" ? sel("Smoothing", "o-sm", SMOOTHING, opts.smoothing, (v) => { opts.smoothing = +v; setSetting("an_smoothing", +v); renderDetail(); }) : null,
      sel("Analysis window", "o-win", WINDOWS, opts.window_ms, (v) => { opts.window_ms = v; setSetting("an_window", v); renderDetail(); }),
      sel("Microphone calibration", "o-cal", calOpts, opts.cal, (v) => { opts.cal = v; renderDetail(); }),
      sel("Clock correction", "o-clk", [["stored", app.info.clock_ppm ? `Stored (${fmt(app.info.clock_ppm, 1)} ppm)` : "Stored (none measured)"], ["off", "Off"]], opts.clock, (v) => { opts.clock = v; renderDetail(); }),
      tab === "response" ? h("label", { class: "inline small" }, h("input", { type: "checkbox", checked: opts.phase, onchange: (e) => { opts.phase = e.target.checked; setSetting("an_phase", opts.phase); renderDetail(); } }), "Show phase") : null);
  }

  function responseTab(body, all, ex, compare) {
    const hold = h("div"), legend = h("div", { class: "legend" });
    const series = all.map((a, i) => ({ short: a.name, css: PALETTE[i % PALETTE.length], f: a.f, y: a.mag, snr: snrAlong(a), visible: () => vis[i] }));
    const vis = all.map(() => true);
    if (compare) series.forEach((s, i) => legend.append(h("button", { class: "chip", type: "button", "aria-pressed": "true", onclick: (e) => { vis[i] = !vis[i]; e.currentTarget.setAttribute("aria-pressed", vis[i]); c.draw(); } }, h("i", { class: "sw", style: { background: s.css } }), s.short)));
    const lows = all.flatMap((a) => a.mag.filter((v) => v !== null)), lo = Math.min(...lows), hi = Math.max(...lows);
    const y0 = Math.floor((lo - 4) / 10) * 10, y1 = Math.ceil((hi + 4) / 10) * 10;
    const x0 = Math.max(20, Math.min(...all.map((a) => a.f[0])) * 0.95);
    const c = LineChart({ host: hold, series, yDom: [Math.max(y0, y1 - 90), y1], yStep: 10, yTitle: "dB re full scale", x0, x1: 20000, labels: false, tipFmt: (v) => fmt(v) + " dB" });
    charts.push(c);
    body.append(legend, hold, h("div", { class: "statrow" },
      h("span", {}, "Signal-to-noise ", h("b", { text: ex.snr_db == null ? "–" : `${fmt(ex.snr_db, 0)} dB` }), " ", ex.snr_db != null ? h("span", { class: "chip " + snrTone(ex.snr_db), text: ex.snr_db >= 40 ? "excellent" : ex.snr_db >= 25 ? "good" : "low" }) : null),
      h("span", {}, "Peak ", h("b", { text: `${fmt(ex.levels.band_peak, 1)} dBFS` })),
      ex.cal ? h("span", {}, "Calibration ", h("b", { text: ex.cal })) : h("span", { class: "chip warn", text: "No calibration applied" }),
      ex.clock_ppm ? h("span", {}, "Clock ", h("b", { text: `${fmt(ex.clock_ppm, 1)} ppm` })) : null,
      ex.clipped ? h("span", { class: "chip bad", text: "Clipped" }) : null),
      h("p", { class: "hint" }, "Dotted sections are mostly noise (under 10 dB signal-to-noise). Levels are relative to digital full scale, not calibrated sound pressure."));
    if (opts.phase) {
      const ph = h("div", { style: { marginTop: "10px" } });
      charts.push(LineChart({ host: ph, series: all.map((a, i) => ({ short: a.name, css: PALETTE[i % PALETTE.length], f: a.f, y: a.phase, width: 1.5 })), yDom: [-180, 180], yStep: 90, yTitle: "Phase (degrees)", x0, x1: 20000, height: 220, tipFmt: (v) => fmt(v, 0) + "°" }));
      body.append(ph, h("p", { class: "hint", text: "Phase is relative to the arrival of the sound, because there is no timing reference. It is mainly useful for comparing how two drivers behave." }));
    }
  }

  function impulseTab(body, a) {
    const peak = Math.max(...a.ir.map(Math.abs)) || 1, hold = h("div");
    charts.push(LineChart({ host: hold, xLog: false, series: [{ short: "Impulse response", css: "var(--s1)", f: a.ir_t, y: a.ir.map((v) => v / peak), width: 1.5 }], x0: a.ir_t[0], x1: a.ir_t[a.ir_t.length - 1],
      yDom: [-1.1, 1.1], yStep: 0.5, yTitle: "Relative amplitude", xTitle: "Milliseconds after the direct sound", zeroLine: true, tipFmt: (v) => fmt(v, 2), xFmt: (x) => `${fmt(x, 2)} ms` }));
    body.append(hold, h("p", { class: "hint", text: "A sharp spike followed by quick decay means a clean speaker and room. A long, ragged tail points to resonances and reflections. A panel speaker such as a DML shows a spike followed by long ringing; that is normal for it." }));
    if (a.delay_ms != null) body.append(h("p", { class: "small", text: `Delay against the timing reference: ${fmt(a.delay_ms, 3)} ms (${fmt(a.delay_ms * 34.3, 1)} cm of air).` }));
  }

  function snrTab(body, a) {
    const hold = h("div");
    charts.push(BandBars({ host: hold, f: a.snr_f, snr: a.snr }));
    body.append(hold, h("div", { class: "statrow" }, h("span", {}, "Median ", h("b", { text: `${fmt(a.snr_db, 0)} dB` })), h("span", {}, "Weakest band ", h("b", { text: `${fmt(a.snr_worst, 0)} dB` }))),
      h("p", { class: "hint", text: "Signal-to-noise per third-octave band, from how loud the sweep was in each band compared with the room noise recorded when nothing was playing. Above 40 dB is excellent, 25 to 40 is good, under 10 means the band is mostly noise. To improve it: raise the mic gain, turn the speaker up, or quiet the room." }));
  }

  function detailsTab(body, a, m) {
    const sw = a.meta.sweep || {};
    const rows = [["Recorded", fDate(a.meta.created)], ["Played from", a.meta.player || "unknown"], ["Channels played", chanLabel(m)], ["Microphone input", `Input ${(a.meta.mic_ch ?? 0) + 1}`],
      ["Sample rate", `${sw.fs} Hz`], ["Sweep", `${sw.f1}–${sw.f2} Hz, ${sw.duration} s, ${fmt(sw.level_dbfs, 0)} dBFS`], ["Recording length", `${a.recording_seconds} s`],
      ["Analysis window", `${a.window_ms} ms`], ["Calibration", a.cal || "none"], ["Clock correction", `${fmt(a.clock_ppm, 2)} ppm`], ["Peak level", `${fmt(a.levels.peak, 1)} dBFS (${fmt(a.levels.band_peak, 1)} above 60 Hz)`],
      ["Level verdict", a.levels.advice.label]];
    body.append(h("div", { class: "tablewrap" }, h("table", {}, h("tbody", {}, rows.map(([k, v]) => h("tr", {}, h("th", { text: k, style: { width: "200px" } }), h("td", { text: v })))))));
  }

  // ------------------------------------------------------------ actions
  async function exportRew(name) {
    try {
      const res = await fetch(`/api/measurements/${encodeURIComponent(name)}/rew.zip?` + qs({ cal: opts.cal, clock: opts.clock, window_ms: opts.window_ms }));
      if (!res.ok) throw new Error((await res.json()).detail);
      download(await res.blob(), `${name}_rew.zip`);
      toast("Saved. In REW use File, Import, then the frequency response and impulse response commands.", "good");
    } catch (e) { toast(e.message, "bad"); }
  }
  async function rename(name) {
    const v = await dialog({ title: "Rename recording", body: "Letters, numbers, spaces, dots and dashes.", input: { value: name, label: "New name" }, ok: "Rename" });
    if (!v || v === name) return;
    try {
      const r = await api("/api/measurements/" + encodeURIComponent(name), { method: "PATCH", body: { name: v } });
      selected.delete(name); selected.add(r.name); primary = r.name; cache.clear(); await load();
    } catch (e) { toast(e.message, "bad"); }
  }
  async function remove(name) {
    if (!(await confirmDialog({ title: `Delete “${name}”?`, body: "The recording and its results are removed from this computer. This cannot be undone.", ok: "Delete", danger: true }))) return;
    try { await api("/api/measurements/" + encodeURIComponent(name), { method: "DELETE" }); selected.delete(name); primary = null; cache.clear(); await load(); toast("Deleted " + name); } catch (e) { toast(e.message, "bad"); }
  }

  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Recordings" }), h("p", { text: "Everything you have measured. Tick several to compare them on one chart." })),
      h("a", { class: "btn primary", href: "#/record", text: "New recording" })),
    h("div", { class: "split" }, h("div", {}, h("div", { class: "field", style: { marginBottom: "10px" } }, search), listHost), detail));
  try { await load(); } catch (e) { detail.append(h("div", { class: "banner bad" }, e.message)); }
  return () => disposeCharts();
}
