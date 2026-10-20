"use strict";

// The trajlab trace site (src/trajlab/capture/trace_server.py serves it). Every text that comes
// from a trial is put in the page with textContent, never parsed as HTML.
(() => {
  const TRIAL_KEY = "\u0000trial"; // the trial-level label's key in the label functions
  const enc = encodeURIComponent;
  const $ = (selector, root = document) => root.querySelector(selector);

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") el.className = value;
      else if (name.startsWith("on")) el.addEventListener(name.slice(2), value);
      else el.setAttribute(name, value === true ? "" : String(value));
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function put(el, ...children) {
    const nodes = children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false);
    el.replaceChildren(...nodes.map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  }

  function storageGet(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function storageSet(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch {
      // storage blocked (private window): the server copy is the one that matters
    }
  }

  async function api(path, options = {}) {
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

  const state = {
    meta: null,
    labeler: storageGet("trajlab.labeler") || "",
    hints: false,
    job: null,
    trialName: null,
    trace: null,
    events: [],
    labels: null,
    selected: -1,
    notice: null,
    filters: { notHooked: true, messages: true, unlabeled: false, search: "" },
    save: { dirty: false, inflight: false, error: null, savedAt: null, timer: null, retry: null },
  };
  let rowEls = new Map();
  let cellEls = new Map();

  // ---------- formatting ----------

  function fmtTime(seconds) {
    if (seconds === null || seconds === undefined) return "";
    const s = Math.max(0, Math.round(seconds));
    const hh = Math.floor(s / 3600);
    const mm = Math.floor((s % 3600) / 60);
    const ss = String(s % 60).padStart(2, "0");
    return hh ? `+${hh}:${String(mm).padStart(2, "0")}:${ss}` : `+${mm}:${ss}`;
  }
  const fmtMinutes = (m) => (m === null || m === undefined ? "–" : m < 1 ? "<1" : String(Math.round(m)));
  const fmtMs = (ms) => (ms === null || ms === undefined ? "" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
  function fmtBytes(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
    return `${(n / 1024 ** 3).toFixed(2)} GB`;
  }
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
  const shortHash = (hash) => (hash ? hash.slice(0, 8) : "");
  const td = (value, cls) => h("td", { class: cls }, value === null || value === undefined ? "–" : value);

  function rewardPill(reward) {
    if (reward === null || reward === undefined) return h("span", { class: "pill" }, "no reward");
    return h("span", { class: `pill ${reward >= 1 ? "ok" : "bad"}` }, `reward ${reward}`);
  }

  // ---------- labels ----------

  const isLabelable = (e) => !!e && (e.kind === "stop" || (e.kind === "call" && e.hooked));
  const keyOf = (index) => (index === -1 ? TRIAL_KEY : state.events[index] && state.events[index].id);

  function getLabel(key) {
    if (!state.labels) return null;
    return key === TRIAL_KEY ? state.labels.trial : state.labels.steps[key] || null;
  }

  const isJudged = (key) => {
    const label = getLabel(key);
    return !!label && Array.isArray(label.patterns);
  };

  function emptyLabels() {
    return {
      job_name: state.job,
      trial_name: state.trialName,
      labeler: state.labeler,
      trial: null,
      steps: {},
      done: false,
      hints_seen: false,
      updated_at: new Date().toISOString(),
    };
  }

  function changeLabel(key, changes, rerender = true) {
    if (!state.labels) return;
    const now = new Date().toISOString();
    const current = getLabel(key) || { patterns: null, unsure: false, note: "" };
    const next = { ...current, ...changes, updated_at: now };
    const empty = next.patterns === null && !next.unsure && !next.note;
    if (key === TRIAL_KEY) state.labels.trial = empty ? null : next;
    else if (empty) delete state.labels.steps[key];
    else state.labels.steps[key] = next;
    state.labels.updated_at = now;
    scheduleSave();
    refreshMarks(key);
    if (rerender) rerenderLabelPanel(key);
  }

  function togglePattern(key, pattern) {
    const current = getLabel(key);
    const chosen = new Set((current && current.patterns) || []);
    if (chosen.has(pattern)) chosen.delete(pattern);
    else chosen.add(pattern);
    const ordered = state.meta.patterns.map((p) => p.key).filter((k) => chosen.has(k));
    // Unticking the last pattern leaves the step unjudged; "None of these" is the explicit no.
    changeLabel(key, { patterns: ordered.length ? ordered : null });
  }

  function markNone(key) {
    const current = getLabel(key);
    const isNone = !!current && Array.isArray(current.patterns) && current.patterns.length === 0;
    changeLabel(key, { patterns: isNone ? null : [] });
  }

  function setDone(done) {
    if (!state.labels) return;
    state.labels.done = done;
    state.labels.updated_at = new Date().toISOString();
    scheduleSave();
    renderTrialBar();
  }

  function markHintsSeen() {
    if (!state.labels || state.labels.hints_seen) return;
    state.labels.hints_seen = true;
    state.labels.updated_at = new Date().toISOString();
    scheduleSave();
  }

  // ---------- saving: every change is saved to the server, and kept in this browser until it is ----------

  const pendingKey = (labels) => `trajlab.pending.${labels.job_name}/${labels.trial_name}/${labels.labeler}`;

  function restorePending(saved) {
    const blank = emptyLabels();
    const raw = storageGet(pendingKey(blank));
    let pending = null;
    try {
      pending = raw ? JSON.parse(raw) : null;
    } catch {
      pending = null;
    }
    const newer = pending && (!saved || Date.parse(pending.updated_at) > Date.parse(saved.updated_at));
    if (newer) {
      state.notice = "Restored labels this browser had not saved yet; saving them now.";
      setTimeout(scheduleSave, 0);
      return pending;
    }
    if (pending) storageSet(pendingKey(blank), null);
    return saved || blank;
  }

  function scheduleSave() {
    if (!state.labels) return;
    state.save.dirty = true;
    storageSet(pendingKey(state.labels), JSON.stringify(state.labels));
    clearTimeout(state.save.timer);
    state.save.timer = setTimeout(() => flushSave(), 400);
    renderSaveStatus();
  }

  async function flushSave() {
    clearTimeout(state.save.timer);
    clearTimeout(state.save.retry);
    while (state.save.inflight) await new Promise((resolve) => setTimeout(resolve, 50));
    if (!state.save.dirty || !state.labels) return;
    const doc = state.labels;
    const body = JSON.stringify(doc);
    state.save.inflight = true;
    state.save.dirty = false;
    renderSaveStatus();
    try {
      const url = `/api/labels/${enc(doc.job_name)}/${enc(doc.trial_name)}?labeler=${enc(doc.labeler)}`;
      const saved = await api(url, { method: "PUT", body });
      if (state.labels === doc && !state.save.dirty) {
        doc.updated_at = saved.updated_at;
        storageSet(pendingKey(doc), null);
      }
      state.save.error = null;
      state.save.savedAt = new Date();
    } catch (error) {
      state.save.error = error.message;
      if (state.labels === doc) state.save.dirty = true;
      state.save.retry = setTimeout(() => flushSave(), 5000);
    } finally {
      state.save.inflight = false;
      renderSaveStatus();
    }
  }

  function renderSaveStatus() {
    const el = $("#save-status");
    const s = state.save;
    if (!state.labels) {
      el.textContent = "";
      el.className = "save-status";
    } else if (s.error && s.dirty) {
      el.textContent = `Not saved: ${s.error}. Retrying…`;
      el.className = "save-status err";
    } else if (s.inflight || s.dirty) {
      el.textContent = "Saving…";
      el.className = "save-status";
    } else if (s.savedAt) {
      el.textContent = `Saved ${s.savedAt.toLocaleTimeString()}`;
      el.className = "save-status ok";
    } else {
      el.textContent = "Labels saved on the server";
      el.className = "save-status muted";
    }
  }

  window.addEventListener("beforeunload", (event) => {
    if (state.save.dirty || state.save.inflight) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ---------- routing ----------

  function parseRoute() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    return { job: parts[0] || null, trial: parts[1] || null, select: parts[2] || null };
  }

  // Each navigation gets a number; a slower, older one never paints over a newer one.
  let routeSeq = 0;
  const stale = (seq) => seq !== routeSeq;

  async function route() {
    const seq = ++routeSeq;
    await flushSave();
    if (stale(seq)) return;
    const r = parseRoute();
    $("#hints-toggle").hidden = !r.trial;
    state.notice = null;
    try {
      if (!state.meta) state.meta = await api("/api/meta");
      if (stale(seq)) return;
      if (r.trial) await showTrial(r.job, r.trial, r.select, seq);
      else {
        state.trace = null;
        state.labels = null;
        renderSaveStatus();
        if (r.job) await showJob(r.job, seq);
        else await showHome(seq);
      }
    } catch (error) {
      if (!stale(seq)) showError(error);
    }
  }

  function setCrumbs(items) {
    const nav = $("#crumbs");
    put(
      nav,
      ...items.flatMap((item) => [
        h("span", { class: "sep" }, "›"),
        item.href ? h("a", { href: item.href }, item.text) : h("span", { class: "here" }, item.text),
      ]),
    );
  }

  function showError(error) {
    const view = $("#view");
    view.className = "page";
    put(
      view,
      h("div", { class: "error" }, h("strong", null, "Could not load this page. "), error.message),
      h("p", null, h("a", { href: "#/" }, "Back to the experiments")),
    );
  }

  function labelerNotice() {
    if (state.labeler) return null;
    return h("p", { class: "notice" }, "Type your name in Labeler (top right) to see your progress and to label.");
  }

  function exportLinks(params, what) {
    const url = (format, extra) => `/api/export?${new URLSearchParams({ format, ...params, ...extra })}`;
    const link = (format, extra, text) =>
      h(
        "a",
        {
          href: url(format, extra),
          onclick: async (event) => {
            if (!state.save.dirty && !state.save.inflight) return;
            event.preventDefault();
            await flushSave();
            location.href = url(format, extra);
          },
        },
        text,
      );
    const mine = state.labeler ? { labeler: state.labeler } : null;
    return h(
      "div",
      { class: "export" },
      h("span", { class: "muted" }, `Download the labels for ${what}: `),
      link("csv", {}, "CSV"),
      " · ",
      link("json", {}, "JSON"),
      mine ? [h("span", { class: "muted" }, " · yours only: "), link("csv", mine, "CSV"), " · ", link("json", mine, "JSON")] : null,
    );
  }

  // ---------- experiments and trials ----------

  async function showHome(seq) {
    const query = state.labeler ? `?labeler=${enc(state.labeler)}` : "";
    const data = await api(`/api/jobs${query}`);
    if (stale(seq)) return;
    setCrumbs([]);
    const view = $("#view");
    view.className = "page";
    const head = ["Experiment", "Trials", "Finished", "Passed", "Content capture", "Labelers", "You finished"];
    const rows = data.jobs.map((job) =>
      h(
        "tr",
        null,
        h("td", null, h("a", { href: `#/${enc(job.name)}` }, job.name)),
        td(job.trials, "num"),
        td(job.finished, "num"),
        td(job.passed, "num"),
        td(job.extras ? `diffs in ${job.extras}` : job.captured ? "paths only" : "not captured"),
        td(job.labelers.length ? job.labelers.join(", ") : "–"),
        td(state.labeler ? `${job.done} of ${job.trials}` : "–", "num"),
      ),
    );
    put(
      view,
      h("h1", null, "Experiments"),
      h("p", { class: "muted" }, `Job dirs under ${data.root}. Labels are saved under ${state.meta.labels_dir}.`),
      labelerNotice(),
      data.jobs.length
        ? h("div", { class: "table-wrap" }, h("table", { class: "grid" }, h("thead", null, h("tr", null, head.map((t) => h("th", null, t)))), h("tbody", null, rows)))
        : h("p", null, "No job dirs found."),
      exportLinks({}, "every experiment"),
    );
  }

  async function showJob(job, seq) {
    const query = state.labeler ? `?labeler=${enc(state.labeler)}` : "";
    const data = await api(`/api/jobs/${enc(job)}${query}`);
    if (stale(seq)) return;
    setCrumbs([{ text: job }]);
    const view = $("#view");
    view.className = "page";
    const titles = Object.fromEntries(state.meta.patterns.map((p) => [p.key, p.title]));
    const head = ["Trial", "Task", "Reward", "Exception", "Agent min", "Hooked calls", "Checkpoints", "Timeouts", "Capture", "Trial label", "Steps judged", "Patterns found", "Done"];
    const rows = data.trials.map((t) => {
      const p = t.progress;
      const steps = t.hooked_calls + t.stops;
      return h(
        "tr",
        { class: p && p.done ? "done" : null },
        h("td", null, h("a", { href: `#/${enc(job)}/${enc(t.name)}` }, t.name)),
        td(t.task),
        h("td", null, t.reward === null || t.reward === undefined ? (t.finished ? "–" : t.died ? h("span", { class: "pill bad" }, "died") : h("span", { class: "pill warn" }, "no result yet")) : rewardPill(t.reward)),
        td(t.exception, t.exception ? "bad" : null),
        td(fmtMinutes(t.minutes), "num"),
        td(t.hooked_calls, "num"),
        td(t.checkpoints, "num"),
        td(t.timeouts, t.timeouts ? "num bad" : "num"),
        td(t.extras ? "diffs" : t.hooked_calls ? "paths only" : t.has_trajectory ? "none" : "–"),
        td(p ? (p.trial_judged ? "yes" : "–") : "–"),
        td(p ? `${p.steps_judged} of ${steps}` : "–", "num"),
        td(p && p.found.length ? p.found.map((k) => titles[k] || k).join(", ") : "–"),
        td(p ? (p.done ? "✓" : "") : "–"),
      );
    });
    put(
      view,
      h("h1", null, job),
      h("p", { class: "muted" }, `${plural(data.trials.length, "trial")}. Labelers so far: ${data.labelers.length ? data.labelers.join(", ") : "none"}.`),
      labelerNotice(),
      h("div", { class: "table-wrap" }, h("table", { class: "grid" }, h("thead", null, h("tr", null, head.map((t) => h("th", null, t)))), h("tbody", null, rows))),
      exportLinks({ job }, "this experiment"),
    );
  }

  // ---------- one trial ----------

  async function showTrial(job, trial, select, seq) {
    const labelsUrl = `/api/labels/${enc(job)}/${enc(trial)}?labeler=${enc(state.labeler)}`;
    const [trace, saved] = await Promise.all([
      api(`/api/trace/${enc(job)}/${enc(trial)}`),
      state.labeler ? api(labelsUrl) : Promise.resolve(null),
    ]);
    if (stale(seq)) return;
    setCrumbs([{ text: job, href: `#/${enc(job)}` }, { text: trial }]);
    state.job = job;
    state.trialName = trial;
    state.trace = trace;
    state.events = trace.events;
    state.labels = state.labeler ? restorePending(saved) : null;
    state.save.savedAt = null;
    state.save.error = null;
    if (state.hints) markHintsSeen();
    let index = -1;
    if (select && select.startsWith("@")) index = Number(select.slice(1));
    else if (select) index = state.events.findIndex((e) => e.id === select);
    state.selected = index >= 0 && index < state.events.length ? index : -1;
    renderTrial();
    renderSaveStatus();
  }

  function renderTrial() {
    const view = $("#view");
    view.className = "trial";
    put(
      view,
      h("div", { id: "trialbar", class: "trialbar" }),
      h("div", { id: "strip", class: "strip", role: "navigation", "aria-label": "Hooked calls" }),
      h(
        "div",
        { class: "panes" },
        h("aside", { class: "list-pane" }, listControls(), h("div", { id: "list", class: "list" })),
        h("section", { id: "detail", class: "detail", tabindex: "-1" }),
      ),
    );
    renderTrialBar();
    renderStrip();
    renderList();
    renderDetail();
  }

  function stepStats() {
    const steps = state.events.filter(isLabelable);
    const judged = steps.filter((e) => isJudged(e.id)).length;
    return { total: steps.length, judged };
  }

  function renderTrialBar() {
    const bar = $("#trialbar");
    if (!bar) return;
    const { trial, capture } = state.trace;
    const stats = stepStats();
    const counts = capture.counts;
    const changedCount = (counts.changed || 0) + (counts.checkpoint || 0);
    const callFacts = [`${changedCount} changed`, `${counts.unchanged || 0} no change`];
    if (counts.baseline) callFacts.push(`${counts.baseline} baseline`);
    if (counts.timeout || counts.missing) callFacts.push(`${(counts.timeout || 0) + (counts.missing || 0)} not captured`);
    put(
      bar,
      h("strong", { class: "trial-name" }, trial.task),
      rewardPill(trial.reward),
      trial.exception ? h("span", { class: "pill bad" }, trial.exception) : null,
      trial.died ? h("span", { class: "pill bad" }, "died before result.json") : !trial.finished ? h("span", { class: "pill warn" }, "not finished") : null,
      h("span", { class: "fact" }, `${fmtMinutes(trial.minutes)} min`),
      h("span", { class: "fact" }, `${plural(capture.hooked_calls, "hooked call")}: ${callFacts.join(", ")}`),
      h("span", { class: "fact" }, capture.extras ? "diffs recorded" : capture.captured ? "paths only (no --extras)" : "not captured"),
      state.labels ? h("span", { class: "fact progress" }, `judged ${stats.judged} of ${stats.total} steps${state.labels.done ? " · done ✓" : ""}`) : null,
      state.labels && state.labels.hints_seen ? h("span", { class: "pill warn", title: "Hints were shown while labeling this trial." }, "not blind") : null,
      state.trace.source === "hook_files"
        ? h("span", { class: "pill warn" }, state.events.length ? "no trajectory yet: calls from the hook's files, no messages" : "no trajectory and no hook records")
        : null,
      state.notice ? h("span", { class: "pill warn" }, state.notice) : null,
    );
  }

  function cellClass(e, index) {
    const label = getLabel(e.id);
    const marks = !label || !Array.isArray(label.patterns) ? "lab-open" : label.patterns.length ? "lab-yes" : "lab-no";
    return `cell st-${e.status} ${marks}${index === state.selected ? " selected" : ""}`;
  }

  function cellTitle(e) {
    const label = getLabel(e.id);
    const what = e.kind === "stop" ? "stop" : `#${e.n} ${e.tool}`;
    const judged = !label || !Array.isArray(label.patterns) ? "not judged" : label.patterns.length ? label.patterns.join(", ") : "none";
    return `${what} · ${e.status.replace("_", " ")} · ${judged}`;
  }

  function renderStrip() {
    const strip = $("#strip");
    cellEls = new Map();
    const cells = [];
    state.events.forEach((e, index) => {
      if (!isLabelable(e)) return;
      const cell = h("button", { class: cellClass(e, index), title: cellTitle(e), onclick: () => select(index) });
      cellEls.set(index, cell);
      cells.push(cell);
    });
    put(strip, cells.length ? cells : h("span", { class: "muted" }, "No hooked calls."));
  }

  function listControls() {
    const f = state.filters;
    const toggle = (name, text) => {
      const input = h("input", { type: "checkbox" });
      input.checked = f[name];
      input.addEventListener("change", () => {
        f[name] = input.checked;
        renderList();
      });
      return h("label", null, input, ` ${text}`);
    };
    const search = h("input", { type: "search", placeholder: "Filter: command, path, text, #12", value: f.search });
    search.addEventListener("input", () => {
      f.search = search.value;
      renderList();
    });
    return h(
      "div",
      { class: "list-controls" },
      search,
      h("div", { class: "toggles" }, toggle("messages", "messages"), toggle("notHooked", "not-hooked calls"), toggle("unlabeled", "only unjudged")),
    );
  }

  function visible(e) {
    const f = state.filters;
    if (e.kind === "message" && !f.messages) return false;
    if (e.kind === "call" && !e.hooked && !f.notHooked) return false;
    if (f.unlabeled && !(isLabelable(e) && !isJudged(e.id))) return false;
    if (f.search) {
      const needle = f.search.toLowerCase();
      const files = e.changes ? e.changes.files.map((x) => x.path) : (e.paths || []).map((x) => x.path);
      const hay = [e.summary, e.text, e.tool, e.id, e.n ? `#${e.n}` : "", ...files].join(" ").toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  }

  function changeCounts(e) {
    const ops = e.changes ? e.changes.files.map((x) => x.op) : (e.paths || []).map((x) => x.op);
    const total = e.changes ? e.changes.files_total : e.paths_total;
    const count = (op) => ops.filter((o) => o === op).length;
    const parts = [];
    if (count("added")) parts.push(`+${count("added")}`);
    if (count("modified")) parts.push(`~${count("modified")}`);
    if (count("removed")) parts.push(`−${count("removed")}`);
    if (total && total > ops.length) parts.push(`(${total})`);
    return parts.join(" ");
  }

  function statusChip(e) {
    switch (e.status) {
      case "changed":
        return h("span", { class: "chip st-changed" }, changeCounts(e) || "changed");
      case "baseline":
        return h("span", { class: "chip st-baseline" }, "baseline");
      case "checkpoint":
        return h("span", { class: "chip st-changed" }, "checkpoint");
      case "unchanged":
        return h("span", { class: "chip st-unchanged" }, "no change");
      case "deferred":
        return h("span", { class: "chip st-deferred" }, "deferred");
      case "timeout":
        return h("span", { class: "chip st-timeout" }, "timeout");
      case "missing":
        return h("span", { class: "chip st-timeout" }, "no record");
      default:
        return null;
    }
  }

  function labelChip(e) {
    if (!isLabelable(e) || !state.labels) return h("span", { class: "lab" });
    const label = getLabel(e.id);
    const extra = label ? `${label.unsure ? " ?" : ""}${label.note ? " ✎" : ""}` : "";
    if (!label || !Array.isArray(label.patterns)) return h("span", { class: "lab open", title: "not judged" }, `○${extra}`);
    if (!label.patterns.length) return h("span", { class: "lab no", title: "judged: none" }, `✓${extra}`);
    const titles = Object.fromEntries(state.meta.patterns.map((p) => [p.key, p.title]));
    return h("span", { class: "lab yes", title: label.patterns.map((k) => titles[k]).join(", ") }, `${label.patterns.length === 1 ? titles[label.patterns[0]] : `${label.patterns.length} patterns`}${extra}`);
  }

  function rowFor(e, index) {
    let cells;
    if (e.kind === "message") cells = [h("span", { class: "num" }, "said"), h("span", { class: "sum" }, e.text || "(thinking only)"), h("span")];
    else if (e.kind === "user") cells = [h("span", { class: "num" }, "user"), h("span", { class: "sum" }, e.text), h("span")];
    else if (e.kind === "stop") cells = [h("span", { class: "num" }, "stop"), h("span", { class: "sum" }, "end of the agent's turn"), h("span", { class: "marks" }, statusChip(e), labelChip(e))];
    else
      cells = [
        h("span", { class: "num" }, `#${e.n}`),
        h("span", { class: "sum" }, h("span", { class: "tool" }, e.tool), " ", e.summary),
        h("span", { class: "marks" }, statusChip(e), labelChip(e)),
      ];
    const classes = ["row", `kind-${e.kind}`, e.kind === "call" && !e.hooked ? "not-hooked" : "", index === state.selected ? "selected" : ""];
    return h("div", { class: classes.join(" "), onclick: () => select(index) }, cells);
  }

  function renderList() {
    const list = $("#list");
    if (!list) return;
    rowEls = new Map();
    const overview = h(
      "div",
      { class: `row kind-trial${state.selected === -1 ? " selected" : ""}`, onclick: () => select(-1) },
      h("span", { class: "num" }, "trial"),
      h("span", { class: "sum" }, "Overview and whole-trial label"),
      h("span", { class: "marks" }, state.labels ? h("span", { class: `lab ${isJudged(TRIAL_KEY) ? (getLabel(TRIAL_KEY).patterns.length ? "yes" : "no") : "open"}` }, isJudged(TRIAL_KEY) ? "judged" : "○") : null),
    );
    rowEls.set(-1, overview);
    const rows = [overview];
    state.events.forEach((e, index) => {
      if (!visible(e)) return;
      const row = rowFor(e, index);
      rowEls.set(index, row);
      rows.push(row);
    });
    if (rows.length === 1 && state.events.length) rows.push(h("p", { class: "muted pad" }, "Nothing matches the filter."));
    put(list, rows);
  }

  function refreshMarks(key) {
    if (key === TRIAL_KEY) {
      renderList();
    } else {
      const index = state.events.findIndex((e) => e.id === key);
      const e = state.events[index];
      const row = rowEls.get(index);
      if (row) {
        const old = row.querySelector(".lab");
        if (old) old.replaceWith(labelChip(e));
      }
      const cell = cellEls.get(index);
      if (cell) {
        cell.className = cellClass(e, index);
        cell.title = cellTitle(e);
      }
    }
    renderTrialBar();
  }

  function select(index, scroll = true) {
    state.selected = index;
    const e = state.events[index];
    const tail = index === -1 ? "" : `/${enc(e.id || `@${index}`)}`;
    history.replaceState(null, "", `#/${enc(state.job)}/${enc(state.trialName)}${tail}`);
    rowEls.forEach((row, i) => row.classList.toggle("selected", i === index));
    cellEls.forEach((cell, i) => cell.classList.toggle("selected", i === index));
    renderDetail();
    if (scroll) {
      const row = rowEls.get(index);
      if (row) row.scrollIntoView({ block: "nearest" });
      const cell = cellEls.get(index);
      if (cell) cell.scrollIntoView({ block: "nearest", inline: "nearest" });
    }
  }

  function visibleIndexes() {
    return [-1, ...state.events.map((e, i) => i).filter((i) => visible(state.events[i]))];
  }

  function moveRow(delta) {
    const order = visibleIndexes();
    const at = order.indexOf(state.selected);
    const next = order[Math.min(order.length - 1, Math.max(0, at + delta))];
    if (next !== undefined) select(next);
  }

  function moveStep(delta) {
    const steps = state.events.map((e, i) => i).filter((i) => isLabelable(state.events[i]));
    if (!steps.length) return;
    let next;
    if (delta > 0) next = steps.find((i) => i > state.selected);
    else next = [...steps].reverse().find((i) => i < state.selected);
    if (next === undefined) next = delta > 0 ? steps[steps.length - 1] : -1;
    select(next);
  }

  function nextUnjudged() {
    const steps = state.events.map((e, i) => i).filter((i) => isLabelable(state.events[i]) && !isJudged(state.events[i].id));
    if (!steps.length) return;
    select(steps.find((i) => i > state.selected) ?? steps[0]);
  }

  // ---------- detail ----------

  function section(title, ...content) {
    return h("section", { class: "sec" }, h("h3", null, title), ...content);
  }

  const note = (text) => h("p", { class: "note muted" }, text);

  function expandable(text, cls, lines = 24) {
    const pre = h("pre", { class: cls });
    pre.textContent = text;
    const count = text.split("\n").length;
    if (count <= lines) return pre;
    pre.classList.add("clipped");
    const button = h("button", { class: "link", onclick: () => {
      pre.classList.toggle("clipped");
      button.textContent = pre.classList.contains("clipped") ? `Show all ${count} lines` : "Show less";
    } }, `Show all ${count} lines`);
    return h("div", null, pre, button);
  }

  function lineClass(line) {
    if (line.startsWith("+++") || line.startsWith("---")) return "hdr";
    if (line.startsWith("@@")) return "hunk";
    if (line.startsWith("+")) return "add";
    if (line.startsWith("-")) return "del";
    if (line.startsWith("[...")) return "cut";
    return "ctx";
  }

  function diffView(lines, max = 400) {
    const pre = h("pre", { class: "diff" });
    const fill = (limit) => {
      pre.replaceChildren(...lines.slice(0, limit).map(([cls, text]) => h("span", { class: cls }, `${text}\n`)));
    };
    fill(max);
    if (lines.length <= max) return pre;
    const button = h("button", { class: "link", onclick: () => {
      fill(lines.length);
      button.remove();
    } }, `Show all ${lines.length} lines`);
    return h("div", null, pre, button);
  }

  const unifiedLines = (text) => text.split("\n").map((line) => [lineClass(line), line]);

  // A line diff of an Edit's old and new strings (longest common subsequence).
  function editLines(before, after) {
    const x = (before || "").split("\n");
    const y = (after || "").split("\n");
    if (x.length * y.length > 250000) return [...x.map((l) => ["del", `-${l}`]), ...y.map((l) => ["add", `+${l}`])];
    const n = x.length;
    const m = y.length;
    const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
    const out = [];
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (x[i] === y[j]) {
        out.push(["ctx", ` ${x[i]}`]);
        i++;
        j++;
      } else if (dp[i + 1][j] >= dp[i][j + 1]) out.push(["del", `-${x[i++]}`]);
      else out.push(["add", `+${y[j++]}`]);
    }
    while (i < n) out.push(["del", `-${x[i++]}`]);
    while (j < m) out.push(["add", `+${y[j++]}`]);
    return out;
  }

  function pathLine(path, extra) {
    return h("div", { class: "pathline" }, h("code", null, path || "(no path)"), extra ? h("span", { class: "muted" }, ` ${extra}`) : null);
  }

  function inputView(e) {
    const a = e.input || {};
    switch (e.tool) {
      case "Bash":
        return [
          a.description ? h("div", { class: "intent" }, h("span", { class: "muted" }, "Says it will: "), a.description) : null,
          h("pre", { class: "command" }, a.command || ""),
          a.run_in_background ? h("span", { class: "pill warn" }, "runs in the background") : null,
          a.timeout ? h("span", { class: "pill" }, `timeout ${fmtMs(a.timeout)}`) : null,
        ];
      case "Edit":
        return [pathLine(a.file_path, a.replace_all ? "(replace all)" : null), diffView(editLines(a.old_string, a.new_string))];
      case "MultiEdit":
        return [
          pathLine(a.file_path, `${plural((a.edits || []).length, "edit")}`),
          ...(a.edits || []).map((edit, k) => [h("div", { class: "sub muted" }, `edit ${k + 1}${edit.replace_all ? " (replace all)" : ""}`), diffView(editLines(edit.old_string, edit.new_string))]),
        ];
      case "Write": {
        const content = a.content || "";
        return [pathLine(a.file_path, `${plural(content.split("\n").length, "line")}`), expandable(content, "content", 30)];
      }
      case "NotebookEdit":
        return [pathLine(a.notebook_path, [a.edit_mode, a.cell_type, a.cell_id].filter(Boolean).join(" · ")), expandable(a.new_source || "", "content", 30)];
      default:
        return Object.keys(a).length ? expandable(JSON.stringify(a, null, 2), "content", 20) : note("(no input recorded)");
    }
  }

  function outputView(e) {
    const badges = [
      e.exit_code !== null && e.exit_code !== undefined ? h("span", { class: `pill ${e.exit_code === 0 ? "ok" : "bad"}` }, `exit ${e.exit_code}`) : null,
      e.is_error ? h("span", { class: "pill bad" }, "tool reported an error") : null,
      e.output_cut ? h("span", { class: "pill warn" }, "middle cut for length") : null,
    ];
    return [h("div", { class: "badges" }, badges), e.output ? expandable(e.output, "output", 24) : note("(no output)")];
  }

  const STATUS_TEXT = {
    changed: "Changed the filesystem.",
    baseline: "First measured call (baseline): the watcher had nothing earlier to compare with, so no change is listed.",
    checkpoint: "A checkpoint was taken (no change detector in this image).",
    unchanged: "No change on disk: the filesystem equals the last checkpoint's.",
    deferred: "Deferred: a later checkpoint covers this call (every-N).",
    timeout: "The hook timed out waiting for the watcher: this call's state was not captured.",
    missing: "The watcher left no record for this call.",
    uncaptured: "This trial ran without the checkpoint watcher: nothing was measured.",
    not_hooked: "Not hooked: the hook does not see this tool, so nothing was measured.",
  };

  const SKIPPED_TEXT = {
    not_text: "binary or not UTF-8, so no diff",
    too_large: "over 1 MiB, so not read",
    too_many_lines: "too many lines to diff",
    over_file_cap: "past the 20 files read per call",
    over_budget: "earlier version not read (2 s budget)",
    unreadable: "could not be read",
  };

  const OP_SIGN = { added: "+", modified: "~", removed: "−" };

  function fileHistory(path, currentId) {
    const versions = (state.trace.files[path] || []).filter((v) => v.call !== null && v.call !== undefined);
    if (versions.length < 2) return null;
    const items = versions.map((v) =>
      h(
        "li",
        { class: v.id === currentId ? "current" : null },
        h("button", { class: "link", onclick: () => selectId(v.id) }, `#${v.call}`),
        ` ${v.op}`,
        v.size !== null && v.size !== undefined ? ` · ${fmtBytes(v.size)}` : "",
        v.sha256 ? h("code", { class: "hash" }, ` ${shortHash(v.sha256)}`) : "",
      ),
    );
    return h("details", { class: "history" }, h("summary", null, `Changed by ${plural(versions.length, "call")} in this trial`), h("ol", null, items));
  }

  function fileView(f, e, open) {
    const meta = [
      f.kind === "symlink" ? "symlink" : null,
      f.size !== null && f.size !== undefined ? fmtBytes(f.size) : null,
      f.before_sha256 || f.sha256 ? `${shortHash(f.before_sha256) || "new"} → ${shortHash(f.sha256) || (f.op === "removed" ? "gone" : "?")}` : null,
    ].filter(Boolean);
    const head = h(
      "div",
      { class: "file-head" },
      h("span", { class: `op op-${f.op}` }, OP_SIGN[f.op] || "?"),
      h("code", { class: "path" }, f.path),
      meta.length ? h("span", { class: "muted" }, meta.join(" · ")) : null,
      f.skipped ? h("span", { class: "pill warn" }, SKIPPED_TEXT[f.skipped] || f.skipped) : null,
    );
    let body = null;
    if (f.diff) {
      const lines = unifiedLines(f.diff);
      const details = h("details", { class: "diff-box" }, h("summary", null, `diff · ${plural(lines.length, "line")}`), diffView(lines));
      if (open) details.open = true;
      body = details;
    }
    return h("div", { class: "file" }, head, body, fileHistory(f.path, e.id));
  }

  function changesView(e) {
    const out = [h("p", { class: `status st-${e.status}` }, STATUS_TEXT[e.status] || e.status)];
    const facts = [];
    if (e.checkpoint) facts.push(`checkpoint #${e.checkpoint.seq}${e.checkpoint.bytes ? ` (${fmtBytes(e.checkpoint.bytes)} layer)` : ""}, ${fmtMs(e.checkpoint.capture_ms)} to save`);
    else if (e.record && e.record.checkpoint_seq) facts.push(`state as in checkpoint #${e.record.checkpoint_seq}`);
    if (e.pause_ms !== null && e.pause_ms !== undefined) facts.push(`agent paused ${fmtMs(e.pause_ms)}`);
    if (e.record && e.record.tool_failed) facts.push("the call failed");
    if (facts.length) out.push(h("p", { class: "muted small" }, facts.join(" · ")));
    if (e.unmatched) out.push(note("This hooked call is not in the trajectory (a subagent's call?)."));
    if (e.changes) {
      const files = e.changes.files;
      if (e.changes.baseline) out.push(note("No file list for the baseline call."));
      else if (!files.length && e.status === "changed") out.push(note("Only directories changed."));
      const open = files.length <= 4;
      out.push(...files.map((f) => fileView(f, e, open && f.diff && f.diff.split("\n").length <= 80)));
      if (e.changes.files_total > files.length) out.push(note(`${e.changes.files_total - files.length} more changed paths were not recorded.`));
    } else if (e.paths && e.paths.length) {
      out.push(note("Paths only: this trial was captured without --extras, so there are no contents or diffs."));
      out.push(h("ul", { class: "paths" }, e.paths.map((p) => h("li", null, h("span", { class: `op op-${p.op}` }, OP_SIGN[p.op] || "?"), h("code", null, p.path), fileHistory(p.path, e.id)))));
      if (e.paths_total && e.paths_total > e.paths.length) out.push(note(`${e.paths_total - e.paths.length} more changed paths were not listed.`));
    }
    return out;
  }

  function processesView(p) {
    if (!p) return null;
    const items = [
      ...p.started.map((cmd) => h("li", { class: "add" }, h("span", { class: "muted" }, "started "), h("code", null, cmd))),
      ...p.stopped.map((cmd) => h("li", { class: "del" }, h("span", { class: "muted" }, "stopped "), h("code", null, cmd))),
      ...p.opened.map((x) => h("li", { class: "add" }, `opened port ${x.proto} ${x.port}`)),
      ...p.closed.map((x) => h("li", { class: "del" }, `closed port ${x.proto} ${x.port}`)),
    ];
    return section(
      "Processes and ports",
      items.length ? h("ul", { class: "procs" }, items) : note("No agent process or port started or stopped."),
      h(
        "details",
        { class: "running" },
        h("summary", null, `Running after this call: ${p.running.length} agent process${p.running.length === 1 ? "" : "es"}, ${plural(p.listening.length, "listening port")}`),
        h("ul", { class: "procs" }, p.running.map((cmd) => h("li", null, h("code", null, cmd))), p.listening.map((x) => h("li", null, `listening ${x.proto} ${x.port}`))),
      ),
    );
  }

  function selectId(id) {
    const index = state.events.findIndex((e) => e.id === id);
    if (index >= 0) select(index);
  }

  function selectCall(n) {
    const index = state.events.findIndex((e) => e.kind === "call" && e.n === n);
    if (index >= 0) select(index);
  }

  const callLink = (n) => h("button", { class: "link", onclick: () => selectCall(n) }, `#${n}`);
  const callLinks = (ns) => ns.flatMap((n, i) => [i ? ", " : "", callLink(n)]);

  function callHints(e) {
    if (!state.hints || !e.hints) return null;
    const items = [];
    const r = e.hints.repeat;
    if (r) items.push(h("li", null, "Same command as ", callLink(r.of), ` (${ordinal(r.count)} time); ${r.changed_between ? "files changed in between" : "no file changed in between"}.`));
    for (const v of e.hints.reverts || []) {
      items.push(h("li", null, h("code", null, v.path), v.to === "original" ? " is back to its content before the trial changed it." : [" is back to its content after ", callLink(v.to), "."]));
    }
    if (!items.length) return null;
    return h("div", { class: "hint" }, h("strong", null, "Hints "), h("ul", null, items));
  }

  function messageHints(e) {
    if (!state.hints || !e.hints) return null;
    const items = [];
    for (const m of e.hints.mentions || []) {
      const before = m.calls.filter((n) => n <= e.after_call);
      const after = m.calls.filter((n) => n > e.after_call);
      const where = m.calls.length
        ? [before.length ? ["changed by ", callLinks(before)] : "not changed before this message", after.length ? ["; later by ", callLinks(after)] : ""]
        : "not changed by any captured call";
      items.push(h("li", null, h("code", null, m.token), ": ", where));
    }
    const c = e.hints.claim;
    if (c) {
      let test = "no test-like command ran before it";
      if (c.test_call) {
        const code = c.test_exit_code === null ? "exit code unknown" : `exit ${c.test_exit_code}`;
        const since = c.changed_since.length ? ["; files changed after it by ", callLinks(c.changed_since)] : "; no file changed after it";
        test = ["the last test-like command before it was ", callLink(c.test_call), ` (${code})`, since];
      }
      items.push(h("li", null, "Sounds like a success claim: ", test, "."));
    }
    if (!items.length) return null;
    return h("div", { class: "hint" }, h("strong", null, "Hints "), h("ul", null, items));
  }

  function ordinal(n) {
    const s = ["th", "st", "nd", "rd"];
    const v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }

  function messageBlock(e) {
    if (e.kind === "user") return h("div", { class: "said user" }, h("div", { class: "who" }, "user turn"), e.text);
    return h(
      "div",
      { class: "said-wrap" },
      e.text ? h("div", { class: "said" }, e.text) : null,
      e.reasoning ? h("details", { class: "thinking" }, h("summary", null, "Thinking"), h("pre", null, e.reasoning)) : null,
      messageHints(e),
    );
  }

  function saidBefore(index) {
    const messages = [];
    const between = [];
    const stopAtAnyCall = !isLabelable(state.events[index]);
    for (let i = index - 1; i >= 0; i--) {
      const e = state.events[i];
      if (isLabelable(e) || (stopAtAnyCall && e.kind === "call")) break;
      if (e.kind === "call") between.unshift(e);
      else messages.unshift(e);
    }
    return { messages, between };
  }

  function callHeader(e) {
    const title = e.kind === "stop" ? "Stop: the agent ended its turn" : `#${e.n} ${e.tool}`;
    return h(
      "div",
      { class: "detail-head" },
      h("h2", null, title),
      h("button", { class: "id", title: "Copy the tool_call_id", onclick: () => navigator.clipboard && navigator.clipboard.writeText(e.id) }, e.id),
      h("span", { class: "muted" }, [fmtTime(e.t), e.record ? `call_seq ${e.record.call_seq}` : null, e.step_id ? `step ${e.step_id}` : null].filter(Boolean).join(" · ")),
    );
  }

  function callDetail(e, index) {
    const { messages, between } = saidBefore(index);
    const agent = [
      section(
        isLabelable(e) ? "Said since the previous hooked call" : "Said just before",
        messages.length ? messages.map(messageBlock) : note("(nothing)"),
        between.length
          ? h("div", { class: "between muted" }, "Also ran, not hooked: ", between.flatMap((c, i) => [i ? ", " : "", h("button", { class: "link", onclick: () => select(state.events.indexOf(c)) }, `#${c.n} ${c.tool}`)]))
          : null,
      ),
    ];
    if (e.kind === "call") agent.push(section("Ran", inputView(e)), section("Saw", outputView(e)));
    const env = [section("What changed", changesView(e)), processesView(e.processes), callHints(e)];
    return [
      callHeader(e),
      h("div", { class: "cols" }, h("div", { class: "col agent" }, h("div", { class: "col-title" }, "Agent: said, ran, saw"), agent), h("div", { class: "col env" }, h("div", { class: "col-title" }, "Environment: what actually changed"), env)),
    ];
  }

  function messageDetail(e, index) {
    let next = null;
    for (let i = index + 1; i < state.events.length; i++) {
      if (state.events[i].kind === "call" || state.events[i].kind === "stop") {
        next = state.events[i];
        break;
      }
    }
    return [
      h("div", { class: "detail-head" }, h("h2", null, e.kind === "user" ? "User turn" : "Agent message"), h("span", { class: "muted" }, [fmtTime(e.t), e.step_id ? `step ${e.step_id}` : null].filter(Boolean).join(" · "))),
      messageBlock(e),
      next ? h("p", { class: "muted" }, "Labels go on the next hooked call or stop, where the detector reads this message: ", h("button", { class: "link", onclick: () => select(state.events.indexOf(next)) }, next.kind === "stop" ? "the stop" : `#${next.n} ${next.tool}`)) : null,
    ];
  }

  function trialDetail() {
    const { trial, capture, checks } = state.trace;
    const facts = [
      ["Trial", trial.name],
      ["Task", trial.task],
      ["Reward", trial.reward === null || trial.reward === undefined ? "–" : String(trial.reward)],
      ["Exception", trial.exception || "–"],
      ["Agent time", `${fmtMinutes(trial.minutes)} min`],
      ["Model", trial.model || "–"],
      ["Claude Code", trial.agent_version || "–"],
      ["API cost", trial.cost_usd === null || trial.cost_usd === undefined ? "–" : `$${trial.cost_usd.toFixed(2)}`],
      ["Hooked calls", String(capture.hooked_calls)],
      ["Checkpoints", String(capture.checkpoints)],
      ["Timeouts", String(capture.timeouts)],
      ["Capture", capture.extras ? "diffs and processes (--extras)" : capture.captured ? "changed paths only" : "none"],
      ["Gate", capture.policy ? `${capture.policy.gate}, every ${capture.policy.every}` : "–"],
      ["Harness ports", capture.harness_ports.length ? capture.harness_ports.join(", ") : "–"],
    ];
    const stats = stepStats();
    const found = {};
    for (const label of Object.values((state.labels && state.labels.steps) || {})) for (const p of label.patterns || []) found[p] = (found[p] || 0) + 1;
    const titles = Object.fromEntries(state.meta.patterns.map((p) => [p.key, p.title]));
    const doneBox = h("input", { type: "checkbox", onchange: (event) => setDone(event.target.checked) });
    doneBox.checked = !!(state.labels && state.labels.done);
    return [
      h("div", { class: "detail-head" }, h("h2", null, "Trial overview")),
      h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", null, k), h("dd", null, v)])),
      trial.exception_message ? section("Exception", h("pre", { class: "output" }, trial.exception_message)) : null,
      checks
        ? section(
            `Verifier: ${checks.passed} of ${checks.total} tests passed`,
            checks.failed.length
              ? h("details", { class: "checks-list" }, h("summary", null, `${plural(checks.failed.length, "failed test")}`), h("ul", null, checks.failed.map((t) => h("li", null, h("code", null, t.name), t.message ? h("pre", { class: "output small" }, t.message) : null))))
              : null,
          )
        : null,
      trial.instruction ? section("Task instruction", h("details", null, h("summary", null, "Show the instruction"), h("pre", { class: "content" }, trial.instruction))) : null,
      !state.events.length ? note("No trajectory and no hook records: this trial did not get far.") : null,
      state.labels
        ? section(
            "Your step labels",
            h("p", null, `Judged ${stats.judged} of ${stats.total} steps.`),
            Object.keys(found).length ? h("p", null, "Patterns found: ", Object.entries(found).map(([k, n]) => `${titles[k]} ×${n}`).join(", ")) : null,
            h("label", { class: "done-toggle" }, doneBox, " I finished labeling this trial"),
          )
        : null,
    ];
  }

  function labelPanel(key, title) {
    if (!state.labeler) return h("div", { class: "labelpanel", id: "labelpanel" }, h("p", { class: "muted" }, "Type your name in Labeler (top right) to label."));
    const label = getLabel(key);
    const judged = !!label && Array.isArray(label.patterns);
    const chosen = new Set((label && label.patterns) || []);
    const box = (hint, text, on, onToggle, help, kind) => {
      const input = h("input", { type: "checkbox", onchange: onToggle });
      input.checked = on;
      return h("label", { class: `check ${kind}${on ? " on" : ""}`, title: help }, input, h("span", null, text), h("kbd", null, hint));
    };
    const patterns = state.meta.patterns.map((p, i) => box(String(i + 1), p.title, chosen.has(p.key), () => togglePattern(key, p.key), p.help, "pattern"));
    const none = box("0", "None of these", judged && chosen.size === 0, () => markNone(key), "Judged: no pattern occurred here.", "none");
    const unsure = box("u", "Unsure", !!(label && label.unsure), () => changeLabel(key, { unsure: !(label && label.unsure) }), "Mark this judgment for a second look.", "unsure");
    const text = h("textarea", { class: "note-field", rows: 2, placeholder: "Notes: what you compared, why you judged it so…", maxlength: 20000 });
    text.value = (label && label.note) || "";
    text.addEventListener("input", () => changeLabel(key, { note: text.value }, false));
    const verdict = !judged ? "not judged" : chosen.size ? `judged: ${[...chosen].length} pattern${chosen.size === 1 ? "" : "s"}` : "judged: none";
    return h(
      "div",
      { class: "labelpanel", id: "labelpanel" },
      h("div", { class: "lp-head" }, h("strong", null, title), h("span", { class: `verdict ${judged ? (chosen.size ? "yes" : "no") : "open"}` }, verdict), h("span", { class: "keys muted" }, "j/k step · 1–8 toggle · 0 none · u unsure · n note · ] next unjudged")),
      h("div", { class: "checks" }, patterns, h("span", { class: "divider" }), none, unsure),
      text,
    );
  }

  function rerenderLabelPanel(key) {
    if (keyOf(state.selected) !== key) return;
    const old = $("#labelpanel");
    if (old) old.replaceWith(labelPanel(key, panelTitle()));
  }

  function panelTitle() {
    if (state.selected === -1) return "Whole trial: which patterns occurred anywhere in it?";
    const e = state.events[state.selected];
    return e.kind === "stop" ? "Your label for the stop" : `Your label for #${e.n}`;
  }

  function renderDetail() {
    const pane = $("#detail");
    if (!pane) return;
    const e = state.events[state.selected];
    let body;
    if (state.selected === -1 || !e) body = trialDetail();
    else if (e.kind === "message" || e.kind === "user") body = messageDetail(e, state.selected);
    else body = callDetail(e, state.selected);
    const labelable = state.selected === -1 || isLabelable(e);
    put(pane, h("div", { class: "detail-body" }, body), labelable ? labelPanel(keyOf(state.selected), panelTitle()) : null);
    pane.scrollTop = 0;
  }

  // ---------- keys ----------

  document.addEventListener("keydown", (event) => {
    if (!state.trace || !$("#detail")) return;
    const target = event.target;
    const typing = target instanceof HTMLTextAreaElement || (target instanceof HTMLInputElement && target.type !== "checkbox");
    if (typing) {
      if (event.key === "Escape") target.blur();
      return;
    }
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const e = state.events[state.selected];
    const key = state.selected === -1 ? TRIAL_KEY : isLabelable(e) ? e.id : null;
    const canLabel = key !== null && state.labels !== null;
    if (event.key === "j") moveStep(1);
    else if (event.key === "k") moveStep(-1);
    else if (event.key === "ArrowDown") moveRow(1);
    else if (event.key === "ArrowUp") moveRow(-1);
    else if (event.key === "]") nextUnjudged();
    else if (event.key === "0" && canLabel) markNone(key);
    else if (event.key === "u" && canLabel) changeLabel(key, { unsure: !(getLabel(key) && getLabel(key).unsure) });
    else if (event.key === "n" && canLabel) {
      const field = $("#labelpanel textarea");
      if (field) field.focus();
    } else if (/^[1-9]$/.test(event.key) && canLabel && Number(event.key) <= state.meta.patterns.length) togglePattern(key, state.meta.patterns[Number(event.key) - 1].key);
    else return;
    event.preventDefault();
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
    storageSet("trajlab.labeler", name || null);
    route();
  });

  const hintsInput = $("#hints");
  hintsInput.addEventListener("change", () => {
    state.hints = hintsInput.checked;
    if (state.hints) markHintsSeen();
    if (state.trace && $("#detail")) {
      renderTrialBar();
      renderDetail();
    }
  });

  window.addEventListener("hashchange", route);
  route();
})();
