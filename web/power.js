"use strict";
// Power page (#/power): energy per device over a period, and trends.

const SPANS = [["day", "Today"], ["week", "7 days"], ["month", "30 days"], ["year", "12 months"]];
const DEVICE_COLORS = ["var(--d1)", "var(--d2)", "var(--d3)", "var(--d4)", "var(--d5)", "var(--d6)", "var(--d7)", "var(--d8)"];

const powerPage = { span: "week", summary: null, trends: null, error: null };

function energyText(wh) {
  if (wh == null) return "–";
  if (wh >= 1000) return `${(wh / 1000).toFixed(wh >= 10000 ? 1 : 2)} kWh`;
  if (wh >= 10) return `${Math.round(wh)} Wh`;
  if (wh >= 1) return `${wh.toFixed(1)} Wh`;
  return wh > 0 ? `${wh.toFixed(2)} Wh` : "0 Wh";
}

function deviceSeries(devices) {
  return devices.map((d, i) => ({ id: d.id, name: d.name, color: DEVICE_COLORS[i % DEVICE_COLORS.length] }));
}

async function loadPower() {
  try {
    const [summary, trends] = await Promise.all([
      api("GET", `/api/energy/summary?span=${powerPage.span}`),
      powerPage.trends ? Promise.resolve(powerPage.trends) : api("GET", "/api/energy/trends"),
    ]);
    powerPage.summary = summary;
    powerPage.trends = trends;
    powerPage.error = null;
  } catch (e) {
    powerPage.error = e.message;
  }
  renderPower();
}

function renderSpans() {
  $("#power-spans").replaceChildren(...SPANS.map(([span, label]) =>
    el("button", { class: span === powerPage.span ? "on" : "",
      onclick: () => { powerPage.span = span; powerPage.summary = null; renderSpans(); renderPower(); loadPower(); } },
      label)));
}

function renderTotals(s) {
  const series = deviceSeries(s.devices);
  const max = Math.max(1, ...s.devices.map((d) => d.wh));
  const rows = s.devices.map((d, i) => el("div", { class: "energy-row" },
    el("span", { class: "swatch", style: `background:${series[i].color}` }),
    el("a", { href: d.kind === "light" ? "#/lights" : d.role === "ato" ? "#/ato" : `#/outlet/${d.id.split(":")[1]}`,
      class: "energy-name" }, d.name, d.measured ? "" : el("span", { class: "muted small" }, " (estimate)")),
    el("span", { class: "energy-bar" }, el("span", { style: `width:${(d.wh / max) * 100}%;background:${series[i].color}` })),
    el("span", { class: "energy-wh" }, energyText(d.wh)),
    el("span", { class: "muted small energy-avg" }, `${fmtWatts(d.avg_watts)} W avg · ${d.share}%`)));
  return el("div", { class: "panel" },
    el("div", { class: "energy-total" },
      el("span", { class: "big-watts" }, energyText(s.total_wh)),
      el("span", { class: "muted" }, ` · ${fmtWatts(s.total_wh / Math.max(s.hours, 1 / 60))} W average`)),
    ...rows,
    el("p", { class: "hint" },
      `Not included: ${s.not_included.join("; ")}. Outlets combine this PC's readings (to a fraction of a Wh, `,
      `updated as it runs) with the strip's own meter (whole Wh, covering times this PC was off). The Prime HD is `,
      `estimated from its color levels, and from its schedule for times the `,
      `dashboard wasn't running; energy tracking started ${new Date(s.since * 1000).toLocaleDateString()}.`));
}

function renderBreakdown(s) {
  const series = deviceSeries(s.devices);
  return el("div", { class: "panel" },
    stackedBarChart(s.buckets, series, { yFormat: (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v < 10 ? String(Math.round(v * 10) / 10) : String(Math.round(v))),
      label: "Energy per period by device" }),
    el("div", { class: "legend" }, ...series.map((x) =>
      el("span", {}, el("span", { class: "swatch", style: `background:${x.color}` }), x.name))),
    el("p", { class: "hint" }, `Wh per ${{ H: "hour", D: "day", M: "month" }[s.period]}. Hover a bar for details.`));
}

function weekLabel(iso) {
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function renderTrends(t) {
  const pumps = t.pumps.map((p) => {
    const known = p.weekly.filter((w) => w != null).length;
    return el("article", { class: `tile${p.flag ? " warn" : ""}` },
      el("div", { class: "tile-head" },
        el("a", { class: "tile-name", href: `#/outlet/${p.position}` }, p.name),
        el("span", { class: "muted small" }, "Weekly draw")),
      barChart(t.weeks.map((w, i) => ({ label: weekLabel(w), value: p.weekly[i] || 0, muted: p.weekly[i] == null,
        title: `Week of ${weekLabel(w)}: ${p.weekly[i] == null ? "not enough readings" : `${p.weekly[i]} W`}` })),
        { kind: "watts", width: 260, height: 150, className: "small-chart", label: `${p.name} weekly draw`,
          emptyText: "No full days of readings yet." }),
      el("div", { class: `small ${p.flag ? "warn-text" : "muted"}` },
        p.flag || (known >= 3 ? "Steady" : `Needs 3 weeks of readings (has ${known})`)));
  });
  const ev = t.evaporation;
  const evItems = (key, fmt) => ev.map((w) => ({ label: weekLabel(w.week), value: w[key] || 0, muted: w[key] == null,
    title: `Week of ${weekLabel(w.week)}: ${w[key] == null ? "not watched enough" : fmt(w[key])} (${w.days} watched days)` }));
  const sends = new Set(t.schedule_sends);
  // Same colors as in the totals above.
  const colorOf = Object.fromEntries(deviceSeries(powerPage.summary?.devices || []).map((x) => [x.id, x.color]));
  const lightSeries = t.lights.map((l, i) => ({ id: l.id, name: l.name, color: colorOf[l.id] || DEVICE_COLORS[i] }));
  const lightBuckets = t.light_days.map((d) => {
    const est = Object.entries(d.estimated_hours).filter(([, h]) => h)
      .map(([id, h]) => `${t.lights.find((l) => l.id === id)?.name} ${h} h from its schedule`);
    return {
      label: `${new Date(`${d.day}T12:00:00`).getDate()}${sends.has(d.day) ? "*" : ""}`,
      values: Object.fromEntries(Object.entries(d.values).map(([id, v]) => [id, v || 0])),
      note: est.join(", "),
    };
  });
  const m = t.hd16_model;
  const fit = m ? (m.learned
    ? `The 16HD's estimate for times the dashboard wasn't running is fitted to its own readings: ${m.idle} W idle + ${Math.round(m.ratio * 100)}% of its rated figures.`
    : "The 16HD's estimate for times the dashboard wasn't running uses a starting guess until it has a week of readings.") : "";
  return [
    el("h3", {}, "Pump draw"),
    el("p", { class: "hint" }, "Weekly average while on. A slow drift often means a clogging intake or a wearing impeller; ",
      "a change of 15% or more from the previous weeks is flagged. Faded bars had too few readings."),
    el("div", { class: "tiles" }, ...pumps),
    el("h3", {}, "Evaporation (ATO)"),
    el("div", { class: "panel" },
      el("div", { class: "small muted" }, "Runs per day, weekly average"),
      barChart(evItems("runs_per_day", (v) => `${v} runs/day`), { height: 140, label: "ATO runs per day by week",
        emptyText: "Not enough watched days yet: a day counts once the dashboard has watched it for 20 hours." }),
      el("div", { class: "small muted" }, "Pump time per day, weekly average"),
      barChart(evItems("seconds_per_day", (v) => mmss(v)), { kind: "seconds", height: 140, yFormat: (v) => mmss(v),
        label: "ATO pump time per day by week", emptyText: "Not enough watched days yet." }),
      el("p", { class: "hint" }, "Only days the dashboard watched for 20 hours or more count. ",
        el("a", { href: "#/ato" }, "ATO details ›"))),
    el("h3", {}, "Lights, energy per day"),
    el("div", { class: "panel" },
      stackedBarChart(lightBuckets, lightSeries, { height: 180, label: "Lights energy per day",
        yFormat: (v) => (v < 10 ? String(Math.round(v * 10) / 10) : String(Math.round(v))) }),
      el("div", { class: "legend" }, ...lightSeries.map((x) =>
        el("span", {}, el("span", { class: "swatch", style: `background:${x.color}` }), x.name))),
      el("p", { class: "hint" }, "Wh per day, last 30 days; * = a schedule was sent that day. The Prime HD is ",
        "estimated from its color levels; the 16HD is measured by the light. Hours the dashboard wasn't running ",
        "come from each light's schedule. ", fit)),
  ];
}

function renderPower() {
  if ($("#page-power").hidden) return;
  const box = $("#power-body");
  if (powerPage.error) { box.replaceChildren(el("p", { class: "error" }, powerPage.error)); return; }
  const s = powerPage.summary, t = powerPage.trends;
  if (!s) { box.replaceChildren(el("p", { class: "muted" }, "Loading…")); return; }
  box.replaceChildren(renderTotals(s), renderBreakdown(s), ...(t ? renderTrends(t) : []));
}

bus.on("route", (r) => {
  if (r.page !== "power") return;
  powerPage.trends = null;  // refresh trends each visit
  renderSpans();
  renderPower();
  loadPower();
});
setInterval(() => { if (!$("#page-power").hidden) loadPower(); }, 300000);
