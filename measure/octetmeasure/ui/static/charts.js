// Charts drawn as plain SVG, no libraries. LineChart (log or linear x, hover readout), BandBars, LevelTimeline.
import { MINUS, fHz, h, svgEl } from "./util.js";

const val = (v) => (typeof v === "function" ? v() : v);

function interp(xs, ys, x, log) {
  const n = xs.length;
  if (!n || x < xs[0] || x > xs[n - 1]) return null;
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; }
  const a = log ? Math.log(xs[lo]) : xs[lo], b = log ? Math.log(xs[hi]) : xs[hi], c = log ? Math.log(x) : x;
  const t = b === a ? 0 : (c - a) / (b - a);
  const ya = ys[lo], yb = ys[hi];
  if (ya === null || yb === null) return null;
  return ya + (yb - ya) * t;
}

function niceTicks(lo, hi, target = 7) {
  const span = hi - lo, raw = span / target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return { step, ticks: out };
}

/* cfg: host, series[{short, css, f?, y, snr?, width?, dash?, opacity?, visible?}], f (shared grid), xLog (default true),
        x0, x1, xticks, yDom, yStep, yTitle, xTitle, zeroLine, fill, labels, height, bands, markers, annotate,
        tipFmt(v, s), tipNote(x) */
export function LineChart(cfg) {
  const host = cfg.host;
  host.classList.add("plot");
  const tip = h("div", { class: "tip", hidden: true });
  let hoverX = null;

  function draw() {
    host.querySelectorAll("svg").forEach((s) => s.remove());
    const log = cfg.xLog !== false;
    const grid = val(cfg.f);
    const vis = cfg.series.filter((s) => s.visible === undefined || s.visible());
    const X0 = val(cfg.x0) ?? (log ? 20 : 0), X1 = val(cfg.x1) ?? (log ? 20000 : 1);
    const W = Math.max(host.clientWidth, 280), narrow = W < 520;
    const H = val(cfg.height) ?? (narrow ? 280 : 360);
    const m = { l: narrow ? 42 : 52, r: cfg.labels ? (narrow ? 54 : 88) : 14, t: 10, b: cfg.xTitle === null ? 24 : 38 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const sx = log ? (f) => m.l + iw * Math.log(f / X0) / Math.log(X1 / X0) : (v) => m.l + iw * (v - X0) / (X1 - X0);
    const sxInv = log ? (px) => X0 * Math.pow(X1 / X0, (px - m.l) / iw) : (px) => X0 + (px - m.l) / iw * (X1 - X0);
    const [y0, y1] = val(cfg.yDom);
    const sy = (v) => m.t + ih * (1 - (v - y0) / (y1 - y0));
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, "aria-hidden": "true" });
    host.insertBefore(svg, tip);
    const fillOn = val(cfg.fill), labelsOn = val(cfg.labels);

    (val(cfg.bands) || []).forEach((b, k) => {
      const xa = sx(Math.max(b.f0, X0)), xb = sx(Math.min(b.f1, X1));
      svgEl("rect", { x: xa, y: m.t, width: Math.max(xb - xa, 1), height: ih, fill: b.fill || "var(--band)" }, svg);
      if (b.label) svgEl("text", { class: "note", x: xa + 6, y: m.t + 14 + k * 15 }, svg).textContent = b.label;
    });

    const yStep = cfg.yStep || niceTicks(y0, y1, 6).step;
    for (let v = Math.ceil(y0 / yStep) * yStep; v <= y1 + 1e-9; v += yStep) {
      const zero = v === 0 && (fillOn || cfg.zeroLine);
      svgEl("line", { x1: m.l, x2: m.l + iw, y1: sy(v), y2: sy(v), stroke: zero ? "var(--line2)" : "var(--line)", "stroke-width": zero ? 1.5 : 1 }, svg);
      svgEl("text", { x: m.l - 8, y: sy(v) + 4, "text-anchor": "end" }, svg).textContent = (v < 0 ? MINUS : "") + Math.abs(+v.toFixed(2));
    }
    if (cfg.yTitle) svgEl("text", { class: "axt", transform: `translate(12 ${m.t + ih / 2}) rotate(-90)`, "text-anchor": "middle" }, svg).textContent = cfg.yTitle;

    const ticks = log
      ? (cfg.xticks || [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]).filter((f) => f >= X0 * 0.999 && f <= X1 * 1.001)
      : (cfg.xticks || niceTicks(X0, X1, 8).ticks);
    for (const t of ticks) {
      svgEl("line", { x1: sx(t), x2: sx(t), y1: m.t, y2: m.t + ih, stroke: "var(--line)" }, svg);
      svgEl("text", { x: sx(t), y: m.t + ih + 17, "text-anchor": log && t >= X1 * 0.999 ? "end" : "middle" }, svg).textContent = log ? (t >= 1000 ? t / 1000 + "k" : t) : (t < 0 ? MINUS : "") + Math.abs(t);
    }
    svgEl("line", { x1: m.l, x2: m.l + iw, y1: m.t + ih, y2: m.t + ih, stroke: "var(--line2)" }, svg);
    if (cfg.xTitle !== null) svgEl("text", { class: "axt", x: m.l + iw / 2, y: H - 4, "text-anchor": "middle" }, svg).textContent = cfg.xTitle ?? (log ? "Frequency (Hz)" : "");

    const xsOf = (s) => s.f || grid;
    if (fillOn && vis[0]) {
      const s = vis[0], xs = xsOf(s), z = sy(0);
      const seg = (pos) => {
        let d = "";
        xs.forEach((f, i) => { const v = pos ? Math.max(s.y[i], 0) : Math.min(s.y[i], 0); d += (i ? "L" : "M") + sx(f).toFixed(1) + " " + sy(v).toFixed(1); });
        return d + `L${sx(xs[xs.length - 1]).toFixed(1)} ${z}L${sx(xs[0]).toFixed(1)} ${z}Z`;
      };
      svgEl("path", { d: seg(false), fill: "var(--bad)", "fill-opacity": 0.25 }, svg);
      svgEl("path", { d: seg(true), fill: "var(--accent)", "fill-opacity": 0.25 }, svg);
    }
    const clipId = "c" + Math.random().toString(36).slice(2, 8);
    const defs = svgEl("defs", {}, svg);
    svgEl("rect", { x: m.l, y: m.t - 2, width: iw, height: ih + 4 }, svgEl("clipPath", { id: clipId }, defs));
    const plot = svgEl("g", { "clip-path": `url(#${clipId})` }, svg);

    for (const s of vis) {
      const xs = xsOf(s), wdt = s.width || 2, ys = s.y;
      const path = (pts) => pts.map((p, k) => (k ? "L" : "M") + sx(p[0]).toFixed(1) + " " + sy(p[1]).toFixed(1)).join("");
      if (!s.snr) {
        const pts = xs.map((f, i) => [f, ys[i]]).filter((p) => p[1] !== null);
        svgEl("path", { d: path(pts), fill: "none", stroke: s.css, "stroke-width": wdt, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": s.dash || "none", opacity: s.opacity ?? 1 }, plot);
        continue;
      }
      const lim = cfg.snrMin ?? 10;
      let i = 0;
      while (i < xs.length - 1) {
        const ok = Math.min(s.snr[i], s.snr[i + 1]) >= lim;
        let j = i + 1; const pts = [[xs[i], ys[i]]];
        while (j < xs.length && (Math.min(s.snr[j - 1], s.snr[j]) >= lim) === ok) { pts.push([xs[j], ys[j]]); j++; }
        if (j < xs.length) pts.push([xs[j], ys[j]]);
        svgEl("path", { d: path(pts), fill: "none", stroke: s.css, "stroke-width": ok ? wdt : 1.5, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": ok ? "none" : "2 4", opacity: ok ? 1 : 0.8 }, plot);
        i = Math.max(j - 1, i + 1);
      }
    }

    if (labelsOn) {
      const L = vis.map((s) => ({ s, y: sy(s.y[s.y.length - 1]) })).sort((a, b) => a.y - b.y);
      for (let k = 1; k < L.length; k++) if (L[k].y - L[k - 1].y < 15) L[k].y = L[k - 1].y + 15;
      for (const l of L) svgEl("text", { class: "lbl", x: m.l + iw + 8, y: l.y + 4 }, svg).textContent = l.s.lab || l.s.short;
    }
    for (const mk of val(cfg.markers) || []) {
      if (mk.x < X0 || mk.x > X1) continue;
      const x = sx(mk.x), y = sy(mk.y);
      svgEl("circle", { cx: x, cy: y, r: mk.r || 4, fill: mk.hollow ? "var(--surface)" : mk.css, stroke: mk.css, "stroke-width": 2 }, svg);
      if (mk.label) svgEl("text", { class: mk.bold ? "lbl" : "note", x: x + (mk.dx ?? 8), y: y + (mk.dy ?? 4), "text-anchor": mk.anchor || "start" }, svg).textContent = mk.label;
    }
    cfg.annotate?.(svg, sx, sy, m, iw);

    const cross = svgEl("line", { y1: m.t, y2: m.t + ih, stroke: "var(--ink2)", "stroke-width": 1, opacity: 0 }, svg);
    const dots = vis.map((s) => svgEl("circle", { r: 4.5, fill: s.css, stroke: "var(--surface)", "stroke-width": 2, opacity: 0 }, svg));
    const hit = svgEl("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent", style: "cursor:crosshair" }, svg);

    const show = (x) => {
      hoverX = x;
      const px = sx(x);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("opacity", 1);
      const rows = [];
      vis.forEach((s, k) => {
        const v = interp(xsOf(s), s.y, x, log);
        if (v === null) { dots[k].setAttribute("opacity", 0); return; }
        dots[k].setAttribute("cx", px); dots[k].setAttribute("cy", sy(v)); dots[k].setAttribute("opacity", 1);
        rows.push(`<div class="tr"><span><i class="sw" style="background:${s.css}"></i>${s.short}</span><b>${cfg.tipFmt ? cfg.tipFmt(v, s) : v.toFixed(1)}</b></div>`);
      });
      const note = cfg.tipNote?.(x, vis);
      tip.innerHTML = `<div class="hz">${log ? fHz(x) : (cfg.xFmt ? cfg.xFmt(x) : x.toFixed(2))}</div>${rows.join("")}${note ? `<div class="low">${note}</div>` : ""}`;
      tip.hidden = false;
      const tw = tip.offsetWidth;
      let left = px + 14; if (left + tw > W - 4) left = px - tw - 14;
      tip.style.left = Math.max(4, left) + "px"; tip.style.top = (m.t + 6) + "px";
    };
    const hide = () => { hoverX = null; tip.hidden = true; cross.setAttribute("opacity", 0); dots.forEach((d) => d.setAttribute("opacity", 0)); };
    const at = (ev) => {
      const r = svg.getBoundingClientRect();
      show(sxInv((ev.clientX - r.left) * (W / r.width)));
    };
    hit.addEventListener("pointermove", at); hit.addEventListener("pointerdown", at); hit.addEventListener("pointerleave", hide);
    host.tabIndex = 0;
    host.onkeydown = (ev) => {
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        const dir = ev.key === "ArrowRight" ? 1 : -1;
        const base = hoverX ?? (log ? Math.sqrt(X0 * X1) : (X0 + X1) / 2);
        show(Math.min(X1, Math.max(X0, log ? base * Math.pow(2, dir / 12) : base + dir * (X1 - X0) / 60)));
      } else if (ev.key === "Escape") hide();
    };
    host.onblur = hide;
    if (hoverX !== null && hoverX >= X0 && hoverX <= X1) show(hoverX); else hoverX = null;
  }

  host.append(tip);
  draw();
  const ro = new ResizeObserver(() => draw());
  ro.observe(host);
  return { draw, destroy: () => ro.disconnect() };
}

/* Signal-to-noise per third-octave band, coloured by how trustworthy the band is. */
export function BandBars({ host, f, snr, labels = true }) {
  host.classList.add("plot");
  const tip = h("div", { class: "tip", hidden: true });
  const tone = (v) => (v < 10 ? "var(--bad)" : v < 25 ? "var(--warn)" : v < 40 ? "var(--ok)" : "var(--good)");
  function draw() {
    host.querySelectorAll("svg").forEach((s) => s.remove());
    const W = Math.max(host.clientWidth, 280), H = 280;
    const m = { l: 46, r: 14, t: 10, b: 36 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const X0 = Math.max(20, f[0] / 1.2), X1 = Math.min(22000, f[f.length - 1] * 1.2), top = 60;
    const sx = (v) => m.l + iw * Math.log(v / X0) / Math.log(X1 / X0), sy = (v) => m.t + ih * (1 - Math.max(0, Math.min(v, top)) / top);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, "aria-hidden": "true" });
    host.insertBefore(svg, tip);
    for (const [v, txt] of [[0, "0"], [10, "10"], [25, "25"], [40, "40"], [60, "60"]]) {
      svgEl("line", { x1: m.l, x2: m.l + iw, y1: sy(v), y2: sy(v), stroke: v === 10 || v === 40 ? "var(--line2)" : "var(--line)", "stroke-dasharray": v === 10 || v === 40 ? "4 4" : "none" }, svg);
      svgEl("text", { x: m.l - 8, y: sy(v) + 4, "text-anchor": "end" }, svg).textContent = txt;
    }
    svgEl("text", { class: "axt", transform: `translate(12 ${m.t + ih / 2}) rotate(-90)`, "text-anchor": "middle" }, svg).textContent = "Signal-to-noise (dB)";
    for (const t of [100, 200, 500, 1000, 2000, 5000, 10000, 20000].filter((t) => t >= X0 && t <= X1))
      svgEl("text", { x: sx(t), y: m.t + ih + 17, "text-anchor": "middle" }, svg).textContent = t >= 1000 ? t / 1000 + "k" : t;
    if (labels) svgEl("text", { class: "axt", x: m.l + iw / 2, y: H - 4, "text-anchor": "middle" }, svg).textContent = "Frequency (Hz), third-octave bands";
    const bars = f.map((fc, i) => {
      const w = Math.pow(2, 1 / 6), a = sx(fc / w), b = sx(fc * w), v = snr[i];
      const r = svgEl("rect", { x: a + 1, y: sy(v), width: Math.max(b - a - 2, 1), height: Math.max(sy(0) - sy(v), 1), fill: tone(v), rx: 2 }, svg);
      r.addEventListener("pointerenter", () => {
        tip.innerHTML = `<div class="hz">${fHz(fc)}</div><div class="tr"><span>Signal-to-noise</span><b>${v.toFixed(0)} dB</b></div><div class="low">${v < 10 ? "Mostly noise: do not trust this band" : v < 25 ? "Usable, with some noise" : v < 40 ? "Good" : "Excellent"}</div>`;
        tip.hidden = false;
        const px = (a + b) / 2, tw = tip.offsetWidth;
        tip.style.left = Math.max(4, Math.min(px - tw / 2, W - tw - 4)) + "px"; tip.style.top = Math.max(0, sy(v) - 78) + "px";
      });
      r.addEventListener("pointerleave", () => { tip.hidden = true; });
      return r;
    });
    return bars;
  }
  host.append(tip);
  draw();
  const ro = new ResizeObserver(() => draw());
  ro.observe(host);
  return { draw, destroy: () => ro.disconnect() };
}

/* Level over time with the ranges a recording should land in. Used while listening and while recording. */
export function LevelTimeline({ host, zones, windowS = 30, height = 230 }) {
  host.classList.add("plot");
  let data = [], marks = [], total = null, raf = 0, hold = 0;
  const tip = h("div", { class: "tip", hidden: true });
  host.append(tip);

  const api = {
    reset(newTotal = null) { data = []; marks = []; total = newTotal; schedule(); },
    push(t, peak, rms) { data.push({ t, peak, rms }); if (total === null && data.length > 4000) data.splice(0, data.length - 4000); schedule(); },
    mark(t, label) { marks.push({ t, label }); schedule(); },
    draw,
  };
  // Redraw on the next frame, or after 200 ms if the browser has paused animation frames (a tab in the background).
  function schedule() {
    if (raf) return;
    const run = () => { if (!raf) return; cancelAnimationFrame(raf); raf = 0; draw(); };
    raf = requestAnimationFrame(run);
    setTimeout(run, 200);
  }

  function draw() {
    host.querySelectorAll("svg").forEach((s) => s.remove());
    const W = Math.max(host.clientWidth, 280), H = height;
    const m = { l: 46, r: 12, t: 8, b: 26 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
    const last = data.length ? data[data.length - 1].t : 0;
    const x1 = total ?? Math.max(last, 1), x0 = total !== null ? 0 : x1 - windowS;       // listening: the newest reading sits at the right edge
    const sx = (t) => m.l + iw * (t - x0) / (x1 - x0), Y0 = -90, Y1 = 0, sy = (v) => m.t + ih * (1 - (Math.max(v, Y0) - Y0) / (Y1 - Y0));
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, "aria-hidden": "true" });
    host.insertBefore(svg, tip);
    for (const z of zones) {
      const top = sy(Math.min(z.hi, 0)), bot = sy(Math.max(z.lo, Y0));
      const col = { good: "var(--good)", ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)" }[z.tone];
      svgEl("rect", { x: m.l, y: top, width: iw, height: Math.max(bot - top, 0), fill: col, opacity: z.id === "target" ? 0.2 : 0.07 }, svg);
      if (z.id === "target") svgEl("text", { class: "note", x: m.l + 6, y: top + 14 }, svg).textContent = `Target range ${MINUS}20 to ${MINUS}6 dBFS`;
    }
    for (const v of [-80, -60, -40, -20, 0]) {
      svgEl("line", { x1: m.l, x2: m.l + iw, y1: sy(v), y2: sy(v), stroke: "var(--line)" }, svg);
      svgEl("text", { x: m.l - 8, y: sy(v) + 4, "text-anchor": "end" }, svg).textContent = v === 0 ? "0" : MINUS + Math.abs(v);
    }
    svgEl("text", { class: "axt", transform: `translate(12 ${m.t + ih / 2}) rotate(-90)`, "text-anchor": "middle" }, svg).textContent = "dBFS";
    const step = (x1 - x0) > 60 ? 10 : (x1 - x0) > 20 ? 5 : 2;
    for (let t = Math.ceil(x0 / step) * step; t <= x1 + 1e-9; t += step) {
      svgEl("line", { x1: sx(t), x2: sx(t), y1: m.t + ih, y2: m.t + ih + 4, stroke: "var(--line2)" }, svg);
      svgEl("text", { x: sx(t), y: m.t + ih + 17, "text-anchor": "middle" }, svg).textContent = total !== null ? Math.round(t) + " s" : (t - x1 === 0 ? "now" : "−" + Math.round(x1 - t) + " s");
    }
    const vis = data.filter((d) => d.t >= x0 - 0.5);
    const line = (key, stroke, w, op) => {
      if (vis.length < 2) return;
      svgEl("path", { d: vis.map((d, i) => (i ? "L" : "M") + sx(d.t).toFixed(1) + " " + sy(d[key]).toFixed(1)).join(""), fill: "none", stroke, "stroke-width": w, "stroke-linejoin": "round", opacity: op }, svg);
    };
    line("rms", "var(--muted)", 1.5, 0.9);
    line("peak", "var(--accent)", 2, 1);
    for (const mk of marks) {
      if (mk.t < x0 || mk.t > x1) continue;
      svgEl("line", { x1: sx(mk.t), x2: sx(mk.t), y1: m.t, y2: m.t + ih, stroke: "var(--ink2)", "stroke-dasharray": "3 4" }, svg);
      svgEl("text", { class: "note", x: sx(mk.t) + 5, y: m.t + ih - 6 }, svg).textContent = mk.label;
    }
    // legend
    svgEl("line", { x1: m.l + iw - 150, x2: m.l + iw - 130, y1: m.t + 12, y2: m.t + 12, stroke: "var(--accent)", "stroke-width": 2 }, svg);
    svgEl("text", { class: "note", x: m.l + iw - 126, y: m.t + 16 }, svg).textContent = "peak";
    svgEl("line", { x1: m.l + iw - 84, x2: m.l + iw - 64, y1: m.t + 12, y2: m.t + 12, stroke: "var(--muted)", "stroke-width": 1.5 }, svg);
    svgEl("text", { class: "note", x: m.l + iw - 60, y: m.t + 16 }, svg).textContent = "average";
  }
  const ro = new ResizeObserver(() => draw());
  ro.observe(host);
  draw();
  api.destroy = () => { ro.disconnect(); if (raf) cancelAnimationFrame(raf); };
  return api;
}
