// Shared helpers and state for the trajlab trace site. Every text that comes from a trial is put
// in the page with text nodes, never parsed as HTML.

export const enc = encodeURIComponent;
export const $ = (selector, root = document) => root.querySelector(selector);

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") el.className = value;
    else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
    else el.setAttribute(name, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// replaceChildren that skips null children, as h() does.
export function put(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// Icons: stroke paths on a 24x24 grid (in the style of Feather).
const ICONS = {
  terminal: [["polyline", { points: "4 17 10 11 4 5" }], ["line", { x1: 12, y1: 19, x2: 20, y2: 19 }]],
  file: [["path", { d: "M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" }], ["polyline", { points: "13 2 13 9 20 9" }]],
  filePlus: [["path", { d: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" }], ["polyline", { points: "14 2 14 8 20 8" }], ["line", { x1: 12, y1: 18, x2: 12, y2: 12 }], ["line", { x1: 9, y1: 15, x2: 15, y2: 15 }]],
  edit: [["path", { d: "M12 20h9" }], ["path", { d: "M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z" }]],
  eye: [["path", { d: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" }], ["circle", { cx: 12, cy: 12, r: 3 }]],
  search: [["circle", { cx: 11, cy: 11, r: 7 }], ["line", { x1: 21, y1: 21, x2: 16.65, y2: 16.65 }]],
  flag: [["path", { d: "M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" }], ["line", { x1: 4, y1: 22, x2: 4, y2: 15 }]],
  message: [["path", { d: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" }]],
  chevronRight: [["polyline", { points: "9 18 15 12 9 6" }]],
  chevronDown: [["polyline", { points: "6 9 12 15 18 9" }]],
  chevronLeft: [["polyline", { points: "15 18 9 12 15 6" }]],
  play: [["polygon", { points: "6 4 20 12 6 20 6 4" }]],
  pause: [["rect", { x: 6, y: 4, width: 4, height: 16 }], ["rect", { x: 14, y: 4, width: 4, height: 16 }]],
  skipBack: [["polygon", { points: "19 20 9 12 19 4 19 20" }], ["line", { x1: 5, y1: 19, x2: 5, y2: 5 }]],
  skipForward: [["polygon", { points: "5 4 15 12 5 20 5 4" }], ["line", { x1: 19, y1: 5, x2: 19, y2: 19 }]],
  sun: [["circle", { cx: 12, cy: 12, r: 4 }], ["path", { d: "M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" }]],
  moon: [["path", { d: "M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" }]],
  monitor: [["rect", { x: 2, y: 3, width: 20, height: 14, rx: 2 }], ["line", { x1: 8, y1: 21, x2: 16, y2: 21 }], ["line", { x1: 12, y1: 17, x2: 12, y2: 21 }]],
  book: [["path", { d: "M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z" }], ["path", { d: "M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" }]],
  users: [["path", { d: "M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" }], ["circle", { cx: 9, cy: 7, r: 4 }], ["path", { d: "M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8" }]],
  columns: [["rect", { x: 3, y: 3, width: 18, height: 18, rx: 2 }], ["line", { x1: 12, y1: 3, x2: 12, y2: 21 }]],
  folder: [["path", { d: "M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" }]],
  clock: [["circle", { cx: 12, cy: 12, r: 9 }], ["polyline", { points: "12 7 12 12 15 14" }]],
  list: [["path", { d: "M9 6h12M9 12h12M9 18h12" }], ["polyline", { points: "3 6 4 7 6 5" }], ["polyline", { points: "3 12 4 13 6 11" }], ["polyline", { points: "3 18 4 19 6 17" }]],
  x: [["line", { x1: 18, y1: 6, x2: 6, y2: 18 }], ["line", { x1: 6, y1: 6, x2: 18, y2: 18 }]],
  check: [["polyline", { points: "20 6 9 17 4 12" }]],
  alert: [["path", { d: "M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" }], ["line", { x1: 12, y1: 9, x2: 12, y2: 13 }], ["line", { x1: 12, y1: 17, x2: 12.01, y2: 17 }]],
  external: [["path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }], ["polyline", { points: "15 3 21 3 21 9" }], ["line", { x1: 10, y1: 14, x2: 21, y2: 3 }]],
  download: [["path", { d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" }], ["polyline", { points: "7 10 12 15 17 10" }], ["line", { x1: 12, y1: 15, x2: 12, y2: 3 }]],
  cpu: [["rect", { x: 5, y: 5, width: 14, height: 14, rx: 2 }], ["rect", { x: 9, y: 9, width: 6, height: 6 }], ["path", { d: "M9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4" }]],
  layout: [["rect", { x: 3, y: 3, width: 18, height: 18, rx: 2 }], ["line", { x1: 3, y1: 9, x2: 21, y2: 9 }], ["line", { x1: 9, y1: 21, x2: 9, y2: 9 }]],
  activity: [["polyline", { points: "22 12 18 12 15 21 9 3 6 12 2 12" }]],
  keyboard: [["rect", { x: 2, y: 6, width: 20, height: 12, rx: 2 }], ["path", { d: "M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10" }]],
  arrowRight: [["line", { x1: 5, y1: 12, x2: 19, y2: 12 }], ["polyline", { points: "12 5 19 12 12 19" }]],
  stop: [["rect", { x: 5, y: 5, width: 14, height: 14, rx: 2 }]],
};

export function icon(name, cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attrs] of ICONS[name] || []) {
    const part = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) part.setAttribute(k, String(v));
    svg.append(part);
  }
  return svg;
}

export const TOOL_ICONS = { Bash: "terminal", Write: "filePlus", Edit: "edit", MultiEdit: "edit", NotebookEdit: "edit", Read: "eye", Grep: "search", Glob: "search", LS: "folder", TodoWrite: "list", Stop: "stop", StopFailure: "stop" };
export const toolIcon = (tool) => icon(TOOL_ICONS[tool] || "file", "tool-icon");

// ---------- formatting ----------

export function fmtTime(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.max(0, Math.round(seconds));
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return hh ? `+${hh}:${String(mm).padStart(2, "0")}:${ss}` : `+${mm}:${ss}`;
}

export const fmtMinutes = (m) => (m === null || m === undefined ? "–" : m < 1 ? "<1" : String(Math.round(m)));

export function fmtSeconds(s) {
  if (s === null || s === undefined) return "–";
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min ${Math.round(s % 60)} s` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

export const fmtMs = (ms) => (ms === null || ms === undefined ? "" : fmtSeconds(ms / 1000));

export function fmtBytes(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function fmtTokens(n) {
  if (n === null || n === undefined) return "–";
  if (n < 1000) return String(n);
  if (n < 1e6) return `${(n / 1000).toFixed(n < 1e4 ? 1 : 0)}k`;
  return `${(n / 1e6).toFixed(2)}M`;
}

export const fmtUsd = (x) => (x === null || x === undefined ? "–" : `$${x < 0.1 ? x.toFixed(3) : x.toFixed(2)}`);
export const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
export const shortHash = (hash) => (hash ? hash.slice(0, 8) : "");

export function ordinal(n) {
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

// ---------- storage and the API ----------

export function storageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function storageSet(key, value) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // storage blocked (private window): only conveniences live here
  }
}

export async function api(path, options = {}) {
  const headers = { "X-Trajlab": "1" };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) throw new Error((data && data.error) || `${response.status} ${response.statusText}`);
  return data;
}

// ---------- shared state ----------

export const state = {
  meta: null,
  guide: null,
  guideOpen: false,
  labeler: storageGet("trajlab.labeler") || "",
  hints: false,
  theme: storageGet("trajlab.theme") || "system",
  route: null,
  job: null,
  trialName: null,
  trace: null,
  events: [],
  labels: null,
  queue: null,
  queueJob: null,
  tab: "trace",
  selected: -1,
  replay: null,
  filters: { notHooked: true, messages: true, unlabeled: false, search: "" },
  notice: null,
  save: { dirty: false, inflight: false, error: null, savedAt: null, timer: null, retry: null },
};

export const isLabelable = (e) => !!e && (e.kind === "stop" || (e.kind === "call" && e.hooked));

export function patternTitle(key) {
  const p = state.meta && state.meta.patterns.find((x) => x.key === key);
  return p ? p.title : key;
}

export function patternHelp(key) {
  const fromGuide = state.guide && state.guide.sections && state.guide.sections[key];
  if (fromGuide) return fromGuide;
  const p = state.meta && state.meta.patterns.find((x) => x.key === key);
  return p ? p.help : "";
}

// Links and pills used by several views.
export const trialHref = (job, trial, tab = "trace", select = null) =>
  `#/trial/${enc(job)}/${enc(trial)}/${tab}${select !== null && select !== undefined ? `/${enc(select)}` : ""}`;

export function resultPill(reward, finished = true, died = false) {
  if (reward === null || reward === undefined) {
    if (died) return h("span", { class: "pill bad" }, "died");
    return h("span", { class: "pill" }, finished ? "no reward" : "no result yet");
  }
  return h("span", { class: `pill ${reward >= 1 ? "ok" : "bad"}` }, reward >= 1 ? "passed" : `failed (${reward})`);
}

export function runLinks(job, trial) {
  const open = (path, text, name) =>
    h("a", { class: "link-icon", href: `/api/${path}/${enc(job)}/${enc(trial)}`, target: "_blank", rel: "noopener", title: text }, icon(name), text);
  return h("span", { class: "run-links" }, open("transcript", "Transcript", "file"), open("raw", "JSON", "download"));
}

// Widths go through the style property: the page's CSP blocks inline style attributes.
export function widthOf(el, share) {
  el.style.width = `${(Math.max(0, Math.min(1, share)) * 100).toFixed(1)}%`;
  return el;
}

export function progressBar(done, total, cls = "") {
  const fill = widthOf(h("span", { class: "bar-fill" }), total ? done / total : 0);
  return h("span", { class: `bar ${cls}`, title: `${done} of ${total}` }, fill);
}

export const debounce = (fn, ms) => {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
};

// ---------- navigation ----------

// Each navigation gets a number; a slower, older one never paints over a newer one.
export const nav = { seq: 0 };
export const stale = (seq) => seq !== nav.seq;

export function setCrumbs(items) {
  const crumbs = $("#crumbs");
  put(
    crumbs,
    items.map((item) => [
      h("span", { class: "sep" }, icon("chevronRight")),
      item.href ? h("a", { href: item.href }, item.text) : h("span", { class: "here" }, item.text),
    ]),
  );
}

export function viewRoot(cls) {
  const view = $("#view");
  view.className = cls;
  return view;
}

export function labelerNotice() {
  if (state.labeler) return null;
  return h("div", { class: "notice" }, icon("alert"), "Type your name in Labeler (top right) to label and to see your progress.");
}

export function exportBar(params, what) {
  const url = (format, extra) => `/api/export?${new URLSearchParams({ format, ...params, ...extra })}`;
  const link = (format, extra, text) => h("a", { class: "link-icon", href: url(format, extra) }, icon("download"), text);
  const mine = state.labeler ? { labeler: state.labeler } : null;
  return h(
    "div",
    { class: "export-bar" },
    h("span", { class: "muted" }, `Labels for ${what}:`),
    link("csv", {}, "CSV"),
    link("json", {}, "JSON"),
    mine ? [h("span", { class: "muted" }, "· yours only:"), link("csv", mine, "CSV"), link("json", mine, "JSON")] : null,
  );
}

export function tabs(items, active) {
  return h(
    "nav",
    { class: "tabs" },
    items.map((t) => h("a", { class: `tab${t.key === active ? " active" : ""}`, href: t.href }, t.icon ? icon(t.icon) : null, t.text, t.count !== undefined ? h("span", { class: "count" }, t.count) : null)),
  );
}

export function section(title, ...content) {
  return h("section", { class: "sec" }, h("h3", { class: "sec-title" }, title), ...content);
}

export const note = (text) => h("p", { class: "note" }, text);

// A diff that may be against an older version of its file (`stale_before` in trace.py). Shown
// always, unlike the hints: it is about the record, not about the agent.
const callName = (n) => (n === null || n === undefined ? "the stop" : `#${n}`);
export const STALE_PILL = "may be against an older version";
export function staleNote(stale) {
  const unread = callName(stale.unread_at);
  return h("p", { class: "stale-note" }, `May be against an older version: ${unread} changed this file without reading it, so this diff starts from the file after ${callName(stale.read_at)} and can show ${unread}'s changes as this call's (a watcher fault, fixed on 2026-10-22). Replay's File view shows the real file.`);
}
