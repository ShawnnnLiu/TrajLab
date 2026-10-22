// Diffs as rows with line numbers, syntax colors, and the changed words of a changed line marked.

import { h, put } from "./core.js";
import { langOf, paint } from "./code.js";

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;
const WORD = /\s+|[A-Za-z0-9_]+|[^\sA-Za-z0-9_]/g;

export function parseUnified(text) {
  const rows = [];
  let oldNo = 0;
  let newNo = 0;
  for (const line of (text || "").split("\n")) {
    const m = line.match(HUNK);
    if (m) {
      oldNo = m[2] === "0" ? Number(m[1]) + 1 : Number(m[1]);
      newNo = m[4] === "0" ? Number(m[3]) + 1 : Number(m[3]);
      rows.push({ kind: "hunk", text: line });
    } else if (line.startsWith("--- ") || line.startsWith("+++ ")) rows.push({ kind: "file", text: line });
    else if (line.startsWith("[... ")) rows.push({ kind: "cut", text: line });
    else if (line.startsWith("+")) rows.push({ kind: "add", text: line.slice(1), new: newNo++ });
    else if (line.startsWith("-")) rows.push({ kind: "del", text: line.slice(1), old: oldNo++ });
    else if (line.startsWith("\\")) rows.push({ kind: "meta", text: line });
    else if (line.startsWith(" ") || line === "") rows.push({ kind: "ctx", text: line.slice(1), old: oldNo++, new: newNo++ });
    else rows.push({ kind: "meta", text: line });
  }
  return rows;
}

// Rows for an edit's old and new strings (longest common subsequence of lines).
export function lineRows(before, after) {
  const x = (before || "").split("\n");
  const y = (after || "").split("\n");
  if (x.length * y.length > 250_000) {
    return [...x.map((t, i) => ({ kind: "del", text: t, old: i + 1 })), ...y.map((t, i) => ({ kind: "add", text: t, new: i + 1 }))];
  }
  const n = x.length;
  const m = y.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const rows = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) rows.push({ kind: "ctx", text: x[i], old: ++i, new: ++j });
    else if (dp[i + 1][j] >= dp[i][j + 1]) rows.push({ kind: "del", text: x[i], old: ++i });
    else rows.push({ kind: "add", text: y[j], new: ++j });
  }
  while (i < n) rows.push({ kind: "del", text: x[i], old: ++i });
  while (j < m) rows.push({ kind: "add", text: y[j], new: ++j });
  return rows;
}

// Character ranges of the words that differ between two lines, or null when the lines share
// too little for word marks to help.
function wordMarks(a, b) {
  const ta = a.match(WORD) || [];
  const tb = b.match(WORD) || [];
  if (!ta.length || !tb.length || ta.length * tb.length > 60_000) return null;
  const dp = Array.from({ length: ta.length + 1 }, () => new Uint16Array(tb.length + 1));
  for (let i = ta.length - 1; i >= 0; i--) for (let j = tb.length - 1; j >= 0; j--) dp[i][j] = ta[i] === tb[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const same = dp[0][0];
  if (same / Math.max(ta.length, tb.length) < 0.35) return null;
  const keepA = new Array(ta.length).fill(false);
  const keepB = new Array(tb.length).fill(false);
  let i = 0;
  let j = 0;
  while (i < ta.length && j < tb.length) {
    if (ta[i] === tb[j]) {
      keepA[i++] = true;
      keepB[j++] = true;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
    else j++;
  }
  // Changed words become ranges; two ranges with only whitespace between them become one.
  const ranges = (parts, keep, text) => {
    const out = [];
    let at = 0;
    parts.forEach((t, k) => {
      if (!keep[k] && t.trim()) {
        const last = out[out.length - 1];
        if (last && !text.slice(last[1], at).trim()) last[1] = at + t.length;
        else out.push([at, at + t.length]);
      }
      at += t.length;
    });
    return out;
  };
  return [ranges(ta, keepA, a), ranges(tb, keepB, b)];
}

function markWords(rows) {
  let k = 0;
  while (k < rows.length) {
    if (rows[k].kind !== "del") {
      k++;
      continue;
    }
    const dels = [];
    while (k < rows.length && rows[k].kind === "del") dels.push(rows[k++]);
    const adds = [];
    while (k < rows.length && rows[k].kind === "add") adds.push(rows[k++]);
    for (let p = 0; p < Math.min(dels.length, adds.length); p++) {
      const marks = wordMarks(dels[p].text, adds[p].text);
      if (marks) [dels[p].marks, adds[p].marks] = marks;
    }
  }
  return rows;
}

const SIGN = { add: "+", del: "−", ctx: " " };

function rowView(row, lang, numbers) {
  if (row.kind === "hunk") return h("div", { class: "dl dl-hunk" }, numbers ? [h("span", { class: "ln" }), h("span", { class: "ln" })] : null, h("span", { class: "dc" }, row.text));
  if (row.kind === "cut" || row.kind === "meta") return h("div", { class: "dl dl-meta" }, numbers ? [h("span", { class: "ln" }), h("span", { class: "ln" })] : null, h("span", { class: "dc" }, row.text));
  return h(
    "div",
    { class: `dl dl-${row.kind}` },
    numbers ? [h("span", { class: "ln" }, row.old ?? ""), h("span", { class: "ln" }, row.new ?? "")] : null,
    h("span", { class: "sign" }, SIGN[row.kind]),
    h("span", { class: "dc" }, paint(row.text, lang, row.marks)),
  );
}

function rowsView(rows, { path = "", numbers = true, max = 400 } = {}) {
  const lang = langOf(path);
  const shown = rows.filter((r) => r.kind !== "file");
  markWords(shown);
  const box = h("div", { class: `diff${numbers ? "" : " no-numbers"}` });
  const fill = (limit) => put(box, shown.slice(0, limit).map((r) => rowView(r, lang, numbers)));
  fill(max);
  if (shown.length <= max) return box;
  const more = h("button", { class: "btn-link", onclick: () => {
    fill(shown.length);
    more.remove();
  } }, `Show all ${shown.length} lines`);
  return h("div", null, box, more);
}

export const diffView = (text, options) => rowsView(parseUnified(text), options);
export const editView = (before, after, options) => rowsView(lineRows(before, after), { numbers: false, ...options });

export function diffStats(text) {
  let add = 0;
  let del = 0;
  for (const line of (text || "").split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) add++;
    else if (line.startsWith("-") && !line.startsWith("---")) del++;
  }
  return { add, del };
}

// A whole file with line numbers and colors; `changed` lines are marked.
export function fileView(text, { path = "", changed = [], max = 3000 } = {}) {
  const lang = langOf(path);
  const lines = text.split("\n");
  if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  const marked = new Set(changed);
  const box = h("div", { class: "file-view" });
  const fill = (limit) =>
    put(box, lines.slice(0, limit).map((line, i) => h("div", { class: `fl${marked.has(i + 1) ? " fl-changed" : ""}`, "data-line": i + 1 }, h("span", { class: "ln" }, i + 1), h("span", { class: "dc" }, paint(line, lang)))));
  fill(max);
  if (lines.length <= max) return box;
  const more = h("button", { class: "btn-link", onclick: () => {
    fill(lines.length);
    more.remove();
  } }, `Show all ${lines.length} lines`);
  return h("div", null, box, more);
}
