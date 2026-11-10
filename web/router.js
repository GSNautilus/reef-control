"use strict";
// Page addresses: #/ (Control), #/lights, #/outlet/N, #/ato, #/power.
// Loaded last, after every page script has registered its bus.on("route").

const PAGES = ["control", "lights", "outlet", "ato", "power"];
const NAV_FOR = { control: "control", lights: "control", outlet: "control", ato: "control", power: "power" };
const TITLES = { control: "Control", lights: "Lights", outlet: "Outlet", ato: "ATO", power: "Power" };

function route() {
  const [page, arg] = location.hash.replace(/^#\/?/, "").split("/");
  const name = PAGES.includes(page) ? page : "control";
  for (const p of PAGES) $(`#page-${p}`).hidden = p !== name;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("on", a.dataset.nav === NAV_FOR[name]);
  document.title = `${TITLES[name]} · Reef Control`;
  window.scrollTo(0, 0);
  bus.emit("route", { page: name, arg });
}

window.addEventListener("hashchange", route);
route();
