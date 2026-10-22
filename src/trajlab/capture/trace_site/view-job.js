// One experiment: its trials, the labeling queue, and the agreement between labelers.

import {
  api, enc, exportBar, fmtMinutes, h, icon, labelerNotice, note, patternTitle, plural, progressBar, put,
  resultPill, runLinks, setCrumbs, stale, state, tabs, trialHref, viewRoot,
} from "./core.js";

const SECTION_TEXT = {
  pilot: ["Labeling pilot", "Label every step. These 5 tune the guide and thresholds; they are not scored."],
  step: ["Step-level sample", "Label every step. Stratified by task and outcome; the marked ones are labeled again by a second labeler."],
  trial: ["Trial-level", "The whole-trial checklist only."],
  all: ["All trials", "Too few finished trials for ADR-0015's draw, so every trial is listed."],
};

export async function showJob(r, seq) {
  const job = r.job;
  const query = state.labeler ? `?labeler=${enc(state.labeler)}` : "";
  const data = await api(`/api/jobs/${enc(job)}${query}`);
  if (stale(seq)) return;
  setCrumbs([{ text: job }]);
  const view = viewRoot("page");
  const body = h("div", { class: "tab-body" });
  put(
    view,
    h(
      "div",
      { class: "page-head row-between" },
      h("div", null, h("h1", null, job), h("p", { class: "muted" }, `${plural(data.trials.length, "trial")} · labelers so far: ${data.labelers.length ? data.labelers.join(", ") : "none"}`)),
      exportBar({ job }, "this experiment"),
    ),
    labelerNotice(),
    tabs(
      [
        { key: "trials", text: "Trials", href: `#/job/${enc(job)}`, count: data.trials.length },
        { key: "queue", text: "Labeling queue", icon: "list", href: `#/job/${enc(job)}/queue` },
        { key: "agreement", text: "Agreement", icon: "users", href: `#/job/${enc(job)}/agreement` },
      ],
      r.tab,
    ),
    body,
  );
  if (r.tab === "queue") await showQueue(job, body, seq);
  else if (r.tab === "agreement") await showAgreement(job, body, seq);
  else showTrials(job, data, body);
}

// ---------- trials ----------

function labelCell(t) {
  const p = t.progress;
  if (!p) return h("td", { class: "muted" }, "–");
  const steps = t.hooked_calls + t.stops;
  return h(
    "td",
    { class: "labels-cell" },
    progressBar(p.steps_judged, steps || 1),
    h("span", { class: "small muted" }, `${p.steps_judged}/${steps}`),
    p.trial_judged ? h("span", { class: "tag", title: "whole-trial label done" }, "trial") : null,
    p.first_wrong ? h("span", { class: "tag flag", title: "first wrong step marked" }, icon("flag")) : null,
    p.done ? h("span", { class: "tag ok", title: "finished" }, icon("check")) : null,
  );
}

function showTrials(job, data, body) {
  const tasks = [...new Set(data.trials.map((t) => t.task))].sort();
  const filter = { text: "", task: "", outcome: "" };
  const short = (task) => task.replace(/^terminal-bench\//, "");
  const picked = new Set();
  const search = h("input", { type: "search", class: "input", placeholder: "Filter trials…" });
  const taskSelect = h("select", { class: "input" }, h("option", { value: "" }, "All tasks"), tasks.map((t) => h("option", { value: t }, short(t))));
  const outcomeSelect = h("select", { class: "input" }, ["", "passed", "failed", "none"].map((o) => h("option", { value: o }, { "": "Any outcome", passed: "Passed", failed: "Failed", none: "No result" }[o])));
  const compare = h("button", { class: "btn primary", disabled: true, onclick: () => {
    const [a, b] = [...picked];
    location.hash = `#/compare/${enc(job)}/${enc(a)}/${enc(b)}`;
  } }, icon("columns"), "Compare 2 selected");
  const tbody = h("tbody");
  const render = () => {
    const rows = data.trials.filter((t) => {
      if (filter.task && t.task !== filter.task) return false;
      const outcome = t.reward === null || t.reward === undefined ? "none" : t.reward >= 1 ? "passed" : "failed";
      if (filter.outcome && outcome !== filter.outcome) return false;
      return !filter.text || `${t.name} ${t.task}`.toLowerCase().includes(filter.text);
    });
    put(
      tbody,
      rows.map((t) => {
        const box = h("input", { type: "checkbox", title: "Pick two to compare" });
        box.checked = picked.has(t.name);
        box.addEventListener("change", () => {
          if (box.checked) picked.add(t.name);
          else picked.delete(t.name);
          if (picked.size > 2) {
            picked.delete([...picked][0]);
            render();
          }
          compare.disabled = picked.size !== 2;
        });
        return h(
          "tr",
          { class: t.progress && t.progress.done ? "done" : null },
          h("td", null, box),
          h("td", null, h("a", { class: "trial-link", href: trialHref(job, t.name) }, t.name)),
          h("td", { class: "muted", title: t.task }, t.task.replace(/^terminal-bench\//, "")),
          h("td", null, resultPill(t.reward, t.finished, t.died), t.exception ? h("span", { class: "pill bad", title: t.exception }, t.exception) : null),
          h("td", { class: "num" }, fmtMinutes(t.minutes)),
          h("td", { class: "num" }, t.hooked_calls),
          h("td", { class: "num" }, t.checkpoints),
          h("td", { class: `num${t.timeouts ? " bad" : ""}` }, t.timeouts),
          h("td", { class: "muted small" }, t.extras ? "diffs" : t.hooked_calls ? "paths" : "–"),
          labelCell(t),
          h("td", null, t.has_trajectory ? runLinks(job, t.name) : null),
        );
      }),
    );
  };
  search.addEventListener("input", () => {
    filter.text = search.value.toLowerCase();
    render();
  });
  taskSelect.addEventListener("change", () => {
    filter.task = taskSelect.value;
    render();
  });
  outcomeSelect.addEventListener("change", () => {
    filter.outcome = outcomeSelect.value;
    render();
  });
  const head = ["", "Trial", "Task", "Result", "Min", "Hooked calls", "Ckpts", "Timeouts", "Capture", "Your labels", "Run"];
  put(
    body,
    h("div", { class: "toolbar" }, search, taskSelect, outcomeSelect, h("span", { class: "spacer" }), compare),
    h("div", { class: "table-card" }, h("table", { class: "table" }, h("thead", null, h("tr", null, head.map((t) => h("th", null, t)))), tbody)),
  );
  render();
}

// ---------- the labeling queue ----------

async function showQueue(job, body, seq) {
  const query = state.labeler ? `?labeler=${enc(state.labeler)}` : "";
  const queue = await api(`/api/queue/${enc(job)}${query}`);
  if (stale(seq)) return;
  state.queue = queue;
  state.queueJob = job;
  const ordered = queue.sections.flatMap((s) => s.trials.map((t) => ({ ...t, section: s })));
  const next = ordered.find((t) => !(t.progress && t.progress.done));
  const second = new Set(queue.second_labeler);
  put(
    body,
    h(
      "div",
      { class: "queue-head" },
      next
        ? h("a", { class: "btn primary big", href: trialHref(job, next.name) }, "Continue: ", h("span", { class: "mono" }, next.name), icon("arrowRight"))
        : h("span", { class: "pill ok" }, ordered.length ? "Every trial in the queue is marked done" : "No finished trials yet"),
      queue.drawn ? note("Drawn with fixed seeds, so the queue is the same every time (ADR-0015 §8): 5 pilot trials from 5 different tasks; then 12 trials covering every task and, where a task has both, a pass and a fail; then the rest. Six of the 12 are for a second labeler.") : null,
    ),
    queue.sections.map((s) => {
      const [title, text] = SECTION_TEXT[s.key];
      const done = s.trials.filter((t) => t.progress && t.progress.done).length;
      return h(
        "section",
        { class: "card queue-section" },
        h("div", { class: "row-between" }, h("div", null, h("h3", null, title, h("span", { class: "count" }, s.trials.length)), h("p", { class: "muted" }, text)), state.labeler ? h("div", { class: "section-progress" }, progressBar(done, s.trials.length || 1), h("span", { class: "small muted" }, `${done} of ${s.trials.length} done`)) : null),
        h(
          "table",
          { class: "table compact" },
          h("tbody", null, s.trials.map((t) => {
            const steps = t.hooked_calls + t.stops;
            const p = t.progress;
            return h(
              "tr",
              { class: p && p.done ? "done" : null },
              h("td", null, h("a", { class: "trial-link", href: trialHref(job, t.name) }, t.name)),
              h("td", null, resultPill(t.reward, t.finished, t.died)),
              h("td", null, h("span", { class: "tag" }, s.steps ? "every step" : "whole trial"), second.has(t.name) ? h("span", { class: "tag accent" }, "second labeler") : null),
              h("td", { class: "labels-cell" }, p ? [progressBar(s.steps ? p.steps_judged : p.trial_judged ? 1 : 0, s.steps ? steps || 1 : 1), h("span", { class: "small muted" }, s.steps ? `${p.steps_judged}/${steps} steps` : p.trial_judged ? "judged" : "not judged")] : null),
              h("td", null, p && p.done ? h("span", { class: "tag ok" }, icon("check"), "done") : null),
            );
          })),
        ),
      );
    }),
  );
}

// ---------- agreement ----------

async function showAgreement(job, body, seq, a = null, b = null) {
  const params = new URLSearchParams();
  if (a) params.set("a", a);
  if (b) params.set("b", b);
  const data = await api(`/api/agreement/${enc(job)}?${params}`);
  if (stale(seq)) return;
  const pick = (value, onchange) => {
    const select = h("select", { class: "input" }, data.labelers.map((l) => h("option", { value: l }, l)));
    select.value = value || "";
    select.addEventListener("change", () => onchange(select.value));
    return select;
  };
  const header = h(
    "div",
    { class: "toolbar" },
    h("span", { class: "muted" }, "Compare"),
    pick(data.a, (v) => showAgreement(job, body, seq, v, data.b)),
    h("span", { class: "muted" }, "with"),
    pick(data.b, (v) => showAgreement(job, body, seq, data.a, v)),
  );
  if (!data.result) {
    put(body, data.labelers.length >= 2 ? header : null, h("div", { class: "empty" }, icon("users"), h("p", null, "Agreement needs two labelers who labeled the same trials. ADR-0015 §8 has a second labeler redo 6 of the 12 step-level trials (they are marked in the labeling queue).")));
    return;
  }
  const result = data.result;
  const kappa = (k) => (k === null || k === undefined ? h("span", { class: "muted", title: "Undefined: no items, or both always gave the same answer" }, "–") : h("span", { class: `kappa ${k >= 0.6 ? "ok" : "warn"}` }, k.toFixed(2)));
  const table = (level, title) =>
    h(
      "section",
      { class: "card" },
      h("h3", null, title, h("span", { class: "count" }, plural(result.items[level], level === "trial" ? "trial" : "step"))),
      h(
        "table",
        { class: "table compact" },
        h("thead", null, h("tr", null, ["Pattern", "Both yes", `Only ${data.a}`, `Only ${data.b}`, "Both no", "κ"].map((t) => h("th", null, t)))),
        h("tbody", null, result.table[level].map((row) => h("tr", null, h("td", null, patternTitle(row.pattern)), h("td", { class: "num" }, row.both_yes), h("td", { class: "num" }, row.a_only), h("td", { class: "num" }, row.b_only), h("td", { class: "num" }, row.both_no), h("td", { class: "num" }, kappa(row.kappa))))),
      ),
    );
  const wrong = result.first_wrong;
  put(
    body,
    header,
    h("p", { class: "muted" }, `${plural(result.trials.length, "trial")} labeled by both. ADR-0015 §8 scores a pattern only if κ is at least 0.6 and it has at least 10 positive labels.`),
    h("div", { class: "grid-2" }, table("trial", "Whole trial"), table("step", "Each step")),
    h("section", { class: "card" }, h("h3", null, "First wrong step"), h("p", null, wrong.both_marked ? `Both marked one in ${plural(wrong.both_marked, "trial")}; the same call in ${wrong.same}.` : "No trial has a first wrong step from both.")),
    h(
      "section",
      { class: "card" },
      h("h3", null, "Disagreements", h("span", { class: "count" }, result.disagreements.length)),
      result.disagreements.length
        ? h(
            "table",
            { class: "table compact" },
            h("thead", null, h("tr", null, ["Trial", "Where", "Pattern", data.a, data.b].map((t) => h("th", null, t)))),
            h("tbody", null, result.disagreements.map((d) => h("tr", null, h("td", null, h("a", { class: "trial-link", href: trialHref(job, d.trial, "trace", d.tool_call_id || null) }, d.trial)), h("td", null, d.level === "trial" ? "whole trial" : d.call ? `#${d.call}` : "stop"), h("td", null, patternTitle(d.pattern)), h("td", null, d.a ? "yes" : "no"), h("td", null, d.b ? "yes" : "no")))),
          )
        : note("None."),
    ),
  );
}
