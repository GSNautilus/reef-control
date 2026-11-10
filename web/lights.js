"use strict";
// Lights page: status cards, on/off, live control, schedule editor, history.
// Uses helpers from common.js.

const CHANNELS = ["uv", "violet", "royal_blue", "blue", "cool_white", "deep_red", "green", "moonlight"];
const SHARED = CHANNELS.slice(0, -1);
const NAMES = {
  uv: "UV", violet: "Violet", royal_blue: "Royal blue", blue: "Blue",
  cool_white: "Cool white", deep_red: "Deep red", green: "Green", moonlight: "Moonlight",
};
const LIGHT_NAMES = { prime_hd: "Prime HD", prime_16hd: "Prime 16HD" };
const LIGHT_CHANNELS = { prime_hd: SHARED, prime_16hd: CHANNELS };

const state = {
  draft: null,          // {points, tweaks, effective}
  view: "base",
  liveTargets: ["prime_hd", "prime_16hd"],
  liveLevels: Object.fromEntries(CHANNELS.map((c) => [c, 0])),
  live: null,
  status: {},
  power: { on: {}, errors: {} },
  powerBusy: new Set(),
  dim: null,            // {on, percent, busy, lights, behind}
  dimBusy: false,
};

const pct = (permille) => Math.round(permille) / 10;
const permille = (percent) => Math.max(0, Math.min(1000, Math.round(Number(percent) * 10)));
const color = (c) => `var(--c-${c})`;

function minutesNow() {
  const d = new Date();
  return d.getHours() * 60 + d.getMinutes();
}

function levelAt(points, channel, minute) {
  if (!points.length) return 0;
  const pts = [...points].sort((a, b) => a.time - b.time);
  let i = pts.findIndex((p) => p.time >= minute);
  if (i !== -1 && pts[i].time === minute) return pts[i].levels[channel] || 0;
  const next = i === -1 ? pts[0] : pts[i];
  const prev = i === -1 ? pts[pts.length - 1] : pts[(i - 1 + pts.length) % pts.length];
  const span = ((next.time - prev.time + 1440) % 1440) || 1440;
  const into = (minute - prev.time + 1440) % 1440;
  const a = prev.levels[channel] || 0;
  const b = next.levels[channel] || 0;
  return a + ((b - a) * into) / span;
}

// ---- lights ---------------------------------------------------------------

const OFF_SCENE_ID = 0x2B80;

function powerSwitch(light) {
  const on = state.power.on[light] !== false;
  const busy = state.powerBusy.has(light);
  return el("button", {
    class: `switch ghost${on ? " on" : ""}${busy ? " busy" : ""}`,
    title: on ? "Turn off (held dark until turned back on)" : "Turn on (back to its schedule)",
    disabled: busy ? "" : null,
    onclick: () => setPower([light], !on),
  }, el("span", { class: "track" }, el("span", { class: "knob" })), busy ? "…" : on ? "On" : "Off");
}

async function setPower(lights, on) {
  for (const l of lights) state.powerBusy.add(l);
  renderCards();
  try {
    state.power = await api("POST", "/api/power", { targets: lights, on });
  } catch (e) {
    state.power.errors = Object.fromEntries(lights.map((l) => [l, e.message]));
  } finally {
    for (const l of lights) state.powerBusy.delete(l);
    renderCards();
    setTimeout(() => loadStatus(), 1500);
  }
}

function renderCards() {
  for (const light of Object.keys(LIGHT_NAMES)) renderCard(light, state.status[light] || { online: null });
  bus.emit("lights");
}

function renderCard(light, s) {
  const card = $(`#card-${light}`);
  const dot = s.online === true ? "on" : s.online === false ? "off" : "";
  const rows = [];
  const on = state.power.on[light] !== false;
  rows.push(["Power", on ? "on — following its schedule" : "off — held dark by the dashboard"]);
  if (state.power.errors?.[light]) rows.push(["Power error", state.power.errors[light]]);
  const dim = state.dim;
  if (dim?.behind?.includes(light)) rows.push(["Dim mode", "not at the dim setting yet (see Dim mode below)"]);
  else if (dim?.on) rows.push(["Dim mode", `on — schedule at ${dim.percent}%`]);
  if (s.online === false) rows.push(["Status", s.error || "offline"]);
  else if (s.online == null) rows.push(["Status", "checking…"]);
  else {
    rows.push(["Firmware", s.firmware ?? "?"]);
    if (light === "prime_hd") {
      rows.push(["Schedule", s.schedule_enabled ? "on" : "off (manual)"]);
      const lit = SHARED.filter((c) => s.levels?.[c]).map((c) => `${NAMES[c]} ${pct(s.levels[c])}%`);
      rows.push(["Right now", lit.length ? lit.join(", ") : "dark"]);
      if (s.watts_estimate != null) rows.push(["Power", `~${s.watts_estimate} W (estimated from its color levels)`]);
    } else {
      const mode = !s.scene ? "schedule"
        : s.scene.id === OFF_SCENE_ID ? `off scene (${Math.round(s.scene.seconds_left / 360) / 10} h left)`
        : `held live setting (${Math.round(s.scene.seconds_left / 360) / 10} h left, renewed while the dashboard runs)`;
      rows.push(["Mode", mode]);
      if (s.watts != null) rows.push(["Power", `${s.watts} W measured (${s.volts} V × ${s.amps} A)`]);
      if (s.driver_temperature != null) rows.push(["Driver / LEDs", `${s.driver_temperature} °C / ${s.module_temperature} °C`]);
    }
  }
  if (s.checked) rows.push(["Checked", s.checked.slice(11)]);
  card.replaceChildren(
    el("h3", {}, el("span", { class: `dot ${dot}` }), LIGHT_NAMES[light], el("span", { class: "spacer" }),
      powerSwitch(light)),
    el("dl", {}, rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
  );
}

async function loadStatus(refresh = false) {
  try {
    const status = await api("GET", `/api/status${refresh ? "?refresh=true" : ""}`);
    state.power = status.power;
    if (status.dim) state.dim = status.dim;
    state.dimUnsupported = !status.dim;
    state.status = status;
  } catch (e) {
    state.status = Object.fromEntries(Object.keys(LIGHT_NAMES).map((l) => [l, { online: false, error: e.message }]));
  }
  renderDim();
  renderCards();
}

// ---- dim mode -----------------------------------------------------------------

function renderDim() {
  // Off until the server says otherwise; the switch is always shown.
  const known = !!state.dim;
  const d = state.dim || { on: false, percent: Number($("#dim-percent").value) || 70, behind: [] };
  const busy = state.dimBusy || !!d.busy;
  const live = !!state.live?.active;
  const blocked = busy || live || !known;
  const slider = $("#dim-percent");
  if (known && !state.dimDragging) {
    slider.value = d.percent;
    $("#dim-out").textContent = `${d.percent}%`;
  }
  slider.disabled = blocked;
  $("#dim-switch").replaceChildren(el("button", {
    class: `switch ghost${d.on ? " on" : ""}${busy ? " busy" : ""}`,
    title: d.on ? "Turn dim mode off (schedules back to 100%)" : "Turn dim mode on at the level on the slider",
    disabled: blocked ? "" : null,
    onclick: () => setDim(!d.on, Number(slider.value)),
  }, el("span", { class: "track" }, el("span", { class: "knob" })), busy ? "…" : d.on ? "On" : "Off"));
  const behind = (d.behind || []).map((l) => LIGHT_NAMES[l]);
  const status = $("#dim-status");
  status.replaceChildren(
    !known ? (state.dimUnsupported
      ? el("span", { class: "warn-text" }, "The dashboard server is older than this page; restart the dashboard to use dim mode")
      : "Checking…")
      : busy ? "Writing both lights' schedules…"
      : live ? "Live control is holding the lights; dim mode can be changed after \"Back to schedule\""
      : d.on ? `On: schedules at ${d.percent}%` : "Off: schedules at 100%",
    !busy && behind.length ? el("span", { class: "warn-text" }, ` · ${behind.join(" and ")} not at this setting yet `) : "",
    !busy && !live && behind.length ? el("button", { class: "ghost", onclick: () => setDim(d.on, d.percent) }, "Retry") : "",
  );
}

async function setDim(on, percent) {
  const writes = on || state.dim?.on || state.dim?.behind?.length;
  state.dimBusy = true;
  renderDim();
  if (writes) {
    $("#dim-result").replaceChildren(el("p", { class: "muted" },
      "Writing both lights' schedules… the 16HD can take up to a minute over Bluetooth."));
  }
  try {
    const out = await api("POST", "/api/dim", { on, percent });
    state.dim = out.dim;
    $("#dim-result").replaceChildren(...renderResults(out.results));
    refreshEffective();
  } catch (e) {
    $("#dim-result").replaceChildren(el("p", { class: "error" }, e.message));
  } finally {
    state.dimBusy = false;
    renderDim();
    renderCards();
  }
}

async function refreshEffective() {
  // The chart's per-light views include dim mode; the draft itself is unchanged.
  if (!state.draft) return;
  try {
    const view = await api("GET", "/api/draft");
    state.draft.effective = view.effective;
    renderChart();
  } catch { /* the next draft save refreshes it */ }
}

// ---- live control -----------------------------------------------------------

function liveChannels() {
  return state.liveTargets.includes("prime_16hd") ? CHANNELS : SHARED;
}

function renderSliders() {
  $("#live-sliders").replaceChildren(...liveChannels().map((c) => {
    const out = el("output", {}, `${pct(state.liveLevels[c])}%`);
    const input = el("input", { type: "range", min: 0, max: 100, step: 0.5, value: pct(state.liveLevels[c]) });
    input.addEventListener("input", () => {
      state.liveLevels[c] = permille(input.value);
      out.textContent = `${pct(state.liveLevels[c])}%`;
      queueLive();
    });
    return el("div", { class: "slider" },
      el("label", {}, el("span", { class: "swatch", style: `background:${color(c)}` }), NAMES[c]), input, out);
  }));
}

let liveTimer = null;
let liveInFlight = false;
let liveAgain = false;

function queueLive() {
  clearTimeout(liveTimer);
  liveTimer = setTimeout(sendLive, 120);
}

async function sendLive() {
  if (liveInFlight) { liveAgain = true; return; }
  liveInFlight = true;
  try {
    state.live = await api("POST", "/api/live", { targets: state.liveTargets, levels: state.liveLevels });
    renderLive();
  } catch (e) {
    showLiveError(e.message);
  } finally {
    liveInFlight = false;
    if (liveAgain) { liveAgain = false; sendLive(); }
  }
}

function showLiveError(msg) {
  const box = $("#live-errors");
  box.hidden = !msg;
  box.textContent = msg || "";
}

function renderLive() {
  const live = state.live;
  const active = live?.active;
  $("#live-badge").hidden = !active;
  $("#live-stop").disabled = !active;
  $("#send").disabled = !!active;
  if (active) {
    const lights = Object.keys(live.levels).map((l) => LIGHT_NAMES[l]).join(" + ");
    const until = live.hold ? "held for maintenance" : "held until you press Back to schedule";
    $("#live-status").textContent = `Live on ${lights}, ${until}` +
      (live.pending?.prime_16hd ? " · sending to 16HD…" : "");
  } else {
    $("#live-status").textContent = "Lights are following their schedules";
  }
  const errors = Object.entries(live?.errors || {}).map(([l, e]) => `${LIGHT_NAMES[l]}: ${e}`);
  showLiveError(errors.join(" · "));
  renderDim();
}

async function pollLive() {
  try {
    state.live = await api("GET", "/api/live");
    renderLive();
    bus.emit("lights");
  } catch { /* server restarting */ }
  setTimeout(pollLive, state.live?.pending?.prime_16hd ? 1000 : 5000);
}

// ---- schedule chart ---------------------------------------------------------

function viewPoints() {
  if (!state.draft) return [];
  return state.view === "base" ? state.draft.points : state.draft.effective[state.view];
}

function viewChannels() {
  return state.view === "prime_hd" ? SHARED : CHANNELS;
}

function renderChart() {
  const W = 1000, H = 260, L = 38, R = 8, T = 10, B = 24;
  const x = (m) => L + (m / 1440) * (W - L - R);
  const y = (v) => T + (1 - v / 1000) * (H - T - B);
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const add = (tag, attrs, parent = svg) => {
    const n = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    parent.append(n);
    return n;
  };
  const grid = add("g", { class: "grid" });
  const axis = add("g", { class: "axis" });
  for (let v = 0; v <= 1000; v += 250) {
    add("line", { x1: L, x2: W - R, y1: y(v), y2: y(v) }, grid);
    add("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, axis).textContent = `${v / 10}%`;
  }
  for (let h = 0; h <= 24; h += 3) {
    add("line", { x1: x(h * 60), x2: x(h * 60), y1: T, y2: H - B }, grid);
    add("text", { x: x(h * 60), y: H - 6, "text-anchor": "middle" }, axis).textContent = `${h}:00`;
  }
  const pts = [...viewPoints()].sort((a, b) => a.time - b.time);
  for (const c of viewChannels()) {
    if (!pts.some((p) => p.levels[c])) continue;
    const coords = [[0, levelAt(pts, c, 0)], ...pts.map((p) => [p.time, p.levels[c] || 0]), [1440, levelAt(pts, c, 0)]];
    add("polyline", {
      points: coords.map(([m, v]) => `${x(m)},${y(v)}`).join(" "),
      fill: "none", stroke: color(c), "stroke-width": 2, "vector-effect": "non-scaling-stroke",
    });
  }
  const now = minutesNow();
  add("line", { class: "now", x1: x(now), x2: x(now), y1: T, y2: H - B, "vector-effect": "non-scaling-stroke" });
  $("#chart").replaceChildren(svg);
  $("#legend").replaceChildren(...viewChannels().filter((c) => pts.some((p) => p.levels[c])).map((c) =>
    el("span", {}, el("span", { class: "swatch", style: `background:${color(c)}` }), NAMES[c])));
}

// ---- schedule table & tweaks -------------------------------------------------

function renderPoints() {
  const head = el("tr", {}, el("th", {}, "Time"), CHANNELS.map((c) =>
    el("th", {}, el("span", { class: "swatch", style: `background:${color(c)};display:inline-block;margin-right:4px` }), NAMES[c])),
    el("th", {}, ""));
  const rows = state.draft.points.map((p, i) => el("tr", {},
    el("td", {}, el("input", {
      type: "time", value: hhmm(p.time),
      onchange: (e) => {
        const [h, m] = e.target.value.split(":").map(Number);
        if (!Number.isNaN(h)) { p.time = h * 60 + m; saveDraft(true); }
      },
    })),
    CHANNELS.map((c) => el("td", {}, el("input", {
      type: "number", min: 0, max: 100, step: 0.1, value: pct(p.levels[c] || 0),
      onchange: (e) => { p.levels[c] = permille(e.target.value); e.target.value = pct(p.levels[c]); saveDraft(); },
    }))),
    el("td", {}, el("button", {
      class: "ghost", title: "Remove point", disabled: state.draft.points.length <= 2 ? "" : null,
      onclick: () => { state.draft.points.splice(i, 1); saveDraft(true); },
    }, "✕")),
  ));
  $("#points").replaceChildren(el("thead", {}, head), el("tbody", {}, rows));
}

function renderTweaks() {
  $("#tweaks").replaceChildren(...Object.keys(LIGHT_NAMES).map((light) => {
    const t = state.draft.tweaks[light];
    const input = (value, onchange, step = 0.1) =>
      el("input", { type: "number", step, value, onchange });
    return el("div", { class: "tweak" },
      el("h4", {}, LIGHT_NAMES[light]),
      el("div", { class: "grid2" },
        el("span", {}, "Scale (%)"),
        input(t.scale, (e) => { t.scale = Number(e.target.value) || 0; saveDraft(); }, 1),
        LIGHT_CHANNELS[light].flatMap((c) => [
          el("span", {}, `${NAMES[c]} offset (%)`),
          input(pct(t.offsets[c] || 0), (e) => { t.offsets[c] = Math.round(Number(e.target.value) * 10) || 0; saveDraft(); }),
        ]),
      ));
  }));
}

function signedDuration(minutes) {
  const a = Math.abs(minutes);
  const h = Math.floor(a / 60), m = a % 60;
  return (h && m) ? `${h} h ${m} min` : h ? `${h} h` : `${m} min`;
}

function renderShift() {
  const shift = state.draft.shift || 0;
  const input = $("#shift-hours");
  if (document.activeElement !== input) input.value = shift / 60;
  const pts = [...state.draft.points].sort((a, b) => a.time - b.time);
  const at = (t) => hhmm((((t + shift) % 1440) + 1440) % 1440);
  const first = pts[0], last = pts[pts.length - 1];
  $("#shift-effect").textContent = !shift || !first
    ? "No shift: the lights run the points at the times shown."
    : `Every point runs ${signedDuration(shift)} ${shift < 0 ? "earlier" : "later"}: ` +
      `${hhmm(first.time)} → ${at(first.time)} … ${hhmm(last.time)} → ${at(last.time)}.`;
}

function setShift(minutes) {
  state.draft.shift = Math.max(-720, Math.min(720, Math.round(minutes) || 0));
  renderShift();
  saveDraft();
}

function renderDraft() {
  renderPoints();
  renderShift();
  renderTweaks();
  renderChart();
}

let saveTimer = null;

function saveDraft(rerenderTable = false) {
  $("#draft-state").textContent = "Saving draft…";
  if (rerenderTable) {
    state.draft.points.sort((a, b) => a.time - b.time);
    renderPoints();
    renderShift();
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const view = await api("PUT", "/api/draft",
        { points: state.draft.points, tweaks: state.draft.tweaks, shift: state.draft.shift || 0 });
      state.draft.effective = view.effective;
      $("#draft-state").textContent = "Draft saved — not sent yet";
      renderChart();
    } catch (e) {
      $("#draft-state").textContent = `Couldn't save draft: ${e.message}`;
    }
  }, 300);
}

function addPoint() {
  const pts = state.draft.points;
  const now = minutesNow();
  let time = now - (now % 15);
  while (pts.some((p) => p.time === time)) time = (time + 15) % 1440;
  pts.push({ time, levels: Object.fromEntries(CHANNELS.map((c) => [c, Math.round(levelAt(pts, c, time))])) });
  saveDraft(true);
}

async function importFrom(light) {
  if (!confirm(`Replace the draft's points with the schedule currently on the ${LIGHT_NAMES[light]}?`)) return;
  $("#draft-state").textContent = `Reading the ${LIGHT_NAMES[light]}'s schedule…`;
  try {
    state.draft = await api("POST", `/api/draft/import/${light}`);
    renderDraft();
    $("#draft-state").textContent = `Imported from the ${LIGHT_NAMES[light]}`;
  } catch (e) {
    $("#draft-state").textContent = `Import failed: ${e.message}`;
  }
}

// ---- sending & history --------------------------------------------------------

function renderResults(results) {
  return Object.entries(results).map(([light, r]) => el("div", { class: `result ${r.ok ? "ok" : "fail"}` },
    el("p", {}, el("strong", {}, LIGHT_NAMES[light]), " — ",
      r.ok ? (r.unchanged ? "already had this schedule (nothing written)" : "sent and checked ✓") : `failed: ${r.error}`),
    r.backup ? el("p", { class: "muted" }, `Previous schedule backed up to ${r.backup}`) : ""));
}

async function send() {
  if (!confirm("Send this schedule to both lights? Each light's current schedule is backed up first.")) return;
  const button = $("#send");
  button.disabled = true;
  $("#send-result").replaceChildren(el("p", { class: "muted" }, "Sending… the 16HD can take up to a minute over Bluetooth."));
  try {
    const out = await api("POST", "/api/send");
    $("#send-result").replaceChildren(...renderResults(out.results));
    loadHistory();
    loadStatus();
  } catch (e) {
    $("#send-result").replaceChildren(el("p", { class: "error" }, e.message));
  } finally {
    button.disabled = !!state.live?.active;
  }
}

async function loadHistory() {
  const entries = await api("GET", "/api/history");
  const rows = entries.map((h) => el("tr", {},
    el("td", {}, h.created.replace("T", " ")),
    el("td", {}, Object.entries(h.results).map(([l, r]) => `${LIGHT_NAMES[l]} ${r.ok ? "✓" : "✗"}`).join(" · ")),
    el("td", {}, [`${h.draft.points.length} points`,
      h.draft.shift ? `shifted ${signedDuration(h.draft.shift)} ${h.draft.shift < 0 ? "earlier" : "later"}` : "",
      h.draft.dim != null ? `dimmed to ${h.draft.dim}%` : ""].filter(Boolean).join(", ")),
    el("td", {}, el("button", {
      class: "ghost",
      onclick: async () => {
        if (!confirm("Load this schedule into the draft? It won't be sent until you press Send.")) return;
        state.draft = await api("POST", `/api/history/${h.id}/load`);
        renderDraft();
        $("#draft-state").textContent = `Loaded from history (${h.created.replace("T", " ")}) — not sent yet`;
      },
    }, "Load into draft")),
  ));
  $("#history").replaceChildren(rows.length ? el("tbody", {}, rows)
    : el("tbody", {}, el("tr", {}, el("td", { class: "muted" }, "Nothing sent from the dashboard yet."))));
}

// ---- wiring ---------------------------------------------------------------------

function segmented(container, onpick) {
  container.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    for (const other of container.querySelectorAll("button")) other.classList.toggle("on", other === b);
    onpick(b.dataset);
  });
}

segmented($("#live-targets"), (d) => { state.liveTargets = d.targets.split(","); renderSliders(); });
segmented($("#chart-view"), (d) => { state.view = d.view; renderChart(); });

$("#live-off").addEventListener("click", () => {
  for (const c of CHANNELS) state.liveLevels[c] = 0;
  renderSliders();
  sendLive();
});
$("#live-now").addEventListener("click", () => {
  const light = state.liveTargets[0];
  const pts = state.draft?.effective[light] || [];
  for (const c of CHANNELS) state.liveLevels[c] = Math.round(levelAt(pts, c, minutesNow()));
  renderSliders();
});
$("#live-stop").addEventListener("click", async () => {
  try {
    state.live = await api("POST", "/api/live/stop", {});
    renderLive();
    setTimeout(() => loadStatus(), 1500);
  } catch (e) {
    showLiveError(e.message);
  }
});
$("#refresh-status").addEventListener("click", () => {
  state.status = {};
  renderCards();
  loadStatus(true);
});
$("#all-on").addEventListener("click", () => setPower(Object.keys(LIGHT_NAMES), true));
$("#all-off").addEventListener("click", () => setPower(Object.keys(LIGHT_NAMES), false));
$("#add-point").addEventListener("click", addPoint);
$("#import-prime_hd").addEventListener("click", () => importFrom("prime_hd"));
$("#import-prime_16hd").addEventListener("click", () => importFrom("prime_16hd"));
$("#send").addEventListener("click", send);
$("#shift-hours").addEventListener("change", (e) => setShift(Number(e.target.value) * 60));
$("#shift-reset").addEventListener("click", () => setShift(0));
$("#dim-percent").addEventListener("input", (e) => {
  state.dimDragging = true;
  $("#dim-out").textContent = `${e.target.value}%`;
});
$("#dim-percent").addEventListener("change", (e) => {
  state.dimDragging = false;
  if (state.dim) setDim(state.dim.on, Number(e.target.value));
});

async function start() {
  renderCards();
  renderSliders();
  renderDim();
  pollLive();
  loadStatus();
  setInterval(() => loadStatus(), 30000);
  setInterval(renderChart, 60000);
  try {
    state.draft = await api("GET", "/api/draft");
    renderDraft();
    $("#draft-state").textContent = "Draft loaded";
  } catch (e) {
    $("#draft-state").textContent = `Couldn't load the draft: ${e.message}`;
  }
  loadHistory().catch(() => {});
}

start();
