// The trajlab trace site (src/trajlab/capture/trace_server.py serves it): routes, the top bar,
// and keys. Views live in view-*.js.

import { $, api, h, icon, nav, put, setCrumbs, stale, state, storageSet, viewRoot } from "./core.js";
import { loadGuide, renderGuide, toggleGuide } from "./guide.js";
import { flushSave, renderSaveStatus } from "./labels.js";
import { showCompare } from "./view-compare.js";
import { showHome } from "./view-home.js";
import { showJob } from "./view-job.js";
import { showTrial } from "./view-trial.js";
import { showTutorial } from "./view-tutorial.js";

// Routes: #/, #/job/<job>[/queue|/agreement], #/trial/<job>/<trial>[/trace|/replay|/files[/<x>]],
// #/compare/<job>/<a>/<b>. Links from the first version of the site (#/<job>/<trial>) still work.
export function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const [first] = parts;
  if (!first) return { view: "home" };
  if (first === "job") return { view: "job", job: parts[1], tab: parts[2] || "trials" };
  if (first === "trial") return { view: "trial", job: parts[1], trial: parts[2], tab: parts[3] || "trace", select: parts.slice(4).join("/") || null };
  if (first === "compare") return { view: "compare", job: parts[1], a: parts[2], b: parts[3] };
  if (first === "tutorial") return { view: "tutorial", section: parts[1] || null };
  if (parts.length === 1) return { view: "job", job: parts[0], tab: "trials" };
  return { view: "trial", job: parts[0], trial: parts[1], tab: "trace", select: parts[2] || null };
}

async function route() {
  const seq = ++nav.seq;
  await flushSave();
  if (stale(seq)) return;
  const r = parseRoute();
  state.route = r;
  state.notice = null;
  state.keyHandler = null;
  state.onHints = null;
  document.body.dataset.view = r.view;
  $("#hints-toggle").hidden = r.view !== "trial";
  try {
    if (!state.meta) state.meta = await api("/api/meta");
    if (!state.guide) await loadGuide();
    if (stale(seq)) return;
    if (r.view === "trial") await showTrial(r, seq);
    else {
      state.trace = null;
      state.labels = null;
      renderSaveStatus();
      if (r.view === "job") await showJob(r, seq);
      else if (r.view === "compare") await showCompare(r, seq);
      else if (r.view === "tutorial") await showTutorial(r, seq);
      else await showHome(seq);
    }
  } catch (error) {
    if (!stale(seq)) showError(error);
  }
}

function showError(error) {
  setCrumbs([]);
  put(
    viewRoot("page"),
    h("div", { class: "error-box" }, icon("alert"), h("div", null, h("strong", null, "Could not load this page. "), error.message, h("p", null, h("a", { href: "#/" }, "Back to the experiments")))),
  );
}

// ---------- theme ----------

const THEMES = ["system", "light", "dark"];
const THEME_ICONS = { system: "monitor", light: "sun", dark: "moon" };

function applyTheme() {
  if (state.theme === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = state.theme;
  const button = $("#theme-button");
  put(button, icon(THEME_ICONS[state.theme]));
  button.title = `Theme: ${state.theme} (click to change)`;
}

$("#theme-button").addEventListener("click", () => {
  state.theme = THEMES[(THEMES.indexOf(state.theme) + 1) % THEMES.length];
  storageSet("trajlab.theme", state.theme === "system" ? null : state.theme);
  applyTheme();
});

// ---------- top bar ----------

const labelerInput = $("#labeler");
labelerInput.value = state.labeler;
labelerInput.addEventListener("change", async () => {
  const name = labelerInput.value.trim();
  if (name && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/.test(name)) {
    labelerInput.setCustomValidity("Use 1-40 letters, digits, '.', '_' or '-', starting with a letter or digit.");
    labelerInput.reportValidity();
    return;
  }
  labelerInput.setCustomValidity("");
  await flushSave();
  state.labeler = name;
  state.labels = null;
  state.labelsFor = null;
  state.queue = null;
  storageSet("trajlab.labeler", name || null);
  route();
});

$("#hints").addEventListener("change", (event) => {
  state.hints = event.target.checked;
  if (state.onHints) state.onHints();
});

$("#guide-button").addEventListener("click", () => toggleGuide());
$("#keys-button").addEventListener("click", () => toggleKeys());

// ---------- shortcuts ----------

const KEYS = [
  ["Everywhere", [["g", "open or close the labeling guide"], ["?", "this list"], ["Esc", "close a panel, or leave a text box"]]],
  ["Trace", [["j / k", "next / previous step you can label"], ["↓ / ↑", "next / previous row"], ["]", "next step you have not judged"], ["1–8", "toggle a pattern"], ["0", "none of these"], ["u", "unsure"], ["w", "mark the first wrong step"], ["n", "write a note"]]],
  ["Replay", [["← / →", "previous / next call"], ["Home / End", "first / last call"], ["Space", "play or pause"], ["1–8, 0, u, w", "label the shown call"]]],
];

function toggleKeys(open) {
  const overlay = $("#overlay");
  const show = open ?? overlay.hidden;
  overlay.hidden = !show;
  if (!show) return;
  put(
    overlay,
    h(
      "div",
      { class: "modal" },
      h("div", { class: "drawer-head" }, icon("keyboard"), h("strong", null, "Keyboard shortcuts"), h("button", { class: "icon-btn", onclick: () => toggleKeys(false) }, icon("x"))),
      h("div", { class: "keys-grid" }, KEYS.map(([group, rows]) => h("div", null, h("h4", null, group), h("dl", null, rows.map(([k, text]) => [h("dt", null, h("kbd", null, k)), h("dd", null, text)]))))),
      h("div", { class: "modal-foot" }, h("a", { href: "#/tutorial", onclick: () => toggleKeys(false) }, icon("book"), "Read the tutorial")),
    ),
  );
}

$("#overlay").addEventListener("click", (event) => {
  if (event.target.id === "overlay") toggleKeys(false);
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  const typing = target instanceof HTMLTextAreaElement || (target instanceof HTMLInputElement && !["checkbox", "range"].includes(target.type)) || target instanceof HTMLSelectElement;
  if (typing) {
    if (event.key === "Escape") target.blur();
    return;
  }
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape") {
    toggleKeys(false);
    if (state.guideOpen) toggleGuide(false);
    return;
  }
  if (event.key === "?") toggleKeys();
  else if (event.key === "g") toggleGuide();
  else if (!(state.keyHandler && state.keyHandler(event))) return;
  event.preventDefault();
});

window.addEventListener("hashchange", route);
applyTheme();
route().then(() => {
  if (state.guideOpen) renderGuide();
});
