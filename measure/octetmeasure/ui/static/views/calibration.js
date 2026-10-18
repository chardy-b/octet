// Calibration: the microphone's calibration files, which one is active, and what they correct.
import { LineChart } from "../charts.js";
import { api, app, clear, confirmDialog, download, fHz, fmt, h, sgn, toast } from "../util.js";

export async function mount(root, params) {
  let files = [], active = null, current = params[0] || null;
  const charts = [], listHost = h("div", { class: "list" }), detail = h("div");

  async function load() {
    const r = await api("/api/calibrations");
    files = r.files; active = r.active;
    if (!current || !files.some((f) => f.name === current)) current = active || files[0]?.name || null;
    renderList(); renderDetail();
  }

  function renderList() {
    clear(listHost);
    listHost.append(h("button", { class: "item", type: "button", "aria-current": String(!active && !!files.length), onclick: () => setActive(null) },
      h("input", { type: "radio", name: "act", checked: !active, tabindex: -1, "aria-hidden": "true" }),
      h("div", {}, h("div", { class: "t", text: "No calibration" }), h("div", { class: "m", text: "Treat the microphone as perfectly flat" }))));
    for (const f of files) {
      listHost.append(h("button", { class: "item", type: "button", "aria-current": String(f.name === current), onclick: () => { current = f.name; renderList(); renderDetail(); } },
        h("input", { type: "radio", name: "act", checked: f.active, tabindex: -1, "aria-hidden": "true", onclick: (e) => { e.stopPropagation(); setActive(f.name); } }),
        h("div", {}, h("div", { class: "t", text: f.name }),
          h("div", { class: "m" }, f.active ? h("span", { class: "chip good", text: "Active" }) : null, f.serial ? h("span", { class: "chip", text: "Serial " + f.serial }) : null,
            f.error ? h("span", { class: "chip bad", text: f.error }) : h("span", { text: `${f.points} points` })))));
    }
  }

  async function renderDetail() {
    charts.splice(0).forEach((c) => c.destroy?.());
    clear(detail);
    if (!files.length) {
      detail.append(h("div", { class: "card empty" }, h("h3", { text: "No calibration file yet" }),
        h("p", { text: "A calibration file tells the app how your microphone's response differs from flat, so it can remove that from your measurements. Most measurement microphones have one for your exact serial number on the maker's website." }),
        h("p", { class: "hint", style: { marginTop: "8px" }, text: "Look for a plain text file with lines of frequency and dB. Without it the high frequencies will be a little off, usually by a few dB." })));
      return;
    }
    if (!current) return;
    let c;
    try { c = await api("/api/calibrations/" + encodeURIComponent(current)); } catch (e) { detail.append(h("div", { class: "banner bad" }, e.message)); return; }
    const hold = h("div");
    const isActive = c.active;
    detail.append(h("div", { class: "card" },
      h("header", {}, h("div", {}, h("h2", { text: c.name }), h("div", { class: "small muted", text: c.serial ? `Serial ${c.serial}` : "" })),
        h("div", { class: "row" },
          isActive ? h("span", { class: "chip good", text: "Active" }) : h("button", { class: "btn primary", type: "button", text: "Use this one", onclick: () => setActive(c.name) }),
          h("button", { class: "btn", type: "button", text: "Download", onclick: () => download(new Blob([c.text], { type: "text/plain" }), c.name) }),
          h("button", { class: "btn ghost danger", type: "button", text: "Delete", onclick: () => remove(c.name) }))),
      h("div", { class: "statrow" },
        h("span", {}, "Points ", h("b", { text: c.points })), h("span", {}, "Range ", h("b", { text: `${fHz(c.f_min)} – ${fHz(c.f_max)}` })),
        c.sens_db != null ? h("span", {}, "Sensitivity at 1 kHz ", h("b", { text: `${fmt(c.sens_db, 1)} dB` })) : null, h("span", {}, "Largest deviation ", h("b", { text: `${fmt(c.max_dev_db, 1)} dB` }))),
      hold,
      h("p", { class: "hint", style: { marginTop: "6px" }, text: "Above zero the microphone reads too loud at that frequency, so the app lowers your measurement there; below zero it raises it." }),
      c.header.length ? h("details", { class: "fold", style: { marginTop: "10px" } }, h("summary", { text: "Notes in the file" }), h("div", { class: "codebox" }, c.header.join("\n"))) : null,
      h("details", { class: "fold" }, h("summary", { text: "Show the whole file" }), h("div", { class: "codebox" }, c.text))));
    const lo = Math.min(...c.db), hi = Math.max(...c.db), pad = Math.max(1, (hi - lo) * 0.2);
    charts.push(LineChart({ host: hold, f: c.f, x0: Math.max(10, c.f[0]), x1: Math.min(40000, c.f[c.f.length - 1]), yDom: [Math.floor(lo - pad), Math.ceil(hi + pad)], yStep: undefined, yTitle: "Microphone deviation (dB)", zeroLine: true, height: 280,
      xticks: [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000], series: [{ short: "Deviation", css: "var(--s1)", y: c.db, width: 2.2 }], tipFmt: (v) => sgn(v) + " dB" }));
  }

  async function setActive(name) {
    try { await api("/api/calibrations/active", { method: "POST", body: { name } }); app.info.active_cal = name; toast(name ? `Using ${name}` : "No calibration will be applied", "good"); await load(); } catch (e) { toast(e.message, "bad"); }
  }
  async function remove(name) {
    if (!(await confirmDialog({ title: `Delete ${name}?`, body: "Recordings are not changed, but their analysis will no longer use this calibration.", ok: "Delete", danger: true }))) return;
    try { await api("/api/calibrations/" + encodeURIComponent(name), { method: "DELETE" }); current = null; await load(); } catch (e) { toast(e.message, "bad"); }
  }
  async function importFile(file) {
    if (!file) return;
    if (file.size > 2_000_000) { toast("That file is too large to be a calibration file.", "bad"); return; }
    try {
      const text = await file.text();
      const r = await api("/api/calibrations", { method: "POST", body: { name: file.name, text, activate: true } });
      current = r.name; toast(`Imported ${r.name} and made it the active calibration.`, "good"); await load();
    } catch (e) { toast(e.message, "bad"); }
  }

  const picker = h("input", { type: "file", accept: ".txt,.cal,.frd,.csv,text/plain", hidden: true, onchange: (e) => { importFile(e.target.files[0]); e.target.value = ""; } });
  const drop = h("div", { class: "drop", tabindex: 0, role: "button", "aria-label": "Import a calibration file", onclick: () => picker.click(), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); picker.click(); } } },
    h("b", { text: "Import a calibration file" }), h("div", { class: "small", text: "Drop it here, or click to choose" }));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => importFile(e.dataTransfer.files[0]));

  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Calibration" }), h("p", { text: "Your microphone's calibration file removes the microphone's own colouring from every measurement." }))),
    h("div", { class: "split" }, h("div", { class: "stack" }, drop, picker, listHost), detail));
  try { await load(); } catch (e) { detail.append(h("div", { class: "banner bad" }, e.message)); }
  return () => charts.splice(0).forEach((c) => c.destroy?.());
}
