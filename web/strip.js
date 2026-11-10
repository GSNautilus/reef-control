"use strict";
// Power strip data shared by the Control, Outlet and ATO pages: polling,
// actions, countdowns and the outlet tile. Uses helpers from common.js.

const OFF_CHOICES = [5, 10, 15, 30, 60, 120, 240, 480];
const ROLE_NAMES = { return: "Return", circulation: "Circulation", skimmer: "Skimmer", ato: "ATO" };

const strip = { data: null, fetchedAt: 0, busy: false, error: null };

function stripOutlet(pos) {
  return strip.data?.outlets.find((o) => o.position === Number(pos));
}

function duration(minutes) {
  return minutes < 60 ? `${minutes} min` : `${minutes / 60} h`;
}

// ---- countdowns that tick between polls ---------------------------------------------

function left(seconds) {
  if (seconds == null) return null;
  return Math.max(0, seconds - (Date.now() - strip.fetchedAt) / 1000);
}

function countdown(seconds) {
  // Updated in place every second, so open menus aren't redrawn.
  return el("span", { "data-left": seconds }, mmss(left(seconds)));
}

function tickCountdowns() {
  for (const span of document.querySelectorAll("[data-left]")) {
    span.textContent = mmss(left(Number(span.dataset.left)));
  }
}

// ---- talking to the server --------------------------------------------------------------

async function stripCall(method, path, body, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  strip.busy = true;
  bus.emit("strip");
  try {
    strip.data = await api(method, path, body);
    strip.fetchedAt = Date.now();
    strip.error = null;
  } catch (e) {
    strip.error = e.message;
  } finally {
    strip.busy = false;
    bus.emit("strip");
  }
}

async function loadStrip() {
  if (strip.busy) return;
  try {
    strip.data = await api("GET", "/api/strip");
    strip.fetchedAt = Date.now();
    if (strip.error?.startsWith("Couldn't reach")) strip.error = null;
  } catch (e) {
    strip.error = `Couldn't reach the dashboard: ${e.message}`;
  }
  bus.emit("strip");
}

// ---- shared pieces ------------------------------------------------------------------------

function outletAction(o) {
  const d = strip.data;
  const busy = strip.busy || !d?.online;
  const path = `/api/strip/outlet/${o.position}`;
  if (!o.on) {
    return el("button", { disabled: busy, onclick: () => stripCall("POST", path, { action: "on" }) }, "Turn on");
  }
  if (d.mode) return el("span", { class: "muted small" }, `${d.mode.name === "feed" ? "Feed" : "Maintenance"} mode is running`);
  const holdable = o.role === "skimmer" || o.role === "ato";
  const select = el("select", { disabled: busy, "aria-label": `Turn ${o.name} off` },
    el("option", { value: "" }, "Off for…"),
    ...OFF_CHOICES.map((m) => el("option", { value: m }, duration(m))),
    holdable ? el("option", { value: "hold" }, "Until I turn it on") : null);
  select.addEventListener("change", () => {
    const v = select.value;
    if (!v) return;
    // Let go of focus: pages skip redrawing tiles while a menu is focused (so an
    // open menu isn't yanked away), and a kept focus stopped the tile from ever
    // updating to show "Turn on".
    select.blur();
    if (v === "hold") stripCall("POST", path, { action: "off" });
    else stripCall("POST", path, { action: "off_for", minutes: Number(v) });
  });
  return select;
}

function outletPill(o) {
  const h = o.health || {};
  const warn = o.unexpected_off || h.state === "warn" || h.state === "bad";
  if (o.on == null) return ["off", "?", warn];
  if (warn) return ["warn", o.on ? "Check" : "Off", warn];
  if (o.on) return ["on", "On", warn];
  if (o.held_off) return ["off", "Held off", warn];
  if (o.timer_left != null) return ["timer", "Off", warn];
  return ["off", "Off", warn];
}

function outletLines(o) {
  const h = o.health || {};
  const lines = [];
  if (o.timer_left != null) lines.push(el("div", { class: "small" }, "Back on in ", countdown(o.timer_left)));
  if (o.restart_in != null) lines.push(el("div", { class: "small warn-text" },
    "Off after a power cut, back on in ", countdown(o.restart_in)));
  if (o.held_off) lines.push(el("div", { class: "small" }, "Off until you turn it on"));
  if (o.unexpected_off) lines.push(el("div", { class: "small warn-text" }, "Off, but not by the dashboard"));
  const warn = h.state === "warn" || h.state === "bad";
  if (h.text && o.on) lines.push(el("div", { class: `small ${warn ? "warn-text" : "muted"}` }, h.text));
  if (o.role === "ato" && o.on) {
    const run = strip.data?.ato?.running_for;
    lines.push(el("div", { class: "small muted" }, run != null ? `Pump running (${mmss(run)})` : "Idle"));
  }
  return lines;
}

function outletTile(o) {
  const [cls, label, warn] = outletPill(o);
  const href = o.role === "ato" ? "#/ato" : `#/outlet/${o.position}`;
  return el("article", { class: `tile${warn ? " warn" : ""}` },
    el("div", { class: "tile-head" },
      el("a", { class: "tile-name", href }, o.name),
      el("span", { class: "muted small" }, `${ROLE_NAMES[o.role]} · ${o.position}`)),
    el("div", { class: "tile-power" },
      el("span", { class: "tile-watts" }, fmtWatts(o.watts)), el("span", { class: "muted" }, " W"),
      el("span", { class: `pill ${cls}` }, label)),
    ...outletLines(o),
    el("div", { class: "tile-action" }, outletAction(o),
      el("a", { class: "details", href }, "Details ›")));
}

function stripWarnings() {
  return [...(strip.error ? [strip.error] : []), ...(strip.data?.warnings || [])];
}

loadStrip();
setInterval(loadStrip, 5000);
setInterval(tickCountdowns, 1000);
