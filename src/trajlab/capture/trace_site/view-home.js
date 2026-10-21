// The experiments page: one card per job dir.

import { api, enc, exportBar, h, icon, labelerNotice, progressBar, put, setCrumbs, stale, state, viewRoot } from "./core.js";

export async function showHome(seq) {
  const query = state.labeler ? `?labeler=${enc(state.labeler)}` : "";
  const data = await api(`/api/jobs${query}`);
  if (stale(seq)) return;
  setCrumbs([]);
  put(
    viewRoot("page"),
    h("div", { class: "page-head" }, h("h1", null, "Experiments"), h("p", { class: "muted" }, `Job dirs under ${data.root}. Labels are saved under ${state.meta.labels_dir}.`)),
    h("a", { class: "callout", href: "#/tutorial" }, icon("book"), h("span", null, h("strong", null, "New here? "), "Read the tutorial: how to read a trial, label it, and export your labels (10 minutes)."), icon("arrowRight")),
    labelerNotice(),
    data.jobs.length ? h("div", { class: "cards" }, data.jobs.map(card)) : h("div", { class: "empty" }, "No job dirs found."),
    exportBar({}, "every experiment"),
  );
}

function card(job) {
  const href = (tab) => `#/job/${enc(job.name)}${tab ? `/${tab}` : ""}`;
  const passRate = job.finished ? job.passed / job.finished : 0;
  const capture = job.extras ? `diffs and processes in ${job.extras}` : job.captured ? "changed paths only" : "not captured";
  return h(
    "article",
    { class: "card job-card" },
    h("a", { class: "card-title", href: href("") }, job.name),
    h("div", { class: "card-stats" }, stat(job.trials, "trials"), stat(job.finished, "finished"), stat(job.passed, "passed")),
    h("div", { class: "card-row" }, h("span", { class: "muted" }, "Pass rate"), progressBar(job.passed, job.finished || 1, "ok"), h("span", { class: "num" }, `${Math.round(passRate * 100)}%`)),
    h("div", { class: "card-row" }, h("span", { class: "muted" }, "Capture"), h("span", null, capture)),
    state.labeler ? h("div", { class: "card-row" }, h("span", { class: "muted" }, "You finished"), progressBar(job.done, job.trials || 1), h("span", { class: "num" }, `${job.done} of ${job.trials}`)) : null,
    h("div", { class: "card-row" }, h("span", { class: "muted" }, "Labelers"), job.labelers.length ? job.labelers.map((l) => h("span", { class: "tag" }, l)) : h("span", { class: "muted" }, "none yet")),
    h(
      "div",
      { class: "card-actions" },
      h("a", { class: "btn", href: href("") }, "Trials"),
      h("a", { class: "btn", href: href("queue") }, icon("list"), "Queue"),
      h("a", { class: "btn", href: href("agreement") }, icon("users"), "Agreement"),
    ),
  );
}

const stat = (n, label) => h("div", { class: "stat" }, h("span", { class: "stat-n" }, n), h("span", { class: "stat-l" }, label));
