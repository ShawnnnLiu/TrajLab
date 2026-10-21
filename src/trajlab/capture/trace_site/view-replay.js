// The Replay tab: the run as an IDE you can scrub through. At the selected call the explorer
// shows every file the agent changed (A/M/D as of then), the editor shows a file's diff or its
// whole content after that call, the terminal shows the history up to it, and a panel shows the
// processes and open ports.

import {
  $, api, enc, fmtTime, h, icon, isLabelable, note, put, state, storageGet, storageSet, toolIcon, trialHref,
} from "./core.js";
import { highlight } from "./code.js";
import { diffStats, diffView, fileView } from "./diff.js";
import { getLabel, labelKey, labelPanel, verdictOf } from "./labels.js";
import { statusChip } from "./view-trace.js";

const NOISY = /\/(site-packages|dist-packages|node_modules|__pycache__|\.cache|\.git|\.npm|\.cargo|\.venv|venv)(\/|$)/;
const OP = { added: "A", modified: "M", removed: "D" };

let calls = [];
let ui = null;

export function renderReplay(body, select) {
  calls = state.events.map((e, i) => ({ e, i })).filter(({ e }) => e.kind === "call" || e.kind === "stop");
  if (!calls.length) {
    put(body, h("div", { class: "empty" }, icon("layout"), h("p", null, "No calls to replay in this trial.")));
    return;
  }
  const r = (state.replay = state.replay || {
    pos: 0,
    open: [],
    active: null,
    mode: "diff",
    showFuture: true,
    showMessages: true,
    collapsed: null,
    files: new Map(),
    labelsOpen: storageGet("trajlab.replay.labels") === "open",
    follow: true,
    timer: null,
  });
  if (select) {
    const at = calls.findIndex((c) => c.e.id === select);
    if (at >= 0) r.pos = at;
  }
  r.pos = Math.min(r.pos, calls.length - 1);
  if (r.collapsed === null) r.collapsed = defaultCollapsed();
  ui = {
    info: h("div", { class: "scrub-info" }),
    range: h("input", { type: "range", class: "scrub-range", min: 0, max: calls.length - 1, step: 1 }),
    ticks: h("div", { class: "scrub-ticks" }),
    play: h("button", { class: "icon-btn", title: "Play or pause (space)" }, icon(state.replay.timer ? "pause" : "play")),
    explorer: h("div", { class: "pane-body explorer-body" }),
    editorTabs: h("div", { class: "editor-tabs" }),
    editor: h("div", { class: "pane-body editor-body" }),
    terminal: h("div", { class: "pane-body terminal-body" }),
    procs: h("div", { class: "pane-body procs-body" }),
    labels: h("div", { class: "replay-labels" }),
    entries: [],
  };
  ui.range.addEventListener("input", () => go(Number(ui.range.value)));
  ui.play.addEventListener("click", togglePlay);
  const button = (name, title, fn) => h("button", { class: "icon-btn", title, onclick: fn }, icon(name));
  const futureToggle = h("input", { type: "checkbox" });
  futureToggle.checked = r.showFuture;
  futureToggle.addEventListener("change", () => {
    r.showFuture = futureToggle.checked;
    renderExplorer();
  });
  const messagesToggle = h("input", { type: "checkbox" });
  messagesToggle.checked = r.showMessages;
  messagesToggle.addEventListener("change", () => {
    r.showMessages = messagesToggle.checked;
    buildTerminal();
    updateTerminal();
  });
  put(
    body,
    h(
      "div",
      { class: "replay" },
      h(
        "div",
        { class: "scrubber card" },
        h("div", { class: "scrub-controls" }, button("skipBack", "First call (Home)", () => go(0)), button("chevronLeft", "Previous call (←)", () => go(r.pos - 1)), ui.play, button("chevronRight", "Next call (→)", () => go(r.pos + 1)), button("skipForward", "Last call (End)", () => go(calls.length - 1))),
        h("div", { class: "scrub-track" }, ui.ticks, ui.range),
        ui.info,
      ),
      h(
        "div",
        { class: "ide" },
        h("div", { class: "pane explorer" }, h("div", { class: "pane-head" }, icon("folder"), "Explorer", h("span", { class: "spacer" }), h("label", { class: "toggle small", title: "Show files that a later call changes" }, futureToggle, h("span", null, "later"))), ui.explorer),
        h("div", { class: "pane editor" }, h("div", { class: "pane-head" }, ui.editorTabs), ui.editor),
        h("div", { class: "pane terminal" }, h("div", { class: "pane-head" }, icon("terminal"), "Terminal", h("span", { class: "muted small" }, "history up to this call"), h("span", { class: "spacer" }), h("label", { class: "toggle small" }, messagesToggle, h("span", null, "agent messages"))), ui.terminal),
        h("div", { class: "pane procs" }, h("div", { class: "pane-head" }, icon("cpu"), "Processes and ports"), ui.procs),
      ),
      ui.labels,
    ),
  );
  renderTicks();
  buildTerminal();
  go(r.pos, false);
  state.keyHandler = replayKeys;
  state.onHints = () => renderInfo();
  state.onLabelsChangeTab = () => {
    renderTicks();
    renderInfo();
  };
}

function defaultCollapsed() {
  const collapsed = new Set();
  const counts = new Map();
  for (const path of Object.keys(state.trace.files)) {
    const parts = path.split("/").filter(Boolean);
    for (let k = 1; k < parts.length; k++) {
      const dir = `/${parts.slice(0, k).join("/")}`;
      counts.set(dir, (counts.get(dir) || 0) + 1);
    }
  }
  for (const [dir, n] of counts) if (NOISY.test(dir) || (n > 40 && dir.split("/").length > 3)) collapsed.add(dir);
  return collapsed;
}

const current = () => calls[state.replay.pos];

function go(pos, push = true) {
  const r = state.replay;
  r.pos = Math.max(0, Math.min(calls.length - 1, pos));
  ui.range.value = String(r.pos);
  const { e } = current();
  if (push) history.replaceState(null, "", `#/trial/${enc(state.job)}/${enc(state.trialName)}/replay/${enc(e.id)}`);
  // With "follow" on, the editor opens the files each call changes.
  const changedNow = changedPaths(e);
  if (r.follow && changedNow.length && !changedNow.includes(r.active)) openFile(changedNow.find((p) => !NOISY.test(p)) || changedNow[0], false);
  renderInfo();
  markTicks();
  renderExplorer();
  renderEditor();
  updateTerminal();
  renderProcs();
  renderLabels();
}

function togglePlay() {
  const r = state.replay;
  if (r.timer) {
    clearInterval(r.timer);
    r.timer = null;
  } else {
    if (r.pos >= calls.length - 1) go(0);
    r.timer = setInterval(() => {
      if (!document.body.contains(ui.range) || r.pos >= calls.length - 1) {
        clearInterval(r.timer);
        r.timer = null;
        if (document.body.contains(ui.play)) put(ui.play, icon("play"));
        return;
      }
      go(r.pos + 1);
    }, 900);
  }
  put(ui.play, icon(r.timer ? "pause" : "play"));
}

function replayKeys(event) {
  const r = state.replay;
  const k = event.key;
  if (k === "ArrowRight") go(r.pos + 1);
  else if (k === "ArrowLeft") go(r.pos - 1);
  else if (k === "Home") go(0);
  else if (k === "End") go(calls.length - 1);
  else if (k === " ") togglePlay();
  else {
    const { e } = current();
    return labelKey(event, isLabelable(e) ? e.id : null, isLabelable(e) ? e.id : null);
  }
  return true;
}

// ---------- the scrubber ----------

function tickClass(e, pos) {
  const label = isLabelable(e) && state.labels ? getLabel(e.id) : null;
  const mark = !isLabelable(e) ? "" : !label || !Array.isArray(label.patterns) ? " lab-open" : label.patterns.length ? " lab-yes" : " lab-no";
  return `tick st-${e.status}${mark}${pos === state.replay.pos ? " selected" : ""}`;
}

function renderTicks() {
  put(ui.ticks, calls.map(({ e }, pos) => {
    const tick = h("span", { class: tickClass(e, pos), title: e.kind === "stop" ? "stop" : `#${e.n} ${e.tool}: ${e.summary}` });
    tick.addEventListener("click", () => go(pos));
    return tick;
  }));
}

function markTicks() {
  [...ui.ticks.children].forEach((tick, pos) => tick.classList.toggle("selected", pos === state.replay.pos));
}

function renderInfo() {
  const { e } = current();
  const copy = h("button", { class: "id-btn", title: "Copy the tool_call_id" }, e.id);
  copy.addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(e.id));
  const repeat = state.hints && e.hints && e.hints.repeat ? h("span", { class: "hint inline" }, `same command as #${e.hints.repeat.of}${e.hints.repeat.changed_between ? "" : ", nothing changed in between"}`) : null;
  put(
    ui.info,
    e.kind === "stop" ? icon("stop", "tool-icon") : toolIcon(e.tool),
    h("strong", null, e.kind === "stop" ? "Stop" : `#${e.n} ${e.tool}`),
    h("span", { class: "muted small" }, `call ${state.replay.pos + 1} of ${calls.length} · ${fmtTime(e.t)}`),
    copy,
    statusChip(e),
    isLabelable(e) && state.labels ? h("span", { class: `verdict ${verdictOf(e.id).cls}` }, verdictOf(e.id).text) : null,
    repeat,
    h("span", { class: "spacer" }),
    h("a", { class: "btn small", href: trialHref(state.job, state.trialName, "trace", e.id) }, icon("list"), "Open in trace"),
  );
}

// ---------- files: what each path looks like at the selected call ----------

function changedPaths(e) {
  if (e.changes) return e.changes.files.map((f) => f.path);
  return (e.paths || []).map((p) => p.path);
}

function stateOf(path, at) {
  const versions = state.trace.files[path] || [];
  const done = versions.filter((v) => v.event <= at);
  const last = done[done.length - 1];
  if (!last) return { kind: versions[0] && versions[0].op === "added" ? "future" : "untouched", next: versions[0] };
  if (last.event === at) return { kind: "now", op: last.op, last };
  if (last.op === "removed") return { kind: "deleted", last };
  return { kind: "present", last };
}

function renderExplorer() {
  const r = state.replay;
  const at = current().i;
  const root = { dirs: new Map(), files: [] };
  for (const path of Object.keys(state.trace.files).sort()) {
    const s = stateOf(path, at);
    if (s.kind === "future" && !r.showFuture) continue;
    const parts = path.split("/").filter(Boolean);
    let node = root;
    for (let k = 0; k < parts.length - 1; k++) {
      const dir = `/${parts.slice(0, k + 1).join("/")}`;
      if (!node.dirs.has(parts[k])) node.dirs.set(parts[k], { path: dir, dirs: new Map(), files: [] });
      node = node.dirs.get(parts[k]);
    }
    node.files.push({ path, name: parts[parts.length - 1], s });
  }
  const touchedNow = (node) => node.files.some((f) => f.s.kind === "now") || [...node.dirs.values()].some(touchedNow);
  const count = (node) => node.files.length + [...node.dirs.values()].reduce((n, d) => n + count(d), 0);
  const fileRow = (f, depth) => {
    const s = f.s;
    const badge = s.kind === "now" ? OP[s.op] : s.kind === "deleted" ? "D" : s.kind === "present" ? OP[s.last.op] : "";
    const when = s.kind === "future" || s.kind === "untouched" ? (s.next ? `later #${s.next.call ?? "stop"}` : "") : s.last.call ? `#${s.last.call}` : "";
    const row = h(
      "div",
      { class: `tree-row file ${s.kind}${r.active === f.path ? " active" : ""}`, title: f.path },
      indent(depth),
      icon("file", "tree-icon"),
      h("span", { class: "tree-name" }, f.name),
      h("span", { class: "tree-when" }, when),
      badge ? h("span", { class: `op op-${{ A: "added", M: "modified", D: "removed" }[badge]}` }, badge) : null,
    );
    row.addEventListener("click", () => openFile(f.path));
    return row;
  };
  const dirRows = (node, depth) => {
    const rows = [];
    for (const [name, dir] of [...node.dirs.entries()].sort()) {
      const closed = r.collapsed.has(dir.path);
      const row = h("div", { class: `tree-row dir${touchedNow(dir) ? " now" : ""}` }, indent(depth), icon(closed ? "chevronRight" : "chevronDown", "tree-icon"), icon("folder", "tree-icon"), h("span", { class: "tree-name" }, name), h("span", { class: "tree-when" }, String(count(dir))));
      row.addEventListener("click", () => {
        if (closed) r.collapsed.delete(dir.path);
        else r.collapsed.add(dir.path);
        renderExplorer();
      });
      rows.push(row);
      if (!closed) rows.push(...dirRows(dir, depth + 1));
    }
    rows.push(...node.files.sort((a, b) => a.name.localeCompare(b.name)).map((f) => fileRow(f, depth)));
    return rows;
  };
  const rows = dirRows(root, 0);
  put(ui.explorer, rows.length ? rows : note("No call in this trial changed a file."));
  const active = ui.explorer.querySelector(".tree-row.active");
  if (active) active.scrollIntoView({ block: "nearest" });
}

const indent = (depth) => h("span", { class: "tree-indent", "data-depth": depth }, " ".repeat(depth * 3));

function openFile(path, render = true) {
  const r = state.replay;
  if (!r.open.includes(path)) r.open.push(path);
  if (r.open.length > 6) r.open.shift();
  r.active = path;
  if (render) {
    renderExplorer();
    renderEditor();
  }
}

function closeFile(path) {
  const r = state.replay;
  r.open = r.open.filter((p) => p !== path);
  if (r.active === path) r.active = r.open[r.open.length - 1] || null;
  renderExplorer();
  renderEditor();
}

function followToggle() {
  const r = state.replay;
  const input = h("input", { type: "checkbox" });
  input.checked = r.follow;
  input.addEventListener("change", () => {
    r.follow = input.checked;
  });
  return h("label", { class: "toggle small", title: "Open the files each call changes" }, input, h("span", null, "follow"));
}

function renderEditor() {
  const r = state.replay;
  const at = current().i;
  const modeButton = (mode, text) => h("button", { class: `seg-btn${r.mode === mode ? " on" : ""}`, onclick: () => {
    r.mode = mode;
    renderEditor();
  } }, text);
  put(
    ui.editorTabs,
    r.open.map((path) => h("span", { class: `editor-tab${path === r.active ? " active" : ""}`, title: path }, h("button", { class: "tab-name", onclick: () => {
      r.active = path;
      renderExplorer();
      renderEditor();
    } }, path.split("/").pop()), h("button", { class: "tab-close", title: "Close", onclick: () => closeFile(path) }, icon("x")))),
    h("span", { class: "spacer" }),
    followToggle(),
    h("span", { class: "seg" }, modeButton("diff", "Diff"), modeButton("file", "File")),
  );
  if (!r.active) {
    const now = changedPaths(current().e);
    put(ui.editor, h("div", { class: "editor-empty" }, h("p", null, "Pick a file in the explorer."), now.length ? h("p", null, "This call changed: ", now.slice(0, 8).flatMap((p, k) => [k ? ", " : "", h("button", { class: "btn-link", onclick: () => openFile(p) }, p)])) : h("p", { class: "muted" }, "This call changed no file.")));
    return;
  }
  const path = r.active;
  const s = stateOf(path, at);
  const header = (text, extra) => h("div", { class: "editor-head" }, h("code", { class: "editor-path" }, path), h("span", { class: "muted small" }, text), extra);
  if (s.kind === "future" || s.kind === "untouched") {
    put(ui.editor, header(s.kind === "future" ? "does not exist yet" : "not changed yet"), note(s.next ? `The first call that changes it is ${s.next.call ? `#${s.next.call}` : "the stop"}.` : ""));
    if (r.mode === "file" && s.kind === "untouched") showFile(path);
    return;
  }
  const last = s.last;
  const event = state.events[last.event];
  const by = last.event === at ? "changed by this call" : `last changed by #${last.call ?? "stop"} ${event.tool}`;
  if (r.mode === "diff") {
    const entry = event.changes ? event.changes.files.find((f) => f.path === path) : null;
    const jump = last.event === at ? null : h("button", { class: "btn-link small", onclick: () => go(calls.findIndex((c) => c.i === last.event)) }, "go there");
    if (last.op === "removed") put(ui.editor, header(`${by}: deleted`, jump), note("Deleted."));
    else if (entry && entry.diff) {
      const stats = diffStats(entry.diff);
      put(ui.editor, header(by, [h("span", { class: "diffstat" }, h("span", { class: "add" }, `+${stats.add}`), h("span", { class: "del" }, `−${stats.del}`)), jump]), diffView(entry.diff, { path, max: 2000 }));
    } else put(ui.editor, header(by, jump), note(entry && entry.skipped ? `No diff recorded (${entry.skipped}).` : event.changes ? "No diff recorded for this file." : "No diff recorded: this trial was captured without --extras (paths only)."), h("p", null, h("button", { class: "btn small", onclick: () => {
      r.mode = "file";
      renderEditor();
    } }, "Show the whole file")));
    return;
  }
  if (last.op === "removed") {
    put(ui.editor, header(`${by}: deleted`), note("Deleted by this point."));
    return;
  }
  showFile(path, last, by);
}

async function showFile(path, last = null, by = "") {
  const r = state.replay;
  const ask = last ? last.id : current().e.id;
  const cacheKey = `${path}@${ask}`;
  const header = (extra) => h("div", { class: "editor-head" }, h("code", { class: "editor-path" }, path), h("span", { class: "muted small" }, by), extra);
  let view = r.files.get(cacheKey);
  if (!view) {
    put(ui.editor, header(null), h("p", { class: "muted pad" }, "Loading…"));
    try {
      view = await api(`/api/file/${enc(state.job)}/${enc(state.trialName)}?path=${enc(path)}&call=${enc(ask)}`);
      r.files.set(cacheKey, view);
    } catch (error) {
      view = { error: error.message };
    }
    if (r.active !== path) return;
  }
  if (view.error) {
    put(ui.editor, header(null), note(view.error));
    return;
  }
  const source = view.source === "diffs" ? h("span", { class: "pill ok", title: view.note }, "rebuilt from diffs") : view.source === "snapshot" ? h("span", { class: `pill ${view.verified ? "ok" : ""}`, title: view.note }, `read from checkpoint #${view.checkpoint}`) : null;
  if (view.text === null || view.text === undefined) {
    put(ui.editor, header(source), note(view.binary ? `A binary file (${view.size} bytes).` : view.note));
    return;
  }
  put(ui.editor, header([source, view.verified ? null : h("span", { class: "pill warn", title: view.note }, "unverified")]), fileView(view.text, { path, changed: view.changed_lines, max: 5000 }));
  const first = ui.editor.querySelector(".fl-changed");
  if (first) first.scrollIntoView({ block: "center" });
}

// ---------- the terminal ----------

function buildTerminal() {
  const r = state.replay;
  ui.entries = [];
  const rows = [];
  state.events.forEach((e, i) => {
    let entry = null;
    if (e.kind === "call" && e.tool === "Bash") {
      const a = e.input || {};
      const output = e.output || "";
      const lines = output.split("\n");
      const tail = lines.length > 14 ? lines.slice(-14).join("\n") : output;
      const pre = h("pre", { class: "term-out" }, tail);
      const more = lines.length > 14 ? h("button", { class: "btn-link small", onclick: () => {
        pre.textContent = output;
        more.remove();
      } }, `show all ${lines.length} lines`) : null;
      entry = h(
        "div",
        { class: "term-entry bash" },
        a.description ? h("div", { class: "term-comment" }, `# ${a.description}`) : null,
        h("div", { class: "term-cmd" }, h("button", { class: "term-n", onclick: () => go(calls.findIndex((c) => c.i === i)) }, `#${e.n}`), h("span", { class: "term-prompt" }, "$"), h("code", null, highlight(a.command || "", "bash")), e.exit_code ? h("span", { class: "chip st-timeout" }, `exit ${e.exit_code}`) : null, a.run_in_background ? h("span", { class: "chip st-deferred" }, "background") : null),
        output ? pre : null,
        more,
      );
    } else if (e.kind === "call") {
      const files = changedPaths(e);
      const writes = ["Write", "Edit", "MultiEdit", "NotebookEdit"].includes(e.tool);
      entry = h(
        "div",
        { class: `term-entry marker${writes ? " write" : ""}` },
        h("button", { class: "term-n", onclick: () => go(calls.findIndex((c) => c.i === i)) }, `#${e.n}`),
        toolIcon(e.tool),
        h("span", null, `${e.tool} `, h("code", null, e.summary)),
        files.length ? h("span", { class: "muted small" }, ` → ${files.length === 1 ? "1 file" : `${files.length} files`}`) : null,
      );
    } else if (e.kind === "stop") {
      entry = h("div", { class: "term-entry marker stop" }, icon("stop", "tool-icon"), "the agent ended its turn");
    } else if (e.kind === "message" && r.showMessages && e.text) {
      entry = h("div", { class: "term-entry said" }, icon("message", "tool-icon"), h("span", null, e.text.split("\n")[0].slice(0, 240)));
    }
    if (entry) {
      ui.entries.push({ i, el: entry });
      rows.push(entry);
    }
  });
  put(ui.terminal, rows);
}

function updateTerminal() {
  const at = current().i;
  let selected = null;
  for (const { i, el } of ui.entries) {
    el.hidden = i > at;
    el.classList.toggle("current", i === at);
    if (i === at) selected = el;
  }
  if (selected) selected.scrollIntoView({ block: "end" });
  else ui.terminal.scrollTop = ui.terminal.scrollHeight;
}

// ---------- processes ----------

function renderProcs() {
  const at = current().i;
  let snapshot = null;
  for (let i = at; i >= 0; i--) {
    const e = state.events[i];
    if (e.processes) {
      snapshot = { e, i, p: e.processes };
      break;
    }
  }
  if (!state.trace.capture.extras) {
    put(ui.procs, note("No process records: this trial was captured without --extras."));
    return;
  }
  if (!snapshot) {
    put(ui.procs, note("No process snapshot yet."));
    return;
  }
  const { p } = snapshot;
  const now = snapshot.i === at;
  const startedNow = new Set(now ? p.started : []);
  put(
    ui.procs,
    now && (p.stopped.length || p.closed.length) ? h("div", { class: "proc-gone" }, p.stopped.map((cmd) => h("div", { class: "del" }, "stopped ", h("code", null, cmd))), p.closed.map((x) => h("div", { class: "del" }, `closed port ${x.port}`))) : null,
    h("div", { class: "proc-group" }, h("div", { class: "muted small" }, `Agent processes (${p.running.length})`), p.running.length ? p.running.map((cmd) => h("div", { class: `proc${startedNow.has(cmd) ? " add" : ""}` }, h("code", null, cmd))) : h("div", { class: "muted small" }, "none")),
    h("div", { class: "proc-group" }, h("div", { class: "muted small" }, `Listening ports (${p.listening.length})`), p.listening.length ? p.listening.map((x) => h("div", { class: `proc${now && p.opened.some((o) => o.port === x.port) ? " add" : ""}` }, h("code", null, `${x.proto} ${x.port}`))) : h("div", { class: "muted small" }, "none")),
    !now ? h("div", { class: "muted small" }, `as of #${snapshot.e.n ?? "stop"}`) : null,
  );
}

// ---------- labels ----------

function renderLabels() {
  const r = state.replay;
  const { e } = current();
  const toggle = h("button", { class: "btn-link small", onclick: () => {
    r.labelsOpen = !r.labelsOpen;
    storageSet("trajlab.replay.labels", r.labelsOpen ? "open" : null);
    renderLabels();
  } }, r.labelsOpen ? "hide checklist" : "show checklist");
  if (!isLabelable(e)) {
    put(ui.labels, h("div", { class: "replay-labels-bar muted small" }, "This call is not hooked, so there is nothing to label here."));
    return;
  }
  const title = e.kind === "stop" ? "Your label for the stop" : `Your label for #${e.n}`;
  put(ui.labels, r.labelsOpen ? [labelPanel(e.id, title, { compact: true, callId: e.id }), h("div", { class: "replay-labels-bar" }, toggle)] : h("div", { class: "replay-labels-bar" }, h("strong", null, title), state.labels ? h("span", { class: `verdict ${verdictOf(e.id).cls}` }, verdictOf(e.id).text) : null, h("span", { class: "spacer" }), toggle));
}
