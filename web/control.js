"use strict";
// Control page (home): warnings, Feed / Maintenance, and every switchable thing
// in one grid, each with a link to its detail page.

const GROUPS = [
  ["Flow", ["return", "circulation"]],
  ["Skimmer and top-off", ["skimmer", "ato"]],
];

function controlVisible() {
  return !$("#page-control").hidden;
}

// ---- lights tiles ---------------------------------------------------------------------

function lightMode(light, s) {
  const live = state.live?.active && state.live.levels?.[light];
  if (live) return state.live.hold ? "Held for maintenance" : "Live control";
  if (state.power.on[light] === false) return "Off (held dark)";
  if (s.online === false) return "Not answering";
  if (s.online == null) return "Checking…";
  if (light === "prime_hd") return s.schedule_enabled ? "Following its schedule" : "Manual mode";
  if (s.scene && s.scene.id !== OFF_SCENE_ID) return "Live scene";
  return "Following its schedule";
}

function lightTile(light) {
  const s = state.status[light] || { online: null };
  const dot = s.online === true ? "on" : s.online === false ? "off" : "";
  const lines = [el("div", { class: "small muted" }, lightMode(light, s))];
  let watts = null;
  if (light === "prime_hd" && s.online) {
    watts = s.watts_estimate;
    const lit = SHARED.filter((c) => s.levels?.[c]).sort((a, b) => s.levels[b] - s.levels[a]).slice(0, 3)
      .map((c) => `${NAMES[c]} ${pct(s.levels[c])}%`);
    lines.push(el("div", { class: "small muted" }, lit.length ? lit.join(", ") : "Dark"));
  }
  if (light === "prime_16hd" && s.online) {
    watts = s.watts;
    if (s.driver_temperature != null) {
      lines.push(el("div", { class: "small muted" }, `Driver ${s.driver_temperature} °C, LEDs ${s.module_temperature} °C`));
    }
  }
  if (state.power.errors?.[light]) lines.push(el("div", { class: "small warn-text" }, state.power.errors[light]));
  if (state.dim?.behind?.includes(light)) {
    lines.push(el("div", { class: "small warn-text" }, "Not at the dim setting yet"));
  } else if (state.dim?.on) {
    lines.push(el("div", { class: "small warn-text" }, `Dim mode: schedule at ${state.dim.percent}%`));
  }
  const unit = light === "prime_hd" ? " W est." : " W";
  return el("article", { class: "tile" },
    el("div", { class: "tile-head" },
      el("a", { class: "tile-name", href: "#/lights" }, el("span", { class: `dot ${dot}` }), " ", LIGHT_NAMES[light]),
      el("span", { class: "muted small" }, light === "prime_hd" ? "Light · estimated" : "Light · measured")),
    el("div", { class: "tile-power" },
      watts != null
        ? [el("span", { class: "tile-watts" }, fmtWatts(watts)), el("span", { class: "muted" }, unit)]
        : el("span", { class: "muted small" }, s.online == null ? "Reading…" : "")),
    ...lines,
    el("div", { class: "tile-action" }, powerSwitch(light),
      el("a", { class: "details", href: "#/lights" }, "Schedule and live ›")));
}

// ---- modes ------------------------------------------------------------------------------

function renderModes() {
  const d = strip.data;
  const box = $("#control-modes");
  if (!d) { box.replaceChildren(el("span", { class: "muted" }, "Loading the power strip…")); return; }
  const s = d.settings;
  const busy = strip.busy || !d.online;
  const mode = d.mode;
  if (!mode) {
    box.replaceChildren(
      el("div", { class: "mode" },
        el("button", { class: "primary", disabled: busy,
          onclick: () => stripCall("POST", "/api/strip/feed", { action: "start" }) }, "Feed"),
        el("span", { class: "muted" },
          `Pumps and skimmer off for ${s.feed_minutes} min; skimmer back ${s.skimmer_delay_minutes} min later.`)),
      el("div", { class: "mode" },
        el("button", { disabled: busy, onclick: () => stripCall("POST", "/api/strip/maintenance", { action: "start" },
          "Turn off all pumps, the skimmer and the ATO, and set the lights to white only?") }, "Maintenance"),
        el("span", { class: "muted" },
          `Everything off, lights white ${s.maintenance_white_percent}%. Stays that way until you press Done.`)));
    return;
  }
  if (mode.name === "feed") {
    const text = left(mode.pumps_left) > 0
      ? ["Feeding: pumps back in ", countdown(mode.pumps_left), ", skimmer in ", countdown(mode.ends_left)]
      : ["Pumps back on; skimmer back in ", countdown(mode.ends_left)];
    box.replaceChildren(el("div", { class: "banner" }, el("strong", {}, ...text), el("div", { class: "spacer" }),
      el("button", { disabled: busy, onclick: () => stripCall("POST", "/api/strip/feed", { action: "end" }) }, "End now")));
  } else {
    box.replaceChildren(el("div", { class: "banner" },
      el("strong", {}, `Maintenance since ${clockTime(mode.started)}: everything off, lights white ${s.maintenance_white_percent}%`),
      el("div", { class: "spacer" }),
      el("button", { class: "primary", disabled: busy,
        onclick: () => stripCall("POST", "/api/strip/maintenance", { action: "done" }) }, "Done")));
  }
}

// ---- page ---------------------------------------------------------------------------------

function renderControlWarnings() {
  const items = [...stripWarnings()];
  for (const light of Object.keys(LIGHT_NAMES)) {
    const s = state.status[light];
    if (s?.online === false) items.push(`${LIGHT_NAMES[light]} isn't answering${s.error ? ` (${s.error})` : ""}`);
  }
  const box = $("#control-warnings");
  box.hidden = !items.length;
  box.replaceChildren(...items.map((w) => el("div", {}, w)));
}

function renderControl() {
  if (!controlVisible()) return;
  const d = strip.data;
  const status = $("#control-status");
  status.textContent = !d ? "" : d.online === false ? "Power strip not answering"
    : d.online == null ? "Connecting to the power strip…"
    : `Strip updated ${Math.max(0, Math.round(d.now - (d.updated || d.now)))}s ago`;
  renderControlWarnings();
  renderModes();
  $("#group-lights").replaceChildren(...Object.keys(LIGHT_NAMES).map(lightTile));
  // Don't redraw outlet tiles while a menu in them is open (the rest of the page still updates).
  const outlets = $("#control-outlets");
  const menuOpen = outlets.contains(document.activeElement) && document.activeElement.tagName === "SELECT";
  if (!menuOpen) {
    outlets.replaceChildren(...(d ? GROUPS.map(([title, roles]) => el("div", { class: "group" },
      el("h3", {}, title),
      el("div", { class: "tiles" }, ...d.outlets.filter((o) => roles.includes(o.role)).map(outletTile)))) : []));
  }
  renderModeSettings();
  $("#control-notes").replaceChildren(...((d?.notes || []).length ? d.notes : [{ time: null, text: "Nothing yet since the dashboard started." }])
    .map((n) => el("li", {}, n.time ? el("span", { class: "muted" }, `${clockTime(n.time)} `) : "", n.text)));
}

const MODE_SETTINGS = [
  ["feed_minutes", "Feed: pumps off (min)", 1],
  ["skimmer_delay_minutes", "Skimmer back after the pumps (min)", 1],
  ["maintenance_white_percent", "Maintenance light, white (%)", 0.5],
];

function renderModeSettings() {
  const box = $("#mode-settings");
  if (!strip.data || box.childElementCount) return;  // built once; inputs keep their values
  box.replaceChildren(...MODE_SETTINGS.map(([key, label, step]) => {
    const input = el("input", { type: "number", step, value: strip.data.settings[key] });
    input.addEventListener("change", () => stripCall("PUT", "/api/strip/settings", { [key]: Number(input.value) }));
    return el("label", {}, el("span", {}, label), input);
  }));
}

bus.on("strip", renderControl);
bus.on("lights", renderControl);
bus.on("route", (r) => { if (r.page === "control") renderControl(); });
