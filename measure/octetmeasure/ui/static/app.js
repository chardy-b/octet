// App shell: navigation, the always-visible job indicator, and loading shared state.
import { api, app, applyTheme, bus, connectEvents, h, toast } from "./util.js";
import * as record from "./views/record.js";
import * as recordings from "./views/recordings.js";
import * as eq from "./views/eq.js";
import * as calibration from "./views/calibration.js";
import * as settings from "./views/settings.js";
import * as autotune from "./views/autotune.js";

const views = { record, recordings, eq, calibration, settings, autotune };
const main = document.getElementById("main");
let current = null;

function updatePill() {
  const box = document.getElementById("jobpill");
  box.replaceChildren();
  if (app.job.running) {
    const label = { auto: "Auto Tune", record: "Recording", monitor: "Listening", noise: "Checking noise", clock: "Checking clock" }[app.job.kind] || "Working";
    box.append(h("a", { href: app.job.kind === 'auto' ? '#/autotune' : '#/record', class: "pill live", title: app.job.message || "", text: label + "…" }));
  }
}

async function route() {
  const [, view = "record", ...rest] = (location.hash || "#/record").split("/");
  const name = views[view] ? view : "record";
  current?.unmount?.();
  main.replaceChildren();
  document.querySelectorAll("nav.side a.nav").forEach((a) => (a.dataset.view === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  const params = rest.map(decodeURIComponent);
  try {
    current = { unmount: await views[name].mount(main, params) };
  } catch (e) {
    console.error(e);
    main.append(h("div", { class: "card" }, h("h2", { text: "Something went wrong" }), h("p", { class: "muted", text: e.message })));
  }
  document.title = { autotune: "Auto Tune", record: "Record", recordings: "Recordings", eq: "EQ", calibration: "Calibration", settings: "Settings" }[name] + " · Octet Measure";
  window.scrollTo(0, 0);
}

async function boot() {
  try {
    app.info = await api("/api/info");
  } catch (e) {
    main.append(h("div", { class: "card" }, h("h2", { text: "Cannot reach the app" }), h("p", { class: "muted", text: e.message })));
    return;
  }
  app.settings = app.info.settings || {};
  applyTheme(app.settings.theme);
  document.getElementById("ver").textContent = "v" + app.info.version + (app.info.demo ? " · demo mode" : "");
  connectEvents();
  app.job = await api("/api/job");
  const auto = await api('/api/auto');
  if(auto.running) app.job = {...auto,kind:'auto'};
  bus.on('autotune', e => { app.job = {...e,kind:'auto'}; updatePill(); });
  bus.on("status", (e) => { app.job = e; updatePill(); });
  bus.on("state", (e) => { app.job = { ...app.job, running: true, kind: e.job, state: e.state, message: e.message }; updatePill(); });
  bus.on("idle", () => { app.job = { ...app.job, running: false }; updatePill(); });
  bus.on("error", (e) => toast(e.message, "bad"));
  updatePill();
  window.addEventListener("hashchange", route);
  route();
}

boot();
