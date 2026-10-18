// Small shared helpers: DOM building, the API client, toasts, dialogs, formatting, and the live event stream.

export const MINUS = "−";

export function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false && k.startsWith("aria-")) { el.setAttribute(k, "false"); continue; }
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (v === true) el.setAttribute(k, k.startsWith("aria-") ? "true" : "");
    else el.setAttribute(k, v);
  }
  const add = (c) => {
    if (Array.isArray(c)) c.forEach(add);
    else if (c !== null && c !== undefined && c !== false) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return el;
}
export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };

// ---------------------------------------------------------------- formatting
export const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : (v < 0 ? MINUS : "") + Math.abs(v).toFixed(d);
export const sgn = (v, d = 1) => (v < 0 ? MINUS : "+") + Math.abs(v).toFixed(d);
export const fHz = (f) => f >= 1000 ? (f / 1000).toFixed(f >= 10000 ? 1 : 2).replace(/\.?0+$/, "") + " kHz" : Math.round(f) + " Hz";
export function fDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
export function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  if (s < 86400 * 14) return Math.round(s / 86400) + " days ago";
  return fDate(iso);
}
export const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;

// ---------------------------------------------------------------- API
export async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new Error("Cannot reach the app. Is it still running in the terminal?");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : "Some of the values are not valid. Please check the form.";
    } catch { /* keep the status text */ }
    throw new Error(msg);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res;
}
export const qs = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");

// ---------------------------------------------------------------- toasts and dialogs
export function toast(message, kind = "") {
  const box = document.getElementById("toasts");
  const t = h("div", { class: "toast " + kind, text: message });
  box.append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 9000 : 4500);
}

export function dialog({ title, body, ok = "OK", cancel = "Cancel", danger = false, input = null }) {
  const dlg = document.getElementById("dlg");
  return new Promise((resolve) => {
    let field = null;
    if (input) field = h("input", { type: "text", value: input.value || "", "aria-label": input.label, style: { marginTop: "10px" } });
    const done = (v) => { dlg.close(); clear(dlg); resolve(v); };
    clear(dlg).append(
      h("h3", { text: title }),
      typeof body === "string" ? h("p", { class: "muted", text: body }) : body,
      field,
      h("div", { class: "row" },
        cancel ? h("button", { class: "btn", type: "button", text: cancel, onclick: () => done(null) }) : null,
        h("button", { class: "btn " + (danger ? "danger" : "primary"), type: "button", text: ok, onclick: () => done(field ? field.value : true) }),
      ),
    );
    dlg.oncancel = (e) => { e.preventDefault(); done(null); };
    dlg.showModal();
    (field || dlg.querySelector(".primary, .danger"))?.focus();
    field?.addEventListener("keydown", (e) => { if (e.key === "Enter") done(field.value); });
  });
}
export const confirmDialog = (o) => dialog(o).then((v) => v !== null);

// ---------------------------------------------------------------- live events
const listeners = new Map();
export const bus = {
  on(type, fn) {
    if (!listeners.has(type)) listeners.set(type, new Set());
    listeners.get(type).add(fn);
    return () => listeners.get(type).delete(fn);
  },
  emit(type, data) { (listeners.get(type) || []).forEach((fn) => fn(data)); (listeners.get("*") || []).forEach((fn) => fn(data)); },
};
let source = null;
export function connectEvents() {
  if (source) return;
  source = new EventSource("/api/events");
  source.onmessage = (m) => { try { const e = JSON.parse(m.data); bus.emit(e.type, e); } catch { /* ignore a bad frame */ } };
  source.onerror = () => bus.emit("connection", { type: "connection", ok: source.readyState === 1 });
  source.onopen = () => bus.emit("connection", { type: "connection", ok: true });
}

// ---------------------------------------------------------------- shared app state
export const app = { info: null, job: { running: false }, settings: {}, devices: null, renderers: null };
let saveTimer = null;
export function setSetting(key, value) {
  app.settings[key] = value;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api("/api/settings", { method: "POST", body: app.settings }).catch(() => {}), 400);
}
export function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}

// SVG helper
export const NS = "http://www.w3.org/2000/svg";
export function svgEl(name, attrs = {}, parent) {
  const e = document.createElementNS(NS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

export const debounce = (fn, ms = 300) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
export function download(blob, name) {
  const a = h("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
export async function copyText(text, what = "Copied") {
  try { await navigator.clipboard.writeText(text); toast(what, "good"); }
  catch {
    const ta = h("textarea", { style: { position: "fixed", opacity: 0 } }); ta.value = text; document.body.append(ta); ta.select();
    try { document.execCommand("copy"); toast(what, "good"); } catch { toast("Could not copy. Select the text and copy it by hand.", "bad"); }
    ta.remove();
  }
}
