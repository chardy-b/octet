// The level meter: a bar with the ranges a recording should land in, a peak hold, and plain-language advice.
import { MINUS, fmt, h } from "./util.js";

const SCALE_LO = -70;
const pct = (db) => `${Math.max(0, Math.min(100, ((db - SCALE_LO) / (0 - SCALE_LO)) * 100))}%`;

export function zoneOf(zones, db) { return zones.find((z) => db >= z.lo && db < z.hi) || zones[zones.length - 1]; }

// The same wording the server uses for a finished recording, so live guidance and the final result agree.
export function adviceFor(zones, centre, peak) {
  const z = zoneOf(zones, peak), change = Math.round(centre - peak);
  let message;
  if (z.id === "target") message = "In the target range. Leave the gain and the volume where they are.";
  else if (["too_low", "low", "ok"].includes(z.id)) message = `Raise the mic gain by about ${change} dB, or turn the speaker up a little. More signal means a cleaner measurement.`;
  else if (z.id === "hot") message = `Close to clipping. Lower the mic gain by about ${-change} dB.`;
  else message = `This will clip. Lower the mic gain by about ${-change} dB before recording.`;
  return { zone: z.id, label: z.label, tone: z.tone, change_db: change, message };
}

export function adviceBox(a, extra) {
  const icon = { good: "✓", ok: "•", warn: "!", bad: "!" }[a.tone] || "•";
  return h("div", { class: "advice " + a.tone, role: "status" },
    h("span", { class: "chip " + a.tone, text: icon }),
    h("div", {}, h("b", { text: a.label + ". " }), a.message, extra || null));
}

export function LevelMeter({ host, zones, centre }) {
  const zoneEls = zones.map((z) => h("div", { class: "zone " + z.tone, style: { left: pct(Math.max(z.lo, SCALE_LO)), width: `calc(${pct(Math.min(z.hi, 0))} - ${pct(Math.max(z.lo, SCALE_LO))})` } }));
  const fill = h("div", { class: "bar-fill", style: { width: "0%" } });
  const rms = h("div", { class: "bar-rms", style: { width: "0%" } });
  const holdEl = h("div", { class: "bar-hold", style: { left: "0%", display: "none" } });
  const plain = h("div", { class: "bar-plain", style: { left: "0%", display: "none" }, title: "Plain peak, including low-frequency rumble" });
  const read = h("div", { class: "meter-read", html: `<span>${MINUS}∞</span><small>dBFS</small>` });
  const chip = h("span", { class: "chip", text: "Waiting" });
  const label = h("div", { class: "zone-labels" });
  for (const z of zones) {
    const mid = (Math.max(z.lo, SCALE_LO) + Math.min(z.hi, 0)) / 2;
    if (Math.min(z.hi, 0) - Math.max(z.lo, SCALE_LO) < 5 && z.hi < 0) continue;       // too narrow to name; its colour and the chip say it
    label.append(h("span", { class: z.hi >= 0 ? "last" : "", style: { left: pct(mid) }, text: z.label }));
  }
  for (const e of [-60, -40, -30, -20, -6, -2]) label.append(h("span", { class: "edge", style: { left: pct(e) }, text: MINUS + Math.abs(e) }));
  host.classList.add("meter");
  host.append(
    h("div", { class: "meter-head" }, read, chip),
    h("div", { class: "bar-wrap", role: "meter", "aria-label": "Input level in dBFS", "aria-valuemin": SCALE_LO, "aria-valuemax": 0 }, zoneEls, fill, rms, plain, holdEl),
    label,
  );
  let holdDb = -120, holdAt = 0;

  return {
    update(m) {
      const now = performance.now();
      const peak = m.band_peak;
      if (peak >= holdDb || now - holdAt > 1800) { holdDb = peak; holdAt = now; }
      const z = zoneOf(zones, peak), col = { good: "var(--good)", ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)" }[z.tone];
      fill.style.width = pct(peak); fill.style.background = peak <= SCALE_LO ? "var(--muted)" : col;
      rms.style.width = pct(m.band_rms);
      holdEl.style.display = "block"; holdEl.style.left = pct(holdDb);
      plain.style.display = "block"; plain.style.left = pct(m.peak);
      read.innerHTML = `<span>${fmt(peak, 1)}</span><small>dBFS above 60 Hz</small>`;
      chip.className = "chip " + z.tone; chip.textContent = z.label;
      host.querySelector(".bar-wrap").setAttribute("aria-valuenow", Math.round(peak));
    },
    reset() {
      fill.style.width = "0%"; rms.style.width = "0%"; holdEl.style.display = "none"; plain.style.display = "none";
      read.innerHTML = `<span>${MINUS}∞</span><small>dBFS</small>`; chip.className = "chip"; chip.textContent = "Waiting";
      holdDb = -120;
    },
  };
}
