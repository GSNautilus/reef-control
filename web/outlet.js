"use strict";
// Outlet detail page (#/outlet/N): state, controls, power chart, normal draw, name.

const outletPage = { pos: null, hours: 24, history: null, loadedAt: 0, error: null };
const RANGES = [[6, "6 h"], [24, "24 h"], [168, "7 days"], [720, "30 days"]];

async function loadOutletHistory() {
  if (outletPage.pos == null) return;
  try {
    outletPage.history = await api("GET", `/api/strip/outlet/${outletPage.pos}/history?hours=${outletPage.hours}`);
    outletPage.error = null;
  } catch (e) {
    outletPage.error = e.message;
  }
  outletPage.loadedAt = Date.now();
  renderOutletChart();
}

function renderOutletChart() {
  const box = $("#outlet-chart");
  const h = outletPage.history;
  if (outletPage.error) { box.replaceChildren(el("p", { class: "error" }, outletPage.error)); return; }
  if (!h) { box.replaceChildren(el("p", { class: "muted" }, "Loading…")); return; }
  const long = outletPage.hours > 48;
  const xFormat = long
    ? (t) => new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" })
    : (t) => clockTime(t);
  box.replaceChildren(
    lineChart(h.points.map(([t, w]) => [t, w]), {
      unit: "W", refLine: h.normal, refLabel: h.normal ? `normal ${fmtWatts(h.normal)} W` : "",
      xFormat, gapSeconds: long ? 1800 : 300, label: `${h.name} power`,
    }),
    el("p", { class: "hint" }, long ? "10-minute averages" : "1-minute averages",
      " while the dashboard was running; gaps are times the PC was off. ",
      "Power history from the strip's own memory comes with the Power page."));
}

function renderOutletPage() {
  if ($("#page-outlet").hidden) return;
  const o = stripOutlet(outletPage.pos);
  const head = $("#outlet-head");
  if (!strip.data) { head.replaceChildren(el("p", { class: "muted" }, "Loading…")); return; }
  if (!o) { head.replaceChildren(el("p", { class: "error" }, `There's no outlet ${outletPage.pos}.`)); return; }
  const [cls, label] = outletPill(o);
  const h = o.health || {};
  const focusInside = head.contains(document.activeElement) && ["SELECT", "INPUT"].includes(document.activeElement.tagName);
  if (!focusInside) {
    head.replaceChildren(
      el("div", { class: "section-head" },
        el("h2", {}, o.name),
        el("span", { class: "muted" }, `${ROLE_NAMES[o.role]} · outlet ${o.position}`)),
      el("div", { class: "detail-grid" },
        el("div", { class: "panel" },
          el("div", { class: "tile-power" },
            el("span", { class: "big-watts" }, fmtWatts(o.watts)), el("span", { class: "muted" }, " W"),
            el("span", { class: `pill ${cls}` }, label)),
          ...outletLines(o),
          el("div", { class: "tile-action" }, outletAction(o))),
        el("div", { class: "panel" },
          el("dl", { class: "facts" },
            el("dt", {}, "Normal draw"), el("dd", {}, h.normal ? `${fmtWatts(h.normal)} W` : "Still learning (about 30 min of running)"),
            el("dt", {}, "Health"), el("dd", {}, h.text || "–"),
            el("dt", {}, "Energy today"), el("dd", {}, o.today_wh != null ? `${o.today_wh < 10 ? o.today_wh.toFixed(2) : Math.round(o.today_wh)} Wh` : "–")),
          o.role !== "ato" ? el("div", { class: "row" },
            el("button", { class: "ghost",
              onclick: () => stripCall("POST", `/api/strip/outlet/${o.position}/relearn`) }, "Relearn normal draw"),
            el("span", { class: "muted small" }, "Use after changing a pump's speed.")) : null)));
  }
  const name = $("#outlet-name");
  if (document.activeElement !== name) name.value = o.name;
}

function renderOutletRanges() {
  $("#outlet-ranges").replaceChildren(...RANGES.map(([hours, label]) =>
    el("button", { class: hours === outletPage.hours ? "on" : "",
      onclick: () => { outletPage.hours = hours; renderOutletRanges(); outletPage.history = null; renderOutletChart(); loadOutletHistory(); } },
      label)));
}

$("#outlet-name").addEventListener("change", (e) => {
  if (outletPage.pos != null) stripCall("PUT", `/api/strip/outlet/${outletPage.pos}/name`, { name: e.target.value });
});

bus.on("route", (r) => {
  if (r.page !== "outlet") return;
  if (outletPage.pos !== Number(r.arg)) {
    outletPage.pos = Number(r.arg);
    outletPage.history = null;
  }
  renderOutletRanges();
  renderOutletPage();
  renderOutletChart();
  loadOutletHistory();
});
bus.on("strip", renderOutletPage);
setInterval(() => { if (!$("#page-outlet").hidden) loadOutletHistory(); }, 60000);
