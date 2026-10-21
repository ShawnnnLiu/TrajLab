// Labels: the labeler's judgments of one trial, saved to the server as they change, and kept in
// this browser until the server has them.

import { $, api, enc, h, icon, isLabelable, patternHelp, state, storageGet, storageSet } from "./core.js";

export const TRIAL_KEY = "\u0000trial"; // the whole-trial label's key

let changed = () => {};
export function onLabelsChange(fn) {
  changed = fn;
}

export function getLabel(key) {
  if (!state.labels) return null;
  return key === TRIAL_KEY ? state.labels.trial : state.labels.steps[key] || null;
}

export const isJudged = (key) => {
  const label = getLabel(key);
  return !!label && Array.isArray(label.patterns);
};

export function emptyLabels() {
  return {
    job_name: state.job,
    trial_name: state.trialName,
    labeler: state.labeler,
    trial: null,
    steps: {},
    done: false,
    first_wrong_step: null,
    updated_at: new Date().toISOString(),
  };
}

function touch() {
  state.labels.updated_at = new Date().toISOString();
  scheduleSave();
}

export function changeLabel(key, changes, rerender = true) {
  if (!state.labels) return;
  const current = getLabel(key) || { patterns: null, unsure: false, note: "" };
  const next = { ...current, ...changes, updated_at: new Date().toISOString() };
  const empty = next.patterns === null && !next.unsure && !next.note;
  if (key === TRIAL_KEY) state.labels.trial = empty ? null : next;
  else if (empty) delete state.labels.steps[key];
  else state.labels.steps[key] = next;
  touch();
  changed(key);
  if (rerender) rerenderPanels(key);
}

export function togglePattern(key, pattern) {
  const current = getLabel(key);
  const chosen = new Set((current && current.patterns) || []);
  if (chosen.has(pattern)) chosen.delete(pattern);
  else chosen.add(pattern);
  const ordered = state.meta.patterns.map((p) => p.key).filter((k) => chosen.has(k));
  // Unticking the last pattern leaves the step unjudged; "None of these" is the explicit no.
  changeLabel(key, { patterns: ordered.length ? ordered : null });
}

export function markNone(key) {
  const current = getLabel(key);
  const isNone = !!current && Array.isArray(current.patterns) && current.patterns.length === 0;
  changeLabel(key, { patterns: isNone ? null : [] });
}

export function toggleUnsure(key) {
  const current = getLabel(key);
  changeLabel(key, { unsure: !(current && current.unsure) });
}

export function setDone(done) {
  if (!state.labels) return;
  state.labels.done = done;
  touch();
  changed(TRIAL_KEY);
}

export function toggleFirstWrong(callId) {
  if (!state.labels) return;
  const previous = state.labels.first_wrong_step;
  state.labels.first_wrong_step = previous === callId ? null : callId;
  touch();
  if (previous) {
    changed(previous);
    rerenderPanels(previous);
  }
  changed(callId);
  rerenderPanels(callId);
}

// ---------- saving ----------

const pendingKey = (labels) => `trajlab.pending.${labels.job_name}/${labels.trial_name}/${labels.labeler}`;

export function restorePending(saved) {
  const blank = emptyLabels();
  let pending = null;
  try {
    pending = JSON.parse(storageGet(pendingKey(blank)) || "null");
  } catch {
    pending = null;
  }
  const newer = pending && (!saved || Date.parse(pending.updated_at) > Date.parse(saved.updated_at));
  if (newer) {
    state.notice = "Restored labels this browser had not saved yet; saving them now.";
    setTimeout(scheduleSave, 0);
    return { ...blank, ...pending };
  }
  if (pending) storageSet(pendingKey(blank), null);
  return saved ? { ...blank, ...saved } : blank;
}

export function scheduleSave() {
  if (!state.labels) return;
  state.save.dirty = true;
  storageSet(pendingKey(state.labels), JSON.stringify(state.labels));
  clearTimeout(state.save.timer);
  state.save.timer = setTimeout(() => flushSave(), 400);
  renderSaveStatus();
}

export async function flushSave() {
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

export function renderSaveStatus() {
  const el = $("#save-status");
  if (!el) return;
  const s = state.save;
  let text = "";
  let cls = "";
  if (!state.labels) text = "";
  else if (s.error && s.dirty) [text, cls] = [`Not saved: ${s.error}. Retrying…`, "err"];
  else if (s.inflight || s.dirty) [text, cls] = ["Saving…", "busy"];
  else if (s.savedAt) [text, cls] = [`Saved ${s.savedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`, "ok"];
  else [text, cls] = ["All labels saved", "ok"];
  el.textContent = text;
  el.className = `save-status ${cls}`;
}

window.addEventListener("beforeunload", (event) => {
  if (state.save.dirty || state.save.inflight) {
    event.preventDefault();
    event.returnValue = "";
  }
});

// ---------- the checklist ----------

export function verdictOf(key) {
  const label = getLabel(key);
  if (!label || !Array.isArray(label.patterns)) return { cls: "open", text: "not judged" };
  if (!label.patterns.length) return { cls: "no", text: "none" };
  return { cls: "yes", text: label.patterns.map((p) => state.meta.patterns.find((x) => x.key === p)?.title || p).join(", ") };
}

export function labelPanel(key, title, { compact = false, callId = null } = {}) {
  const panel = h("div", { class: `labelpanel${compact ? " compact" : ""}` });
  panel.dataset.key = key;
  panel.dataset.title = title;
  panel.dataset.compact = compact ? "1" : "";
  panel.dataset.call = callId || "";
  fillPanel(panel);
  return panel;
}

function fillPanel(panel) {
  const key = panel.dataset.key;
  const title = panel.dataset.title;
  const compact = !!panel.dataset.compact;
  const callId = panel.dataset.call || null;
  panel.replaceChildren();
  if (!state.labeler) {
    panel.append(h("p", { class: "muted" }, "Type your name in Labeler (top right) to label."));
    return;
  }
  const label = getLabel(key);
  const judged = !!label && Array.isArray(label.patterns);
  const chosen = new Set((label && label.patterns) || []);
  const box = (hint, text, on, onToggle, help, kind) => {
    const input = h("input", { type: "checkbox", onchange: onToggle });
    input.checked = on;
    return h("label", { class: `chip-check ${kind}${on ? " on" : ""}`, title: help }, input, h("span", null, text), h("kbd", null, hint));
  };
  const patterns = state.meta.patterns.map((p, i) => box(String(i + 1), p.title, chosen.has(p.key), () => togglePattern(key, p.key), patternHelp(p.key), "pattern"));
  const none = box("0", "None of these", judged && chosen.size === 0, () => markNone(key), "Judged: no pattern occurred here.", "none");
  const unsure = box("u", "Unsure", !!(label && label.unsure), () => toggleUnsure(key), "Mark this judgment for a second look.", "unsure");
  const isWrong = callId && state.labels.first_wrong_step === callId;
  const wrong = callId
    ? h("button", { class: `chip-check wrong${isWrong ? " on" : ""}`, title: "The call where the trial first went wrong (one per trial).", onclick: () => toggleFirstWrong(callId) }, icon("flag"), isWrong ? "First wrong step" : "Mark first wrong step", h("kbd", null, "w"))
    : null;
  const note = h("textarea", { class: "note-field", rows: compact ? 1 : 2, placeholder: "Notes: what you compared, why you judged it so…", maxlength: 20000 });
  note.value = (label && label.note) || "";
  note.addEventListener("input", () => changeLabel(key, { note: note.value }, false));
  const verdict = verdictOf(key);
  panel.append(
    h(
      "div",
      { class: "lp-head" },
      h("span", { class: "lp-title" }, title),
      h("span", { class: `verdict ${verdict.cls}` }, verdict.text),
      compact ? null : h("span", { class: "lp-keys" }, "1–8 toggle · 0 none · u unsure · w first wrong · n note"),
    ),
    h("div", { class: "lp-checks" }, patterns, h("span", { class: "divider" }), none, unsure, wrong),
    note,
  );
}

function rerenderPanels(key) {
  for (const panel of document.querySelectorAll(".labelpanel")) {
    if (panel.dataset.key === key) {
      const focused = panel.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA";
      if (!focused) fillPanel(panel);
    }
  }
}

export function refreshAllPanels() {
  for (const panel of document.querySelectorAll(".labelpanel")) fillPanel(panel);
}

// Keys shared by every view that shows a checklist: returns true when the key was used.
export function labelKey(event, key, callId) {
  if (key === null || !state.labels) return false;
  const k = event.key;
  if (k === "0") markNone(key);
  else if (k === "u") toggleUnsure(key);
  else if (k === "w" && callId) toggleFirstWrong(callId);
  else if (k === "n") {
    const field = document.querySelector(`.labelpanel textarea`);
    if (field) field.focus();
  } else if (/^[1-9]$/.test(k) && Number(k) <= state.meta.patterns.length) togglePattern(key, state.meta.patterns[Number(k) - 1].key);
  else return false;
  return true;
}

export { isLabelable };
