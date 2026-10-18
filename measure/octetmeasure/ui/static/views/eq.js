// EQ: fit a handful of parametric filters that move a measurement toward a gentle target, and show exactly what they do.
import { LineChart } from "../charts.js";
import { MINUS, api, app, clear, copyText, debounce, download, fHz, fmt, h, setSetting, sgn } from "../util.js";

const DEFAULTS = { bass_db: 4, corner: 105, tilt: -0.4, hp: "auto", lp: "auto", bands: 8, max_boost: 3, max_cut: 8, max_q: 3, fmin: 100, fmax: 16000, smooth: 3, window_ms: "" };

export async function mount(root, params) {
  const o = { ...DEFAULTS, ...(app.settings.eq_options || {}) };
  const charts = [], resultHost = h("div"), listHost = h("div", { class: "stack" });
  let list = await api("/api/measurements").catch(() => []);
  let selected = new Set(params.filter((n) => list.some((m) => m.name === n)));
  if (!selected.size && list.length) selected = new Set([list[0].name]);
  let token = 0;

  const save = () => setSetting("eq_options", o);
  const refit = debounce(fit, 350);
  const set = (k, v) => { o[k] = v; save(); refit(); };

  const slider = (label, key, min, max, step, unit, hint) => {
    const out = h("span", { class: "num small", text: `${fmt(+o[key], step < 1 ? 1 : 0)}${unit}` });
    const input = h("input", { id: "e-" + key, type: "range", min, max, step, value: o[key], oninput: (e) => { out.textContent = `${fmt(+e.target.value, step < 1 ? 1 : 0)}${unit}`; set(key, +e.target.value); } });
    return h("div", { class: "field" }, h("label", { for: "e-" + key }, label, " ", out), input, hint ? h("span", { class: "hint", text: hint }) : null);
  };
  const edgeField = (label, key, hint) => {
    const mode = o[key] === "auto" || o[key] === "off" ? o[key] : "hz";
    const num = h("input", { type: "number", min: 20, max: 20000, value: mode === "hz" ? o[key] : "", placeholder: "Hz", hidden: mode !== "hz", "aria-label": label + " frequency", oninput: (e) => e.target.value && set(key, +e.target.value) });
    const sel = h("select", { id: "e-" + key, onchange: (e) => { const v = e.target.value; num.hidden = v !== "hz"; if (v === "hz") { num.value = num.value || (key === "hp" ? 100 : 3000); set(key, +num.value); } else set(key, v); } },
      [["auto", "Automatic"], ["off", "Off"], ["hz", "At a frequency"]].map(([v, t]) => h("option", { value: v, text: t, selected: v === mode })));
    return h("div", { class: "field" }, h("label", { for: "e-" + key, text: label }), h("div", { class: "row" }, sel, num), h("span", { class: "hint", text: hint }));
  };

  function renderList() {
    clear(listHost);
    if (!list.length) { listHost.append(h("div", { class: "empty" }, h("h3", { text: "No recordings yet" }), h("p", { text: "Make a recording first, then come back to fit an EQ." }), h("a", { class: "btn primary", href: "#/record", text: "Record" }))); return; }
    list.forEach((m) => listHost.append(h("label", { class: "inline", style: { padding: "4px 0" } },
      h("input", { type: "checkbox", checked: selected.has(m.name), onchange: (e) => { e.target.checked ? selected.add(m.name) : selected.delete(m.name); refit(); } }), h("span", { text: m.name }),
      h("span", { class: "chip", text: ({ both: "Both", left: "Left", right: "Right" }[m.channel] || "Both") }))));
  }

  const settingsCard = h("div", { class: "stack" },
    h("div", { class: "card" }, h("h3", { text: "Recordings to fit" }), h("p", { class: "hint", style: { margin: "4px 0 8px" }, text: "Tick several recordings of the same speaker, from different mic positions, to average them. Averaging avoids correcting a dip that only exists at one spot." }), listHost),
    h("div", { class: "card" }, h("h3", { text: "Target" }),
      h("p", { class: "hint", style: { margin: "4px 0 10px" }, text: "A gentle bass rise and a slight treble tilt, in the spirit of Harman's listening research. It is a starting shape, not an official curve. The target is matched to your speaker's average level between 300 Hz and 3 kHz, so only the shape changes." }),
      h("div", { class: "stack" }, slider("Bass rise", "bass_db", 0, 8, 0.5, " dB", "How much louder the lowest bass should be."), slider("Where it starts", "corner", 60, 200, 5, " Hz", "The bass rise is half done at this frequency."), slider("Treble tilt", "tilt", -1.5, 0.5, 0.1, " dB/oct", "How fast the treble falls above 1 kHz. Steeper sounds duller."))),
    h("div", { class: "card" }, h("h3", { text: "Filters" }),
      h("div", { class: "stack", style: { marginTop: "10px" } },
        slider("Peaking filters", "bands", 1, 10, 1, "", "Most players allow 10 filters in total. High-pass and low-pass use two."), slider("Strongest boost", "max_boost", 0, 6, 0.5, " dB", "Boosts cost headroom, cuts do not."), slider("Strongest cut", "max_cut", 0, 15, 0.5, " dB"), slider("Narrowest filter (Q)", "max_q", 0.5, 6, 0.5, "", "A higher number is a narrower filter. Narrow filters chase details that move with the mic position."),
        edgeField("High-pass", "hp", "Protects the speaker below where it can play. Automatic picks where its output has fallen 10 dB."), edgeField("Low-pass", "lp", "Cuts out frequencies a bass driver cannot reproduce. Automatic only applies one if the output really rolls off."),
        h("details", { class: "fold" }, h("summary", { text: "Fitted range and smoothing" }),
          h("div", { class: "stack", style: { marginTop: "8px" } }, slider("Lowest frequency", "fmin", 40, 500, 10, " Hz"), slider("Highest frequency", "fmax", 2000, 20000, 500, " Hz"), slider("Smoothing", "smooth", 1, 12, 1, "", "Fit to a 1/N octave smoothed response. Lower numbers smooth more."))),
        h("button", { class: "btn ghost", type: "button", text: "Reset to defaults", onclick: () => { Object.assign(o, DEFAULTS); save(); root.replaceChildren(); mount(root, [...selected]); } }))));

  async function fit() {
    const names = list.filter((m) => selected.has(m.name)).map((m) => m.name), mine = ++token;
    charts.splice(0).forEach((c) => c.destroy?.());
    if (!names.length) { clear(resultHost).append(h("div", { class: "card empty" }, h("p", { text: list.length ? "Tick at least one recording to fit an EQ." : "" }))); return; }
    clear(resultHost).append(h("div", { class: "card" }, h("div", { class: "skeleton", style: { height: "320px" } })));
    try {
      const r = await api("/api/peq", { method: "POST", body: { names, options: { ...o, window_ms: o.window_ms || null } } });
      if (mine === token) show(r, names);
    } catch (e) { if (mine === token) clear(resultHost).append(h("div", { class: "banner bad" }, "Could not fit an EQ: " + e.message)); }
  }

  function show(r, names) {
    clear(resultHost);
    const holdA = h("div"), holdB = h("div", { style: { marginTop: "6px" } });
    const lowY = Math.floor((Math.min(...r.measured, ...r.target, ...r.predicted) - 2) / 5) * 5, hiY = Math.ceil((Math.max(...r.measured, ...r.target, ...r.predicted) + 2) / 5) * 5;
    charts.push(LineChart({
      host: holdA, f: r.f, x0: r.f[0], x1: r.f[r.f.length - 1], xticks: [100, 200, 300, 500, 1000, 2000, 3000, 5000, 10000, 16000], yDom: [lowY, hiY], yStep: 5, yTitle: "dB, matched at 300 Hz – 3 kHz", zeroLine: true, height: 340,
      series: [{ short: "Measured", css: "var(--muted)", y: r.measured, width: 1.8 }, { short: "Target", css: "var(--ink)", y: r.target, dash: "6 4", width: 1.6 }, { short: "With EQ", css: "var(--accent)", y: r.predicted, width: 2.6 }],
      tipFmt: (v) => fmt(v) + " dB",
    }));
    const eqAt = (fc) => { let b = 0; r.eq_f.forEach((f, i) => { if (Math.abs(Math.log(f / fc)) < Math.abs(Math.log(r.eq_f[b] / fc))) b = i; }); return r.eq_db[b]; };
    const lo2 = Math.floor((Math.min(...r.eq_db) - 1) / 2) * 2, hi2 = Math.ceil((Math.max(...r.eq_db) + 1) / 2) * 2;
    charts.push(LineChart({
      host: holdB, f: r.eq_f, x0: 60, x1: 20000, xticks: [100, 200, 500, 1000, 2000, 5000, 10000, 20000], yDom: [Math.max(lo2, -18), hi2], yStep: 2, yTitle: "EQ gain (dB)", zeroLine: true, height: 260,
      series: [{ short: "EQ curve", css: "var(--accent)", y: r.eq_db, width: 2.4 }],
      markers: r.filters.map((p) => ({ x: p.fc, y: p.kind === "PK" ? p.gain : eqAt(p.fc), css: "var(--accent)", hollow: p.kind !== "PK", r: p.kind === "PK" ? 4 : 5, label: p.kind === "PK" ? String(Math.round(p.fc)) : p.kind, dx: 0, dy: p.kind === "PK" ? (p.gain >= 0 ? -9 : 17) : -10, anchor: "middle" })),
      tipFmt: (v) => sgn(v) + " dB",
      tipNote: (x) => r.filters.filter((p) => Math.abs(Math.log2(x / p.fc)) < 0.1).map((p) => p.kind === "PK" ? `Peak ${Math.round(p.fc)} Hz, ${sgn(p.gain)} dB, Q ${p.q}` : `${p.kind === "HP" ? "High-pass" : "Low-pass"} ${Math.round(p.fc)} Hz`).join("<br>"),
    }));
    const better = r.rms_after < r.rms_before - 0.3;
    resultHost.append(
      h("div", { class: "card" },
        h("header", {}, h("h2", { text: "The fitted EQ" }), h("div", { class: "row" },
          h("button", { class: "btn primary", type: "button", text: "Copy filters", onclick: () => copyText(r.text, "Filters copied") }),
          h("button", { class: "btn", type: "button", text: "Copy table", onclick: () => copyText(r.table, "Table copied") }),
          h("button", { class: "btn", type: "button", text: "Download .txt", onclick: () => download(new Blob([r.text + "\n"], { type: "text/plain" }), names.join("+") + "_eq.txt") }))),
        h("div", { class: "statrow" },
          h("span", {}, "Distance from target ", h("b", { text: `${fmt(r.rms_before, 1)} → ${fmt(r.rms_after, 1)} dB` }), " ", h("span", { class: "chip " + (better ? "good" : "warn"), text: better ? "improves" : "little change" })),
          h("span", {}, "Fitted ", h("b", { text: `${fHz(r.f_lo)} – ${fHz(r.f_hi)}` })), h("span", {}, "Largest boost ", h("b", { text: `${sgn(r.max_boost)} dB` })),
          h("span", {}, "Suggested preamp ", h("b", { text: `${sgn(r.preamp)} dB` }))),
        holdA,
        h("p", { class: "hint", style: { margin: "6px 0 14px" } }, "Gray is your measurement, smoothed. The dashed line is the target. Blue is the measurement with the EQ applied, as predicted by the filter maths. It has not been re-measured."),
        holdB,
        h("div", { class: "tablewrap", style: { marginTop: "14px" } }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", { text: "#" }), h("th", { text: "Type" }), h("th", { class: "r", text: "Frequency (Hz)" }), h("th", { class: "r", text: "Gain (dB)" }), h("th", { class: "r", text: "Q" }))),
          h("tbody", {}, r.filters.map((p, i) => h("tr", {}, h("td", { text: i + 1 }), h("td", { text: { PK: "Peak", HP: "High-pass", LP: "Low-pass" }[p.kind] }), h("td", { class: "r", text: Math.round(p.fc) }), h("td", { class: "r", text: p.kind === "PK" ? sgn(p.gain) : "–" }), h("td", { class: "r", text: p.q })))))),
        h("p", { class: "hint", style: { marginTop: "10px" } }, "To use it: enter each filter by hand in your player's parametric EQ, or paste the copied list into an app that reads Equalizer APO format. Set the preamp, or lower the volume by that amount, so the boosts cannot clip. Filters are standard peaking filters; high-pass and low-pass are second order (Q 0.71). If your player's Q or slopes differ, expect small differences.")));
  }

  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "EQ" }), h("p", { text: "Turn a measurement into a short list of filters you can enter in your player, and see what they will do before you listen." }))),
    h("div", { class: "split eq" }, settingsCard, resultHost));
  renderList();
  fit();
  return () => { charts.splice(0).forEach((c) => c.destroy?.()); token++; };
}
