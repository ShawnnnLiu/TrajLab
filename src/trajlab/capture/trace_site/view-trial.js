// One trial: the header, and the Trace, Replay, and Files tabs.

import {
  $, api, enc, fmtMinutes, fmtUsd, h, icon, isLabelable, plural, put, resultPill, runLinks, setCrumbs,
  stale, state, tabs, trialHref, viewRoot,
} from "./core.js";
import { isJudged, onLabelsChange, renderSaveStatus, restorePending } from "./labels.js";
import { renderFiles } from "./view-files.js";
import { renderReplay } from "./view-replay.js";
import { renderTrace } from "./view-trace.js";

export async function showTrial(r, seq) {
  const loaded = state.trace && state.job === r.job && state.trialName === r.trial;
  if (!loaded) {
    const labelsUrl = `/api/labels/${enc(r.job)}/${enc(r.trial)}?labeler=${enc(state.labeler)}`;
    const [trace, saved] = await Promise.all([api(`/api/trace/${enc(r.job)}/${enc(r.trial)}`), state.labeler ? api(labelsUrl) : Promise.resolve(null)]);
    if (stale(seq)) return;
    state.job = r.job;
    state.trialName = r.trial;
    state.trace = trace;
    state.events = trace.events;
    state.labels = null;
    state.labels = state.labeler ? restorePending(saved) : null;
    state.save.savedAt = null;
    state.save.error = null;
    state.replay = null;
    state.selected = -1;
  }
  if (state.labeler && (state.queueJob !== r.job || !state.queue)) {
    try {
      state.queue = await api(`/api/queue/${enc(r.job)}?labeler=${enc(state.labeler)}`);
      state.queueJob = r.job;
    } catch {
      state.queue = null;
    }
    if (stale(seq)) return;
  }
  setCrumbs([{ text: r.job, href: `#/job/${enc(r.job)}` }, { text: r.trial }]);
  state.tab = r.tab;
  const body = h("div", { id: "tab-body", class: `tab-body tab-${r.tab}` });
  put(viewRoot("trial-page"), h("div", { id: "trialbar", class: "trialbar" }), body);
  renderTrialBar();
  renderSaveStatus();
  loadJobTrials(r.job);
  // Each tab sets state.onLabelsChangeTab to refresh its own marks when a label changes.
  state.onLabelsChangeTab = null;
  onLabelsChange((key) => {
    renderTrialBar();
    if (state.onLabelsChangeTab) state.onLabelsChangeTab(key);
  });
  if (r.tab === "replay") renderReplay(body, r.select);
  else if (r.tab === "files") renderFiles(body, r.select);
  else renderTrace(body, r.select);
}

export function stepStats() {
  const steps = state.events.filter(isLabelable);
  return { total: steps.length, judged: steps.filter((e) => isJudged(e.id)).length };
}

// The trial's place in the labeling queue, and the next trial there that is not done.
function queuePlace() {
  if (!state.queue || state.queueJob !== state.job) return null;
  const order = state.queue.sections.flatMap((s) => s.trials.map((t) => ({ name: t.name, done: t.progress && t.progress.done, section: s })));
  const at = order.findIndex((t) => t.name === state.trialName);
  if (at < 0) return null;
  const next = order.slice(at + 1).find((t) => !t.done) || order.find((t) => !t.done && t.name !== state.trialName);
  const section = order[at].section;
  return { section, index: section.trials.findIndex((t) => t.name === state.trialName), next };
}

const SECTION_NAMES = { pilot: "Labeling pilot", step: "Step-level sample", trial: "Trial-level", all: "Queue" };

async function loadJobTrials(job) {
  if (state.jobTrials && state.jobTrials.job === job) return;
  try {
    const data = await api(`/api/jobs/${enc(job)}`);
    state.jobTrials = { job, trials: data.trials };
    renderTrialBar();
  } catch {
    state.jobTrials = null;
  }
}

function compareSelect() {
  const trials = state.jobTrials && state.jobTrials.job === state.job ? state.jobTrials.trials : [];
  const task = state.trace.trial.task;
  const others = trials.filter((t) => t.name !== state.trialName).sort((a, b) => (a.task === task ? 0 : 1) - (b.task === task ? 0 : 1) || a.name.localeCompare(b.name));
  const select = h(
    "select",
    { class: "input small", title: "Compare this trial with another" },
    h("option", { value: "" }, "Compare with…"),
    others.map((t) => h("option", { value: t.name }, `${t.name}${t.reward === null || t.reward === undefined ? "" : t.reward >= 1 ? "  ✓" : "  ✗"}`)),
  );
  select.addEventListener("change", () => {
    if (select.value) location.hash = `#/compare/${enc(state.job)}/${enc(state.trialName)}/${enc(select.value)}`;
  });
  return select;
}

export function renderTrialBar() {
  const bar = $("#trialbar");
  if (!bar || !state.trace) return;
  const { trial, capture, timing } = state.trace;
  const counts = capture.counts;
  const changedCount = (counts.changed || 0) + (counts.checkpoint || 0);
  const callFacts = [`${changedCount} changed`, `${counts.unchanged || 0} no change`];
  if (counts.baseline) callFacts.push(`${counts.baseline} baseline`);
  const lost = (counts.timeout || 0) + (counts.missing || 0);
  if (lost) callFacts.push(`${lost} not captured`);
  const stats = stepStats();
  const place = queuePlace();
  const job = state.job;
  const tabList = [
    { key: "trace", text: "Trace", icon: "list", href: trialHref(job, state.trialName, "trace") },
    { key: "replay", text: "Replay", icon: "layout", href: trialHref(job, state.trialName, "replay") },
    { key: "files", text: "Files", icon: "folder", href: trialHref(job, state.trialName, "files"), count: Object.keys(state.trace.files).length },
  ];
  put(
    bar,
    h(
      "div",
      { class: "trialbar-main" },
      h(
        "div",
        { class: "trial-title" },
        h("h1", null, trial.task.replace(/^terminal-bench\//, "")),
        h("span", { class: "mono muted small" }, trial.name),
        resultPill(trial.reward, trial.finished, trial.died),
        trial.exception ? h("span", { class: "pill bad" }, trial.exception) : null,
        !trial.finished && !trial.died ? h("span", { class: "pill warn" }, "not finished") : null,
      ),
      h(
        "div",
        { class: "trial-facts" },
        h("span", { class: "fact" }, icon("clock"), `${fmtMinutes(trial.minutes)} min`),
        h("span", { class: "fact" }, icon("terminal"), `${plural(capture.hooked_calls, "hooked call")}: ${callFacts.join(", ")}`),
        h("span", { class: "fact" }, capture.extras ? "diffs + processes" : capture.captured ? "paths only" : "not captured"),
        timing && timing.cost_usd !== null && timing.cost_usd !== undefined ? h("span", { class: "fact" }, fmtUsd(timing.cost_usd)) : null,
        state.labels ? h("span", { class: "fact strong" }, `judged ${stats.judged} of ${stats.total} steps${state.labels.done ? " · done" : ""}`) : null,
        state.trace.source === "hook_files" ? h("span", { class: "pill warn" }, state.events.length ? "no trajectory yet: calls from the hook's files" : "no trajectory and no hook records") : null,
        state.notice ? h("span", { class: "pill warn" }, state.notice) : null,
      ),
      h(
        "div",
        { class: "trial-actions" },
        state.trace.source === "trajectory" ? runLinks(job, state.trialName) : null,
        compareSelect(),
        h("button", { class: "btn small", title: "Reload this trial from disk", onclick: () => {
          state.trace = null;
          window.dispatchEvent(new HashChangeEvent("hashchange"));
        } }, "Reload"),
      ),
    ),
    place
      ? h(
          "div",
          { class: "queue-strip" },
          icon("list"),
          h("span", null, h("strong", null, SECTION_NAMES[place.section.key]), ` · trial ${place.index + 1} of ${place.section.trials.length} · `, place.section.steps ? "label every step" : "whole-trial checklist only"),
          state.queue.second_labeler.includes(state.trialName) ? h("span", { class: "tag accent" }, "second labeler too") : null,
          h("span", { class: "spacer" }),
          place.next ? h("a", { class: "btn small", href: trialHref(job, place.next.name) }, "Next in queue", icon("arrowRight")) : h("span", { class: "muted small" }, "last in the queue"),
        )
      : null,
    tabs(tabList, state.tab),
  );
}
