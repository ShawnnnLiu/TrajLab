// Two trials side by side: how they differ in result, effort, files, and tools, and both runs.

import {
  api, enc, fmtMinutes, fmtTokens, fmtUsd, h, icon, isLabelable, put, resultPill, setCrumbs, stale,
  toolIcon, trialHref, viewRoot, widthOf,
} from "./core.js";
import { statusChip } from "./view-trace.js";

export async function showCompare(r, seq) {
  const [a, b, job] = await Promise.all([
    api(`/api/trace/${enc(r.job)}/${enc(r.a)}`),
    api(`/api/trace/${enc(r.job)}/${enc(r.b)}`),
    api(`/api/jobs/${enc(r.job)}`),
  ]);
  if (stale(seq)) return;
  setCrumbs([{ text: r.job, href: `#/job/${enc(r.job)}` }, { text: "compare" }]);
  const pick = (value, other, side) => {
    const select = h("select", { class: "input" }, job.trials.map((t) => h("option", { value: t.name }, t.name)));
    select.value = value;
    select.addEventListener("change", () => {
      const [x, y] = side === "a" ? [select.value, other] : [other, select.value];
      location.hash = `#/compare/${enc(r.job)}/${enc(x)}/${enc(y)}`;
    });
    return select;
  };
  const swap = h("a", { class: "btn small", href: `#/compare/${enc(r.job)}/${enc(r.b)}/${enc(r.a)}`, title: "Swap sides" }, "⇄");
  put(
    viewRoot("page wide"),
    h("div", { class: "page-head" }, h("h1", null, "Compare two trials"), h("div", { class: "toolbar" }, h("span", { class: "side-tag a" }, "A"), pick(r.a, r.b, "a"), swap, h("span", { class: "side-tag b" }, "B"), pick(r.b, r.a, "b"))),
    summary(r, a, b),
    h("div", { class: "grid-2" }, filesCompare(a, b), toolsCompare(a, b)),
    h("div", { class: "grid-2 timelines" }, timeline(r.job, r.a, a, "a"), timeline(r.job, r.b, b, "b")),
  );
}

function facts(trace) {
  const calls = trace.events.filter((e) => e.kind === "call");
  const counts = trace.capture.counts;
  return {
    result: resultPill(trace.trial.reward, trace.trial.finished, trace.trial.died),
    minutes: trace.trial.minutes,
    calls: calls.length,
    hooked: trace.capture.hooked_calls,
    changed: (counts.changed || 0) + (counts.checkpoint || 0),
    unchanged: counts.unchanged || 0,
    failed: calls.filter((e) => e.exit_code && e.exit_code !== 0).length,
    files: Object.keys(trace.files).length,
    messages: trace.events.filter((e) => e.kind === "message" && e.text).length,
    tokens: trace.timing ? trace.timing.completion_tokens : null,
    cost: trace.timing ? trace.timing.cost_usd : null,
  };
}

function summary(r, a, b) {
  const fa = facts(a);
  const fb = facts(b);
  const row = (label, x, y, fmt = (v) => (v === null || v === undefined ? "–" : String(v))) => {
    const differ = typeof x === "number" && typeof y === "number" && x !== y;
    return h("tr", null, h("th", null, label), h("td", { class: `num${differ && x > y ? " more" : ""}` }, x instanceof Node ? x : fmt(x)), h("td", { class: `num${differ && y > x ? " more" : ""}` }, y instanceof Node ? y : fmt(y)));
  };
  return h(
    "div",
    { class: "card" },
    h(
      "table",
      { class: "table compare-table" },
      h("thead", null, h("tr", null, h("th"), h("th", null, h("span", { class: "side-tag a" }, "A"), h("a", { href: trialHref(r.job, r.a) }, r.a)), h("th", null, h("span", { class: "side-tag b" }, "B"), h("a", { href: trialHref(r.job, r.b) }, r.b)))),
      h(
        "tbody",
        null,
        row("Result", fa.result, fb.result),
        row("Agent minutes", fa.minutes, fb.minutes, fmtMinutes),
        row("Tool calls", fa.calls, fb.calls),
        row("Hooked calls", fa.hooked, fb.hooked),
        row("Changed the filesystem", fa.changed, fb.changed),
        row("Changed nothing", fa.unchanged, fb.unchanged),
        row("Failed commands", fa.failed, fb.failed),
        row("Files changed", fa.files, fb.files),
        row("Agent messages", fa.messages, fb.messages),
        row("Output tokens", fa.tokens, fb.tokens, fmtTokens),
        row("Cost (API prices)", fa.cost, fb.cost, fmtUsd),
      ),
    ),
  );
}

const NOISY = /\/(site-packages|dist-packages|node_modules|__pycache__|\.cache|\.git|\.npm|\.cargo|\.venv|venv)\//;

function filesCompare(a, b) {
  const pa = new Set(Object.keys(a.files).filter((p) => !NOISY.test(p)));
  const pb = new Set(Object.keys(b.files).filter((p) => !NOISY.test(p)));
  const only = (x, y) => [...x].filter((p) => !y.has(p)).sort();
  const both = [...pa].filter((p) => pb.has(p)).sort();
  const column = (title, paths, cls, count) => h("div", { class: `file-col ${cls}` }, h("h4", null, title, h("span", { class: "count" }, paths.length)), h("ul", null, paths.slice(0, 60).map((p) => h("li", { title: p }, h("code", null, p), count ? h("span", { class: "muted small" }, count(p)) : null))), paths.length > 60 ? h("p", { class: "muted small" }, `and ${paths.length - 60} more`) : null);
  return h(
    "section",
    { class: "card" },
    h("h3", null, "Files changed", h("span", { class: "muted small" }, " (package and cache files left out)")),
    h("div", { class: "file-cols" }, column("Only A", only(pa, pb), "a"), column("Both", both, "both", (p) => ` A ${a.files[p].length}× · B ${b.files[p].length}×`), column("Only B", only(pb, pa), "b")),
  );
}

function toolsCompare(a, b) {
  const count = (trace) => {
    const out = {};
    for (const e of trace.events) if (e.kind === "call") out[e.tool] = (out[e.tool] || 0) + 1;
    return out;
  };
  const ca = count(a);
  const cb = count(b);
  const tools = [...new Set([...Object.keys(ca), ...Object.keys(cb)])].sort((x, y) => (cb[y] || 0) + (ca[y] || 0) - (cb[x] || 0) - (ca[x] || 0));
  const max = Math.max(1, ...Object.values(ca), ...Object.values(cb));
  return h(
    "section",
    { class: "card" },
    h("h3", null, "Tools used"),
    h(
      "div",
      { class: "tool-bars" },
      tools.map((tool) =>
        h(
          "div",
          { class: "tool-bar-row" },
          h("span", { class: "tool-name" }, toolIcon(tool), tool),
          h("span", { class: "tool-bar a" }, widthOf(h("span", { class: "fill" }), (ca[tool] || 0) / max), h("span", { class: "n" }, ca[tool] || 0)),
          h("span", { class: "tool-bar b" }, widthOf(h("span", { class: "fill" }), (cb[tool] || 0) / max), h("span", { class: "n" }, cb[tool] || 0)),
        ),
      ),
    ),
  );
}

function timeline(job, name, trace, side) {
  const rows = trace.events.map((e) => {
    if (e.kind === "message" && e.text) return h("div", { class: "tl-row said" }, icon("message", "row-icon"), h("span", { class: "sum" }, e.text.split("\n")[0]));
    if (e.kind === "stop") return h("a", { class: "tl-row", href: trialHref(job, name, "trace", e.id), target: "_blank", rel: "noopener" }, icon("stop", "row-icon"), h("span", { class: "num" }, "stop"), h("span", { class: "sum" }, "end of the agent's turn"), statusChip(e));
    if (e.kind !== "call") return null;
    return h(
      "a",
      { class: `tl-row${isLabelable(e) ? "" : " not-hooked"}`, href: trialHref(job, name, "trace", e.id), target: "_blank", rel: "noopener", title: e.summary },
      toolIcon(e.tool),
      h("span", { class: "num" }, `#${e.n}`),
      h("span", { class: "sum" }, h("span", { class: "tool" }, e.tool), " ", e.summary),
      e.exit_code ? h("span", { class: "chip st-timeout" }, `exit ${e.exit_code}`) : null,
      statusChip(e),
    );
  });
  return h("section", { class: "card timeline-col" }, h("h3", null, h("span", { class: `side-tag ${side}` }, side.toUpperCase()), name, resultPill(trace.trial.reward, trace.trial.finished, trace.trial.died)), h("div", { class: "tl-list" }, rows));
}

