"use strict";
// ATO detail page (#/ato): status, today's runs, runs and pump time per day, thresholds.

const atoPage = { days: 14, history: null, error: null };

async function loadAtoHistory() {
  try {
    atoPage.history = await api("GET", `/api/strip/ato/history?days=${atoPage.days}`);
    atoPage.error = null;
  } catch (e) {
    atoPage.error = e.message;
  }
  renderAtoHistory();
}

function atoOutlet() {
  return strip.data?.outlets.find((o) => o.role === "ato");
}

function renderAtoStatus() {
  if ($("#page-ato").hidden) return;
  const box = $("#ato-status");
  const o = atoOutlet();
  const a = strip.data?.ato;
  if (!o || !a) { box.replaceChildren(el("p", { class: "muted" }, strip.data ? "No ATO outlet configured." : "Loading…")); return; }
  if (box.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  const [cls, label] = outletPill(o);
  const stat = (k, v) => el("div", { class: "stat" }, el("div", { class: "muted small" }, k), el("div", { class: "stat-value" }, v));
  box.replaceChildren(
    el("div", { class: "detail-grid" },
      el("div", { class: "panel" },
        el("div", { class: "tile-power" },
          el("span", { class: "big-watts" }, fmtWatts(o.watts)), el("span", { class: "muted" }, " W"),
          el("span", { class: `pill ${cls}` }, label)),
        ...outletLines(o),
        el("div", { class: "tile-action" }, outletAction(o))),
      el("div", { class: "panel" },
        el("div", { class: "stats" },
          stat("Runs today", a.count),
          stat("Usual per day", a.usual == null ? "learning" : a.usual),
          stat("Longest today", a.longest ? mmss(a.longest) : "–"),
          stat("Pump time today", a.pump_seconds ? mmss(a.pump_seconds) : "–")),
        a.strip_wh_today != null ? el("p", { class: "hint" },
          `The strip's own meter shows ${a.strip_wh_today} Wh on this outlet today, including time the PC was off.`) : null)),
    a.warnings.length ? el("div", { class: "warnings" }, ...a.warnings.map((w) => el("div", {}, w))) : null);
}

function renderAtoHistory() {
  const h = atoPage.history;
  const charts = $("#ato-charts");
  const list = $("#ato-runs");
  if (atoPage.error) { charts.replaceChildren(el("p", { class: "error" }, atoPage.error)); return; }
  if (!h) { charts.replaceChildren(el("p", { class: "muted" }, "Loading…")); return; }
  const short = (iso) => {
    const d = new Date(`${iso}T12:00:00`);
    return `${d.toLocaleDateString([], { weekday: "short" })} ${d.getDate()}`;
  };
  const items = (key, fmt) => h.days.map((d) => ({
    label: short(d.date), value: d[key], muted: d.watched_hours < 20,
    title: `${dayLabel(d.date)}: ${fmt(d[key])} (dashboard watched ${d.watched_hours} h)`,
  }));
  charts.replaceChildren(
    el("h3", {}, "Runs per day"),
    barChart(items("runs", (v) => `${v} runs`), { label: "ATO runs per day" }),
    el("h3", {}, "Pump time per day"),
    barChart(items("seconds", (v) => mmss(v)), { label: "ATO pump time per day", kind: "seconds", yFormat: (v) => mmss(v) }),
    el("p", { class: "hint" }, "Faded bars are days the dashboard watched for less than 20 hours, so they undercount. ",
      "More runs or pump time than usual means more evaporation, or a leak or stuck float."));
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const runs = h.runs.filter((r) => r.started * 1000 >= today.getTime()).reverse();
  list.replaceChildren(...(runs.length ? runs.map((r) =>
    el("li", {}, el("span", { class: "muted" }, `${clockTime(r.started)} `), `${mmss(r.seconds)}`,
      r.peak_watts != null ? el("span", { class: "muted" }, ` · ${fmtWatts(r.peak_watts)} W`) : null))
    : [el("li", { class: "muted" }, "No runs logged yet today.")]));
}

const ATO_SETTINGS = [
  ["ato_long_run_seconds", "Warn if one run is longer than (s)", 5],
  ["ato_runs_factor", "Warn at this many times the usual runs per day", 0.1],
];

function renderAtoSettings() {
  const box = $("#ato-settings");
  if (!strip.data || box.childElementCount) return;
  box.replaceChildren(...ATO_SETTINGS.map(([key, label, step]) => {
    const input = el("input", { type: "number", step, value: strip.data.settings[key] });
    input.addEventListener("change", () => stripCall("PUT", "/api/strip/settings", { [key]: Number(input.value) }));
    return el("label", {}, el("span", {}, label), input);
  }));
}

bus.on("route", (r) => {
  if (r.page !== "ato") return;
  renderAtoStatus();
  renderAtoSettings();
  renderAtoHistory();
  loadAtoHistory();
});
bus.on("strip", () => { renderAtoStatus(); renderAtoSettings(); });
setInterval(() => { if (!$("#page-ato").hidden) loadAtoHistory(); }, 60000);
