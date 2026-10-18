// Settings: appearance, the player clock offset, where data lives, and about.
import { api, app, applyTheme, bus, clear, confirmDialog, fDate, fmt, h, setSetting, toast } from "../util.js";

export async function mount(root) {
  const offs = [];
  const info = app.info;
  const clockHost = h("div"), runHost = h("div");

  // ------------------------------------------------------------ appearance
  const theme = h("div", { class: "seg", role: "group", "aria-label": "Theme" });
  [["system", "Match my system"], ["light", "Light"], ["dark", "Dark"]].forEach(([v, t]) => theme.append(h("button", { type: "button", text: t, "aria-pressed": String((app.settings.theme || "system") === v), onclick: () => {
    setSetting("theme", v); applyTheme(v); theme.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.textContent === t)));
  } })));

  // ------------------------------------------------------------ clock
  async function renderClock() {
    const c = await api("/api/clock").catch(() => null);
    app.info.clock = c; app.info.clock_ppm = c ? c.ppm : 0;
    clear(clockHost);
    if (c) {
      clockHost.append(h("div", { class: "statrow" }, h("span", {}, "Offset ", h("b", { text: `${fmt(c.ppm, 2)} ppm` })), c.player ? h("span", {}, "Player ", h("b", { text: c.player })) : null, h("span", { class: "muted", text: c.saved ? "Measured " + fDate(c.saved) : "" }),
        c.runs ? h("span", { class: "muted", text: "Runs: " + c.runs.map((v) => fmt(v, 1)).join(", ") + " ppm" }) : null));
    } else clockHost.append(h("p", { class: "muted", text: "Not measured yet." }));
  }

  const rendererSel = h("select", { id: "s-renderer", "aria-label": "Player" });
  async function loadRenderers(btn) {
    if (btn) { btn.disabled = true; btn.textContent = "Searching…"; }
    try { app.renderers = await api("/api/renderers"); } catch (e) { toast(e.message, "bad"); }
    if (btn) { btn.disabled = false; btn.textContent = "Search again"; }
    clear(rendererSel);
    const list = app.renderers || [];
    if (!list.length) rendererSel.append(h("option", { value: "", text: app.renderers ? "No players found" : "Search to see players" }));
    list.forEach((r) => rendererSel.append(h("option", { value: r.name, text: `${r.name} (${r.ip})`, selected: r.name === app.settings.renderer })));
  }
  const searchBtn = h("button", { class: "btn", type: "button", text: app.renderers ? "Search again" : "Search the network", onclick: (e) => loadRenderers(e.currentTarget) });
  const measureBtn = h("button", { class: "btn primary", type: "button", text: "Measure the clock offset" });
  const status = h("div", { class: "small muted", style: { marginTop: "8px" } }), prog = h("i"), manual = h("input", { type: "number", step: "0.1", placeholder: "ppm", "aria-label": "Offset in ppm", style: { width: "110px" } }), manualPlayer = h("input", { type: "text", placeholder: "Player name", "aria-label": "Player name", style: { width: "180px" } });

  measureBtn.onclick = async () => {
    const dev = (app.devices || (app.devices = await api("/api/devices"))).filter((d) => d.inputs > 0);
    const mic = dev.find((d) => d.name === app.settings.in_device) || dev.find((d) => d.default_input) || dev[0];
    const player = info.demo ? "demo" : "dlna", renderer = rendererSel.value || (info.demo ? "Demo streamer" : "");
    if (!renderer) { toast("Search for your player first, then choose it.", "bad"); return; }
    try {
      await api("/api/job/clock/start", { method: "POST", body: { config: { player, renderer, fs: +(app.settings.fs || 44100), in_device: mic?.name || "", mic_ch: +(app.settings.mic_ch || 0), channels: Math.min(mic?.inputs || 2, 8), repeat: 2 }, save: true } });
      clear(runHost).append(h("div", { class: "progress", "aria-hidden": "true" }, prog), status);
      prog.style.width = "0%"; status.textContent = "Starting…";
    } catch (e) { toast(e.message, "bad"); }
  };
  let level = 0;
  offs.push(bus.on("state", (e) => { if (e.job === "clock") status.textContent = e.message || e.state; }));
  offs.push(bus.on("level", (e) => { if (e.job === "clock" && e.total) prog.style.width = `${Math.min(100, (e.t / e.total) * 100)}%`; }));
  offs.push(bus.on("done", async (e) => { if (e.result.kind === "clock") { clear(runHost); toast(`Clock offset ${fmt(e.result.ppm, 1)} ppm saved.`, "good"); await renderClock(); } }));
  offs.push(bus.on("error", () => clear(runHost)));

  const clockCard = h("div", { class: "card" },
    h("header", {}, h("h2", { text: "Player clock offset" })),
    h("p", { class: "muted", style: { maxWidth: "70ch" }, text: "A streaming player and your microphone interface each run on their own clock, and no two are exactly alike. At a few tens of parts per million that is inaudible in music, but it smears the top of a swept measurement slightly. The app measures the difference once with a steady tone and then corrects for it. You only need to do this once per player." }),
    h("div", { style: { margin: "12px 0" } }, clockHost),
    h("div", { class: "row" }, h("div", { class: "field", style: { flex: "1 1 260px" } }, h("label", { for: "s-renderer", text: "Player to measure" }), rendererSel), h("div", { style: { alignSelf: "flex-end" }, class: "row" }, searchBtn, measureBtn)),
    runHost,
    h("p", { class: "hint", style: { marginTop: "8px" }, text: "It plays a quiet 1 kHz tone through your player for about half a minute and listens with the microphone on the Record page. Only needed for network players: your own sound card and the sweep downloaded as a file do not need it." }),
    h("details", { class: "fold", style: { marginTop: "10px" } }, h("summary", { text: "Enter a value by hand, or clear it" }),
      h("div", { class: "row", style: { marginTop: "8px" } }, manual, manualPlayer,
        h("button", { class: "btn", type: "button", text: "Save", onclick: async () => { if (manual.value === "") return; try { await api("/api/clock", { method: "POST", body: { ppm: +manual.value, player: manualPlayer.value } }); await renderClock(); toast("Saved", "good"); } catch (e) { toast(e.message, "bad"); } } }),
        h("button", { class: "btn ghost danger", type: "button", text: "Clear", onclick: async () => { if (await confirmDialog({ title: "Clear the clock offset?", body: "Analyses will no longer correct for it.", ok: "Clear", danger: true })) { await api("/api/clock", { method: "DELETE" }); await renderClock(); } } }))));

  const row = (k, v) => h("tr", {}, h("th", { text: k, style: { width: "190px" } }), h("td", { text: v }));
  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Settings" }), h("p", { text: "Look and feel, the player clock offset, and where your data lives." }))),
    h("div", { class: "stack" },
      h("div", { class: "card" }, h("header", {}, h("h2", { text: "Appearance" })), theme),
      clockCard,
      h("div", { class: "card" }, h("header", {}, h("h2", { text: "Your data" })),
        h("div", { class: "tablewrap" }, h("table", {}, h("tbody", {}, row("Recordings and results", info.data_dir), row("Settings file", info.settings_file)))),
        h("p", { class: "hint", style: { marginTop: "8px" }, text: "Everything stays on this computer. Back up the recordings folder to keep your measurements. To use a different folder, set the OCTET_DATA environment variable before starting the app." })),
      h("div", { class: "card" }, h("header", {}, h("h2", { text: "About" })),
        h("div", { class: "tablewrap" }, h("table", {}, h("tbody", {}, row("Octet Measure", "v" + info.version + (info.demo ? " (demo mode)" : "")), row("System", `${info.platform}, Python ${info.python}`), row("Interface", "Local web app. Press Ctrl+C in the terminal to stop it.")))),
        h("p", { class: "hint", style: { marginTop: "8px" } }, "For developers: ", h("a", { href: "/api/docs", target: "_blank", rel: "noopener", text: "the app's API documentation" }), "."))));
  await renderClock();
  loadRenderers(null);
  return () => offs.forEach((f) => f());
}
