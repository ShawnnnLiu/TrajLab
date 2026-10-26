// The Trace tab: every call in order on the left; on the right, what the agent said, ran, and
// saw beside what actually changed, with the checklist below.

import {
  $, enc, fmtBytes, fmtMs, fmtSeconds, fmtTime, fmtTokens, fmtUsd, h, icon, isLabelable, note, ordinal,
  patternTitle, plural, put, section, shortHash, STALE_PILL, staleNote, state, toolIcon, trialHref, widthOf,
} from "./core.js";
import { highlight } from "./code.js";
import { diffStats, diffView, editView, fileView } from "./diff.js";
import { TRIAL_KEY, getLabel, isJudged, labelKey, labelPanel, setDone, verdictOf } from "./labels.js";
import { markdown } from "./md.js";

let rowEls = new Map();
let cellEls = new Map();

export function renderTrace(body, select) {
  let index = state.selected;
  if (select && select.startsWith("@")) index = Number(select.slice(1));
  else if (select) index = state.events.findIndex((e) => e.id === select);
  state.selected = index >= 0 && index < state.events.length ? index : -1;
  put(
    body,
    h("div", { id: "strip", class: "strip", "aria-label": "Hooked calls" }),
    h("div", { class: "panes" }, h("aside", { class: "list-pane" }, listControls(), h("div", { id: "list", class: "list" })), h("section", { id: "detail", class: "detail" })),
  );
  renderStrip();
  renderList();
  renderDetail();
  scrollToSelected();
  state.keyHandler = traceKeys;
  state.onHints = () => renderDetail();
  state.onLabelsChangeTab = refreshMarks;
}

// ---------- the strip and the list ----------

const keyOf = (index) => (index === -1 ? TRIAL_KEY : state.events[index] && state.events[index].id);

function labelClass(id) {
  const label = getLabel(id);
  if (!label || !Array.isArray(label.patterns)) return "lab-open";
  return label.patterns.length ? "lab-yes" : "lab-no";
}

function cellClass(e, index) {
  const wrong = state.labels && state.labels.first_wrong_step === e.id ? " wrong" : "";
  return `cell st-${e.status} ${labelClass(e.id)}${wrong}${index === state.selected ? " selected" : ""}`;
}

function cellTitle(e) {
  const what = e.kind === "stop" ? "stop" : `#${e.n} ${e.tool}`;
  return `${what} · ${e.status.replace("_", " ")} · ${verdictOf(e.id).text}`;
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
  put(strip, cells.length ? cells : h("span", { class: "muted small" }, "No hooked calls."));
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
    return h("label", { class: "toggle small" }, input, h("span", null, text));
  };
  const search = h("input", { type: "search", class: "input", placeholder: "Filter: command, path, text, #12", value: f.search });
  search.addEventListener("input", () => {
    f.search = search.value;
    renderList();
  });
  return h("div", { class: "list-controls" }, search, h("div", { class: "toggles" }, toggle("messages", "messages"), toggle("notHooked", "not-hooked calls"), toggle("unlabeled", "only unjudged")));
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

export function statusChip(e) {
  const chip = (cls, text) => h("span", { class: `chip ${cls}` }, text);
  switch (e.status) {
    case "changed":
      return chip("st-changed", changeCounts(e) || "changed");
    case "checkpoint":
      return chip("st-changed", "checkpoint");
    case "baseline":
      return chip("st-baseline", "baseline");
    case "unchanged":
      return chip("st-unchanged", "no change");
    case "deferred":
      return chip("st-deferred", "deferred");
    case "timeout":
      return chip("st-timeout", "timeout");
    case "missing":
      return chip("st-timeout", "no record");
    default:
      return null;
  }
}

function labelChip(e) {
  if (!isLabelable(e) || !state.labels) return h("span", { class: "lab" });
  const label = getLabel(e.id);
  const wrong = state.labels.first_wrong_step === e.id ? icon("flag", "flag-icon") : null;
  const extra = label ? `${label.unsure ? " ?" : ""}${label.note ? " ✎" : ""}` : "";
  if (!label || !Array.isArray(label.patterns)) return h("span", { class: "lab open", title: "not judged" }, wrong, `○${extra}`);
  if (!label.patterns.length) return h("span", { class: "lab no", title: "judged: none" }, wrong, `✓${extra}`);
  const text = label.patterns.length === 1 ? patternTitle(label.patterns[0]) : `${label.patterns.length} patterns`;
  return h("span", { class: "lab yes", title: label.patterns.map(patternTitle).join(", ") }, wrong, `${text}${extra}`);
}

function rowFor(e, index) {
  let cells;
  if (e.kind === "message" || e.kind === "user") cells = [icon("message", "row-icon"), h("span", { class: "num" }, e.kind === "user" ? "user" : "said"), h("span", { class: "sum said-sum" }, e.text || "(thinking only)"), h("span")];
  else if (e.kind === "stop") cells = [icon("stop", "row-icon"), h("span", { class: "num" }, "stop"), h("span", { class: "sum strong" }, "End of the agent's turn"), h("span", { class: "marks" }, statusChip(e), labelChip(e))];
  else cells = [toolIcon(e.tool), h("span", { class: "num" }, `#${e.n}`), h("span", { class: "sum" }, h("span", { class: "tool" }, e.tool), " ", e.summary), h("span", { class: "marks" }, e.exit_code ? h("span", { class: "chip st-timeout", title: `exit ${e.exit_code}` }, `exit ${e.exit_code}`) : null, statusChip(e), labelChip(e))];
  const classes = ["row", `kind-${e.kind}`, e.kind === "call" && !e.hooked ? "not-hooked" : "", index === state.selected ? "selected" : ""];
  return h("div", { class: classes.join(" "), onclick: () => select(index) }, cells);
}

function renderList() {
  const list = $("#list");
  if (!list) return;
  rowEls = new Map();
  const whole = getLabel(TRIAL_KEY);
  const overview = h(
    "div",
    { class: `row kind-trial${state.selected === -1 ? " selected" : ""}`, onclick: () => select(-1) },
    icon("layout", "row-icon"),
    h("span", { class: "num" }, "trial"),
    h("span", { class: "sum strong" }, "Overview and whole-trial label"),
    h("span", { class: "marks" }, state.labels ? h("span", { class: `lab ${whole && Array.isArray(whole.patterns) ? (whole.patterns.length ? "yes" : "no") : "open"}` }, whole && Array.isArray(whole.patterns) ? "judged" : "○") : null),
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
    return;
  }
  const index = state.events.findIndex((e) => e.id === key);
  const e = state.events[index];
  if (!e) return;
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

function select(index, scroll = true) {
  state.selected = index;
  const e = state.events[index];
  const tail = index === -1 ? "" : `/${enc(e.id || `@${index}`)}`;
  history.replaceState(null, "", `#/trial/${enc(state.job)}/${enc(state.trialName)}/trace${tail}`);
  rowEls.forEach((row, i) => row.classList.toggle("selected", i === index));
  cellEls.forEach((cell, i) => cell.classList.toggle("selected", i === index));
  renderDetail();
  if (scroll) scrollToSelected();
}

function scrollToSelected() {
  const row = rowEls.get(state.selected);
  if (row) row.scrollIntoView({ block: "nearest" });
  const cell = cellEls.get(state.selected);
  if (cell) cell.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function traceKeys(event) {
  const k = event.key;
  if (k === "j") moveStep(1);
  else if (k === "k") moveStep(-1);
  else if (k === "ArrowDown") moveRow(1);
  else if (k === "ArrowUp") moveRow(-1);
  else if (k === "]") nextUnjudged();
  else {
    const e = state.events[state.selected];
    const key = state.selected === -1 ? TRIAL_KEY : isLabelable(e) ? e.id : null;
    return labelKey(event, key, state.selected === -1 ? null : key);
  }
  return true;
}

function moveRow(delta) {
  const order = [-1, ...state.events.map((e, i) => i).filter((i) => visible(state.events[i]))];
  const at = order.indexOf(state.selected);
  const next = order[Math.min(order.length - 1, Math.max(0, at + delta))];
  if (next !== undefined) select(next);
}

function moveStep(delta) {
  const steps = state.events.map((e, i) => i).filter((i) => isLabelable(state.events[i]));
  if (!steps.length) return;
  let next = delta > 0 ? steps.find((i) => i > state.selected) : [...steps].reverse().find((i) => i < state.selected);
  if (next === undefined) next = delta > 0 ? steps[steps.length - 1] : -1;
  select(next);
}

function nextUnjudged() {
  const steps = state.events.map((e, i) => i).filter((i) => isLabelable(state.events[i]) && !isJudged(state.events[i].id));
  if (steps.length) select(steps.find((i) => i > state.selected) ?? steps[0]);
}

// ---------- the detail of one call ----------

const callLink = (n) => h("button", { class: "btn-link", onclick: () => select(state.events.findIndex((e) => e.kind === "call" && e.n === n)) }, `#${n}`);
const callLinks = (ns) => ns.flatMap((n, i) => [i ? ", " : "", callLink(n)]);

function expandable(text, cls, lines = 24) {
  const pre = h("pre", { class: cls });
  pre.textContent = text;
  const count = text.split("\n").length;
  if (count <= lines) return pre;
  pre.classList.add("clipped");
  const button = h("button", { class: "btn-link", onclick: () => {
    pre.classList.toggle("clipped");
    button.textContent = pre.classList.contains("clipped") ? `Show all ${count} lines` : "Show less";
  } }, `Show all ${count} lines`);
  return h("div", null, pre, button);
}

const codeBlock = (text, lang) => h("pre", { class: "code-block" }, h("code", null, highlight(text, lang)));
const pathLine = (path, extra) => h("div", { class: "pathline" }, icon("file"), h("code", null, path || "(no path)"), extra ? h("span", { class: "muted" }, extra) : null);

function inputView(e) {
  const a = e.input || {};
  switch (e.tool) {
    case "Bash":
      return [
        a.description ? h("div", { class: "intent" }, h("span", { class: "muted" }, "Says it will: "), a.description) : null,
        codeBlock(a.command || "", "bash"),
        a.run_in_background ? h("span", { class: "pill warn" }, "runs in the background") : null,
        a.timeout ? h("span", { class: "pill" }, `timeout ${fmtMs(a.timeout)}`) : null,
      ];
    case "Edit":
      return [pathLine(a.file_path, a.replace_all ? "replace all" : null), editView(a.old_string, a.new_string, { path: a.file_path })];
    case "MultiEdit":
      return [pathLine(a.file_path, plural((a.edits || []).length, "edit")), ...(a.edits || []).map((edit, k) => [h("div", { class: "sub muted" }, `edit ${k + 1}${edit.replace_all ? " (replace all)" : ""}`), editView(edit.old_string, edit.new_string, { path: a.file_path })])];
    case "Write": {
      const content = a.content || "";
      return [pathLine(a.file_path, plural(content.split("\n").length, "line")), fileView(content, { path: a.file_path, max: 200 })];
    }
    case "NotebookEdit":
      return [pathLine(a.notebook_path, [a.edit_mode, a.cell_type, a.cell_id].filter(Boolean).join(" · ")), codeBlock(a.new_source || "", a.cell_type === "markdown" ? "" : "python")];
    default:
      return Object.keys(a).length ? codeBlock(JSON.stringify(a, null, 2), "json") : note("(no input recorded)");
  }
}

function outputView(e) {
  const chips = [
    e.exit_code !== null && e.exit_code !== undefined ? h("span", { class: `pill ${e.exit_code === 0 ? "ok" : "bad"}` }, `exit ${e.exit_code}`) : null,
    e.is_error ? h("span", { class: "pill bad" }, "tool reported an error") : null,
    e.output_cut ? h("span", { class: "pill warn" }, "middle cut for length") : null,
  ];
  return [h("div", { class: "chips" }, chips), e.output ? expandable(e.output, "output", 24) : note("(no output)")];
}

const STATUS_TEXT = {
  changed: ["Changed the filesystem.", "ok"],
  baseline: ["First measured call (baseline): nothing earlier to compare with, so no change is listed.", "muted"],
  checkpoint: ["A checkpoint was taken (no change detector in this image).", "ok"],
  unchanged: ["No change on disk: the filesystem equals the last checkpoint's.", "muted"],
  deferred: ["Deferred: a later checkpoint covers this call (every-N).", "warn"],
  timeout: ["The hook timed out waiting for the watcher: this call's state was not captured.", "bad"],
  missing: ["The watcher left no record for this call.", "bad"],
  uncaptured: ["This trial ran without the checkpoint watcher: nothing was measured.", "muted"],
  not_hooked: ["Not hooked: the hook does not see this tool, so nothing was measured.", "muted"],
};

const SKIPPED_TEXT = {
  not_text: "binary or not UTF-8, so no diff",
  too_large: "over 1 MiB, so not read",
  too_many_lines: "too many lines to diff",
  over_file_cap: "past the 20 files read per call",
  over_budget: "earlier version not read (2 s budget)",
  unreadable: "could not be read",
};

const OP = { added: ["A", "added"], modified: ["M", "modified"], removed: ["D", "deleted"] };
const opBadge = (op) => h("span", { class: `op op-${op}`, title: OP[op] ? OP[op][1] : op }, OP[op] ? OP[op][0] : "?");

function filesHref(path) {
  return trialHref(state.job, state.trialName, "files", path);
}

function fileChangeView(f, open) {
  const stats = f.diff ? diffStats(f.diff) : null;
  const meta = [
    f.kind === "symlink" ? "symlink" : null,
    f.size !== null && f.size !== undefined ? fmtBytes(f.size) : null,
    f.before_sha256 || f.sha256 ? `${shortHash(f.before_sha256) || "new"} → ${shortHash(f.sha256) || (f.op === "removed" ? "gone" : "?")}` : null,
  ].filter(Boolean);
  const head = h(
    "div",
    { class: "file-head" },
    opBadge(f.op),
    h("a", { class: "path", href: filesHref(f.path), title: "This file's history" }, f.path),
    stats ? h("span", { class: "diffstat" }, h("span", { class: "add" }, `+${stats.add}`), h("span", { class: "del" }, `−${stats.del}`)) : null,
    meta.length ? h("span", { class: "muted small" }, meta.join(" · ")) : null,
    f.skipped ? h("span", { class: "pill warn" }, SKIPPED_TEXT[f.skipped] || f.skipped) : null,
    f.stale_before ? h("span", { class: "pill warn" }, STALE_PILL) : null,
  );
  const stale = f.stale_before ? staleNote(f.stale_before, !!f.diff) : null;
  if (!f.diff) return h("div", { class: "file-card" }, head, stale);
  const details = h("details", { class: "diff-box" }, h("summary", null, "diff"), diffView(f.diff, { path: f.path }));
  if (open) details.open = true;
  return h("div", { class: "file-card" }, head, stale, details);
}

function changesView(e) {
  const [text, tone] = STATUS_TEXT[e.status] || [e.status, ""];
  const out = [h("p", { class: `status ${tone}` }, text)];
  const facts = [];
  if (e.checkpoint) facts.push(`checkpoint #${e.checkpoint.seq}${e.checkpoint.bytes ? ` (${fmtBytes(e.checkpoint.bytes)} layer)` : ""}, ${fmtMs(e.checkpoint.capture_ms)} to save`);
  else if (e.state_of) facts.push(`state as in checkpoint #${e.state_of}`);
  if (e.pause_ms !== null && e.pause_ms !== undefined) facts.push(`agent paused ${fmtMs(e.pause_ms)}`);
  if (e.record && e.record.tool_failed) facts.push("the call failed");
  if (facts.length) out.push(h("p", { class: "muted small" }, facts.join(" · ")));
  if (e.unmatched) out.push(note("This hooked call is not in the trajectory (a subagent's call?)."));
  if (e.changes) {
    const files = e.changes.files;
    if (e.changes.baseline) out.push(note("No file list for the baseline call."));
    else if (!files.length && e.status === "changed") out.push(note("Only directories changed."));
    const open = files.length <= 4;
    out.push(...files.map((f) => fileChangeView(f, open && f.diff && f.diff.split("\n").length <= 80)));
    if (e.changes.files_total > files.length) out.push(note(`${e.changes.files_total - files.length} more changed paths were not recorded.`));
  } else if (e.paths && e.paths.length) {
    out.push(note("Paths only: this trial was captured without --extras, so there are no contents or diffs."));
    out.push(h("div", { class: "paths" }, e.paths.map((p) => h("div", { class: "path-row" }, opBadge(p.op), h("a", { class: "path", href: filesHref(p.path) }, p.path)))));
    if (e.paths_total && e.paths_total > e.paths.length) out.push(note(`${e.paths_total - e.paths.length} more changed paths were not listed.`));
  }
  return out;
}

function timingView(e) {
  const step = e.step_id !== null && e.step_id !== undefined ? state.trace.steps[String(e.step_id)] : null;
  if (!step && (e.tool_s === undefined || e.tool_s === null) && !e.pause_ms) return null;
  const parts = [];
  if (step && step.model_s !== null) parts.push(h("span", { class: "time-part model" }, h("b", null, fmtSeconds(step.model_s)), e.tool_s === undefined && state.trace.capture.captured === false ? " model + tools" : " model"));
  if (e.tool_s !== undefined && e.tool_s !== null) parts.push(h("span", { class: "time-part tool" }, h("b", null, fmtSeconds(e.tool_s)), " tool"));
  if (e.pause_ms) parts.push(h("span", { class: "time-part pause" }, h("b", null, fmtMs(e.pause_ms)), " checkpoint pause"));
  const tokens = step && step.prompt_tokens !== null
    ? h("div", { class: "muted small" }, `This step's model call: ${fmtTokens(step.prompt_tokens)} in (${fmtTokens(step.cached_tokens)} cached) · ${fmtTokens(step.completion_tokens)} out · ${fmtUsd(step.cost_usd)}`)
    : null;
  return section("Time and cost", h("div", { class: "time-parts" }, parts), tokens);
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
      h("summary", null, `Running after this call: ${plural(p.running.length, "agent process", "agent processes")}, ${plural(p.listening.length, "listening port")}`),
      h("ul", { class: "procs" }, p.running.map((cmd) => h("li", null, h("code", null, cmd))), p.listening.map((x) => h("li", null, `listening ${x.proto} ${x.port}`))),
    ),
  );
}

function callHints(e) {
  if (!state.hints || !e.hints) return null;
  const items = [];
  const r = e.hints.repeat;
  if (r) items.push(h("li", null, "Same command as ", callLink(r.of), ` (${ordinal(r.count)} time); ${r.changed_between ? "files changed in between" : "no file changed in between"}.`));
  for (const v of e.hints.reverts || []) items.push(h("li", null, h("code", null, v.path), v.to === "original" ? " is back to its content before the trial changed it." : [" is back to its content after ", callLink(v.to), "."]));
  return items.length ? h("div", { class: "hint" }, h("strong", null, "Hints"), h("ul", null, items)) : null;
}

function messageHints(e) {
  if (!state.hints || !e.hints) return null;
  const items = [];
  for (const m of e.hints.mentions || []) {
    const before = m.calls.filter((n) => n <= e.after_call);
    const after = m.calls.filter((n) => n > e.after_call);
    const where = m.calls.length ? [before.length ? ["changed by ", callLinks(before)] : "not changed before this message", after.length ? ["; later by ", callLinks(after)] : ""] : "not changed by any captured call";
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
  return items.length ? h("div", { class: "hint" }, h("strong", null, "Hints"), h("ul", null, items)) : null;
}

function messageBlock(e) {
  if (e.kind === "user") return h("div", { class: "said user" }, h("div", { class: "who" }, "user turn"), markdown(e.text, { compact: true }));
  return h(
    "div",
    { class: "said-wrap" },
    e.text ? h("div", { class: "said" }, markdown(e.text, { compact: true })) : null,
    e.reasoning ? h("details", { class: "thinking" }, h("summary", null, "Thinking"), h("pre", { class: "output" }, e.reasoning)) : null,
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
  const copy = h("button", { class: "id-btn", title: "Copy the tool_call_id" }, e.id);
  copy.addEventListener("click", () => navigator.clipboard && navigator.clipboard.writeText(e.id).then(() => {
    copy.classList.add("copied");
    setTimeout(() => copy.classList.remove("copied"), 900);
  }));
  return h(
    "div",
    { class: "detail-head" },
    e.kind === "stop" ? icon("stop", "head-icon") : toolIcon(e.tool),
    h("h2", null, title),
    copy,
    h("span", { class: "muted small" }, [fmtTime(e.t), e.record ? `call_seq ${e.record.call_seq}` : null, e.step_id ? `step ${e.step_id}` : null].filter(Boolean).join(" · ")),
    h("span", { class: "spacer" }),
    h("a", { class: "btn small", href: trialHref(state.job, state.trialName, "replay", e.id), title: "See this call in the IDE replay" }, icon("layout"), "Open in replay"),
  );
}

function callDetail(e, index) {
  const { messages, between } = saidBefore(index);
  const agent = [
    section(
      isLabelable(e) ? "Said since the previous hooked call" : "Said just before",
      messages.length ? messages.map(messageBlock) : note("(nothing)"),
      between.length ? h("div", { class: "between muted small" }, "Also ran, not hooked: ", between.flatMap((c, i) => [i ? ", " : "", h("button", { class: "btn-link", onclick: () => select(state.events.indexOf(c)) }, `#${c.n} ${c.tool}`)])) : null,
    ),
  ];
  if (e.kind === "call") agent.push(section("Ran", inputView(e)), section("Saw", outputView(e)));
  const env = [section("What changed", changesView(e)), timingView(e), processesView(e.processes), callHints(e)];
  return [
    callHeader(e),
    h("div", { class: "cols" }, h("div", { class: "col card agent" }, h("div", { class: "col-title" }, "Agent: said, ran, saw"), agent), h("div", { class: "col card env" }, h("div", { class: "col-title" }, "Environment: what actually changed"), env)),
  ];
}

function messageDetail(e, index) {
  const next = state.events.slice(index + 1).find((x) => x.kind === "call" || x.kind === "stop");
  return [
    h("div", { class: "detail-head" }, icon("message", "head-icon"), h("h2", null, e.kind === "user" ? "User turn" : "Agent message"), h("span", { class: "muted small" }, [fmtTime(e.t), e.step_id ? `step ${e.step_id}` : null].filter(Boolean).join(" · "))),
    h("div", { class: "card" }, messageBlock(e)),
    next ? h("p", { class: "muted" }, "Labels go on the next hooked call or stop, where the detector reads this message: ", h("button", { class: "btn-link", onclick: () => select(state.events.indexOf(next)) }, next.kind === "stop" ? "the stop" : `#${next.n} ${next.tool}`)) : null,
  ];
}

// ---------- the trial overview ----------

function timeBar(trial, timing, captured) {
  const total = (trial.minutes || 0) * 60;
  if (!total) return null;
  const parts = [
    ["model", captured ? "model" : "model + tools", timing.model_s],
    ["tool", "tools", captured ? timing.tool_s : 0],
    ["pause", "checkpoint pauses", timing.pause_s],
  ];
  const other = Math.max(0, total - parts.reduce((s, p) => s + (p[2] || 0), 0));
  parts.push(["other", "other", other]);
  return h(
    "div",
    null,
    h("div", { class: "stack-bar" }, parts.map(([cls, , s]) => widthOf(h("span", { class: `seg ${cls}`, title: fmtSeconds(s) }), (s || 0) / total))),
    h("div", { class: "legend" }, parts.filter((p) => p[2]).map(([cls, label, s]) => h("span", null, h("i", { class: `dot ${cls}` }), `${label} ${fmtSeconds(s)}`))),
  );
}

function trialDetail() {
  const { trial, capture, checks, timing } = state.trace;
  const stats = { judged: 0, total: 0 };
  for (const e of state.events) if (isLabelable(e)) {
    stats.total++;
    if (isJudged(e.id)) stats.judged++;
  }
  const found = {};
  for (const label of Object.values((state.labels && state.labels.steps) || {})) for (const p of label.patterns || []) found[p] = (found[p] || 0) + 1;
  const wrongId = state.labels && state.labels.first_wrong_step;
  const wrong = wrongId ? state.events.find((e) => e.id === wrongId) : null;
  const doneBox = h("input", { type: "checkbox", onchange: (event) => setDone(event.target.checked) });
  doneBox.checked = !!(state.labels && state.labels.done);
  const fact = (k, v) => [h("dt", null, k), h("dd", null, v)];
  return [
    h("div", { class: "detail-head" }, icon("layout", "head-icon"), h("h2", null, "Trial overview")),
    h(
      "div",
      { class: "overview-grid" },
      h("div", { class: "card" }, h("h3", null, "Result"), h("dl", { class: "facts" }, fact("Reward", trial.reward === null || trial.reward === undefined ? "–" : String(trial.reward)), fact("Exception", trial.exception || "–"), fact("Verifier", checks ? `${checks.passed} of ${checks.total} tests passed` : "–"), fact("Model", trial.model || "–"), fact("Claude Code", trial.agent_version || "–")),
        checks && checks.failed.length ? h("details", { class: "checks-list" }, h("summary", null, plural(checks.failed.length, "failed test")), h("ul", null, checks.failed.map((t) => h("li", null, h("code", null, t.name), t.message ? h("pre", { class: "output small" }, t.message) : null)))) : null),
      h("div", { class: "card" }, h("h3", null, "Where the time went"), h("p", { class: "big-number" }, `${trial.minutes ? Math.round(trial.minutes) : "–"} min`), timing ? timeBar(trial, timing, capture.captured) : null),
      h("div", { class: "card" }, h("h3", null, "Tokens and cost"), timing && timing.prompt_tokens !== null ? h("dl", { class: "facts" }, fact("Input", fmtTokens(timing.prompt_tokens)), fact("Cached", fmtTokens(timing.cached_tokens)), fact("Output", fmtTokens(timing.completion_tokens)), fact("Cost (API prices)", fmtUsd(timing.cost_usd))) : note("No token counts in the trajectory.")),
      h("div", { class: "card" }, h("h3", null, "Capture"), h("dl", { class: "facts" }, fact("Hooked calls", String(capture.hooked_calls)), fact("Checkpoints", String(capture.checkpoints)), fact("Timeouts", String(capture.timeouts)), fact("Records", capture.extras ? "diffs and processes" : capture.captured ? "changed paths" : "none"), fact("Gate", capture.policy ? `${capture.policy.gate}, every ${capture.policy.every}` : "–"), fact("Harness ports", capture.harness_ports.length ? capture.harness_ports.join(", ") : "–"))),
    ),
    trial.exception_message ? section("Exception", h("pre", { class: "output" }, trial.exception_message)) : null,
    trial.instruction ? section("Task instruction", h("details", { class: "card" }, h("summary", null, "Show the task instruction"), markdown(trial.instruction))) : null,
    !state.events.length ? note("No trajectory and no hook records: this trial did not get far.") : null,
    state.labels
      ? section(
          "Your labels",
          h(
            "div",
            { class: "card" },
            h("p", null, `Judged ${stats.judged} of ${stats.total} steps.`, Object.keys(found).length ? ` Found: ${Object.entries(found).map(([k, n]) => `${patternTitle(k)} ×${n}`).join(", ")}.` : ""),
            h("p", null, icon("flag", "flag-icon"), "First wrong step: ", wrong ? h("button", { class: "btn-link", onclick: () => select(state.events.indexOf(wrong)) }, wrong.kind === "stop" ? "the stop" : `#${wrong.n} ${wrong.tool} ${wrong.summary.slice(0, 60)}`) : h("span", { class: "muted" }, "not marked (press w on a call)")),
            h("label", { class: "toggle strong" }, doneBox, h("span", null, "I finished labeling this trial")),
          ),
        )
      : null,
  ];
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
  const key = keyOf(state.selected);
  put(pane, h("div", { class: "detail-body" }, body), labelable ? labelPanel(key, panelTitle(), { callId: state.selected === -1 ? null : key }) : null);
  pane.scrollTop = 0;
}
