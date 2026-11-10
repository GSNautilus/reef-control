"use strict";
// Shared helpers for every page. Loaded first; the page scripts use these globals.

const $ = (sel) => document.querySelector(sel);

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 404 && data.detail === "Not Found") {
    // The page files are newer than the running server (it reads them from disk).
    throw new Error("The dashboard server is older than this page. Close the dashboard window and start it again.");
  }
  if (!resp.ok) throw new Error(data.detail || `${method} ${path} failed (${resp.status})`);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) node.setAttribute(k, v);
  for (const child of children.flat()) if (child != null) node.append(child instanceof Node ? child : String(child));
  return node;
}

function hhmm(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function mmss(seconds) {
  seconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = String(seconds % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function fmtWatts(w) {
  if (w == null) return "–";
  return w < 10 ? w.toFixed(1) : String(Math.round(w));
}

function clockTime(epoch) {
  return new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function dayLabel(isoDate) {
  const d = new Date(`${isoDate}T12:00:00`);
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

// A tiny event bus so pages can redraw when shared data changes.
const bus = {
  handlers: {},
  on(name, fn) { (this.handlers[name] ||= []).push(fn); },
  emit(name, data) { for (const fn of this.handlers[name] || []) fn(data); },
};

// ---- charts (plain SVG, nothing loaded from the internet) -------------------------------

const TICK_STEPS = {
  count: [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000],
  watts: [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000],
  seconds: [10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600],
};

/** Round axis: [top, step] with about `ticks` steps from 0 to above `max`. */
function niceAxis(max, kind = "count", ticks = 4) {
  const steps = TICK_STEPS[kind] || TICK_STEPS.count;
  const raw = Math.max(max, 1e-9) / ticks;
  let step = steps.find((s) => s >= raw);
  if (step == null) step = steps[steps.length - 1] * Math.ceil(raw / steps[steps.length - 1]);
  const top = Math.max(step, Math.ceil((max * 1.02) / step) * step);
  return [top, step];
}

/** Line chart of [t (epoch s), value] points; null values leave a gap.
 *  opts: {height, unit, refLine, refLabel, xFormat} */
function lineChart(points, opts = {}) {
  const W = 800, H = opts.height || 220, L = 44, R = 10, T = 10, B = 26;
  const vals = points.map((p) => p[1]).filter((v) => v != null);
  if (!vals.length) return el("p", { class: "muted" }, "No readings for this period yet.");
  const t0 = points[0][0], t1 = points[points.length - 1][0] || t0 + 1;
  const [vmax, vstep] = niceAxis(Math.max(...vals, opts.refLine || 0, 0.5), "watts");
  const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (W - L - R);
  const y = (v) => T + (1 - v / vmax) * (H - T - B);
  const gapLimit = (opts.gapSeconds || 300);
  let d = "", prevT = null;
  for (const [t, v] of points) {
    if (v == null) { prevT = null; continue; }
    d += `${prevT == null || t - prevT > gapLimit ? "M" : "L"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
    prevT = t;
  }
  const grid = [], labels = [];
  for (let v = 0; v <= vmax + 1e-9; v += vstep) {
    grid.push(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }));
    labels.push(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "axis-label" },
      vstep < 1 ? v.toFixed(1) : String(Math.round(v))));
  }
  const xFormat = opts.xFormat || ((t) => clockTime(t));
  for (let i = 0; i <= 4; i++) {
    const t = t0 + ((t1 - t0) / 4) * i;
    labels.push(svg("text", { x: x(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle",
      class: "axis-label" }, xFormat(t)));
  }
  const ref = opts.refLine ? [
    svg("line", { x1: L, x2: W - R, y1: y(opts.refLine), y2: y(opts.refLine), class: "ref-line" }),
    svg("text", { x: W - R, y: y(opts.refLine) - 4, "text-anchor": "end", class: "axis-label" }, opts.refLabel || ""),
  ] : [];
  return svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": opts.label || "chart" },
    ...grid, ...ref, svg("path", { d, class: "data-line" }), ...labels,
    svg("text", { x: 4, y: T + 8, class: "axis-label" }, opts.unit || ""));
}

/** Stacked bars. buckets: [{label, values: {id: number}}]; series: [{id, name, color}].
 *  opts.yFormat formats axis values; opts.title(bucket) gives the hover text. */
function stackedBarChart(buckets, series, opts = {}) {
  const W = 800, H = opts.height || 240, L = 52, R = 10, T = 10, B = 26;
  const totals = buckets.map((b) => series.reduce((s, x) => s + (b.values[x.id] || 0), 0));
  if (!totals.some((t) => t > 0)) return el("p", { class: "muted" }, "No energy recorded for this period yet.");
  const [vmax, vstep] = niceAxis(Math.max(...totals), opts.kind || "watts", 4);
  const bw = (W - L - R) / buckets.length;
  const y = (v) => T + (1 - v / vmax) * (H - T - B);
  const parts = [];
  for (let v = 0; v <= vmax + 1e-9; v += vstep) {
    parts.push(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }));
    parts.push(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "axis-label" },
      opts.yFormat ? opts.yFormat(v) : String(Math.round(v))));
  }
  buckets.forEach((b, i) => {
    let acc = 0;
    const x = L + i * bw + bw * 0.15;
    for (const s of series) {
      const v = b.values[s.id] || 0;
      if (v <= 0) continue;
      parts.push(svg("rect", { x, y: y(acc + v), width: bw * 0.7, height: y(acc) - y(acc + v), fill: s.color },
        svg("title", {}, `${b.label} · ${s.name}: ${opts.yFormat ? opts.yFormat(v) : v}`)));
      acc += v;
    }
    if (buckets.length <= 16 || i % Math.ceil(buckets.length / 10) === 0) {
      parts.push(svg("text", { x: L + i * bw + bw / 2, y: H - 6, "text-anchor": "middle", class: "axis-label" }, b.label));
    }
  });
  return svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": opts.label || "chart" }, ...parts);
}


/** Bar chart: items [{label, value, title, muted}]. opts.kind: "count" (default), "seconds" or "watts";
 *  opts.width for narrow spots (default 800); opts.emptyText shown instead when every value is 0. */
function barChart(items, opts = {}) {
  const W = opts.width || 800, H = opts.height || 180, L = 44, R = 10, T = 10, B = 26;
  if (!items.length || (opts.emptyText && !items.some((i) => (i.value || 0) > 0))) {
    return el("p", { class: "muted small empty-chart" }, opts.emptyText || "Nothing to show yet.");
  }
  const [vmax, vstep] = niceAxis(Math.max(...items.map((i) => i.value || 0), 1), opts.kind || "count", 3);
  const bw = (W - L - R) / items.length;
  const y = (v) => T + (1 - v / vmax) * (H - T - B);
  const parts = [];
  for (let v = 0; v <= vmax + 1e-9; v += vstep) {
    parts.push(svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid-line" }));
    parts.push(svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "axis-label" },
      opts.yFormat ? opts.yFormat(v) : vstep < 1 ? v.toFixed(1) : String(Math.round(v))));
  }
  items.forEach((it, i) => {
    const h = Math.max(0, y(0) - y(it.value || 0));
    parts.push(svg("rect", { x: L + i * bw + bw * 0.15, y: y(it.value || 0), width: bw * 0.7, height: h,
      class: it.muted ? "bar muted-bar" : "bar" }, svg("title", {}, it.title || `${it.label}: ${it.value}`)));
    const maxLabels = Math.max(3, Math.floor(W / 70));
    if (items.length <= maxLabels || i % Math.ceil(items.length / maxLabels) === 0) {
      parts.push(svg("text", { x: L + i * bw + bw / 2, y: H - 6, "text-anchor": "middle", class: "axis-label" }, it.label));
    }
  });
  return svg("svg", { viewBox: `0 0 ${W} ${H}`, class: `chart-svg ${opts.className || ""}`, role: "img",
    "aria-label": opts.label || "chart" }, ...parts);
}
