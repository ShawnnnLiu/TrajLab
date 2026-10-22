// The Files tab: every path the trial changed, and each path's history across calls.

import { api, enc, fmtBytes, h, icon, note, plural, put, shortHash, state, toolIcon, trialHref } from "./core.js";
import { diffStats, diffView, fileView } from "./diff.js";

const NOISY = /\/(site-packages|dist-packages|node_modules|__pycache__|\.cache|\.git|\.npm|\.cargo|\.venv|venv)\//;
const OP = { added: ["A", "added"], modified: ["M", "modified"], removed: ["D", "deleted"] };

export function renderFiles(body, select) {
  const files = state.trace.files;
  const paths = Object.keys(files).sort();
  if (!paths.length) {
    put(body, h("div", { class: "empty" }, icon("folder"), h("p", null, "No call in this trial changed a file.")));
    return;
  }
  const quiet = paths.filter((p) => !NOISY.test(p));
  const noisy = paths.filter((p) => NOISY.test(p));
  let active = select && files[select] ? select : quiet[0] || paths[0];
  const filter = { text: "", noisy: false };
  const list = h("div", { class: "list" });
  const detail = h("section", { class: "detail files-detail" });
  const search = h("input", { type: "search", class: "input", placeholder: "Filter paths…" });
  const noisyToggle = h("input", { type: "checkbox" });
  const renderList = () => {
    const shown = (filter.noisy ? paths : quiet).filter((p) => !filter.text || p.toLowerCase().includes(filter.text));
    put(
      list,
      shown.map((path) => {
        const versions = files[path];
        const last = versions[versions.length - 1];
        const ops = [...new Set(versions.map((v) => v.op))];
        const row = h(
          "div",
          { class: `row file-row${path === active ? " selected" : ""}`, title: path },
          icon("file", "row-icon"),
          h("span", { class: "sum mono" }, h("span", { class: "muted" }, path.slice(0, path.lastIndexOf("/") + 1)), path.slice(path.lastIndexOf("/") + 1)),
          h("span", { class: "marks" }, ops.map((op) => h("span", { class: `op op-${op}` }, OP[op][0])), h("span", { class: "chip st-unchanged" }, `${versions.length}×`), h("span", { class: "muted small" }, last.call ? `#${last.call}` : "stop")),
        );
        row.addEventListener("click", () => {
          active = path;
          history.replaceState(null, "", trialHref(state.job, state.trialName, "files", path));
          renderList();
          renderDetail();
        });
        return row;
      }),
      !filter.noisy && noisy.length ? h("p", { class: "muted small pad" }, `${plural(noisy.length, "package or cache file")} hidden.`) : null,
    );
  };
  const renderDetail = () => put(detail, h("div", { class: "detail-body" }, fileHistory(active)));
  search.addEventListener("input", () => {
    filter.text = search.value.toLowerCase();
    renderList();
  });
  noisyToggle.addEventListener("change", () => {
    filter.noisy = noisyToggle.checked;
    renderList();
  });
  put(
    body,
    h(
      "div",
      { class: "panes" },
      h(
        "aside",
        { class: "list-pane" },
        h("div", { class: "list-controls" }, search, h("div", { class: "toggles" }, h("label", { class: "toggle small" }, noisyToggle, h("span", null, `package and cache files (${noisy.length})`)), h("span", { class: "muted small" }, plural(paths.length, "path")))),
        list,
      ),
      detail,
    ),
  );
  renderList();
  renderDetail();
  state.keyHandler = null;
  state.onHints = renderDetail;
}

function fileHistory(path) {
  const versions = state.trace.files[path];
  const first = versions[0];
  const original = first && first.before_sha256;
  const seen = [];
  return [
    h(
      "div",
      { class: "detail-head" },
      icon("file", "head-icon"),
      h("h2", { class: "mono" }, path),
      h("span", { class: "muted small" }, `changed by ${plural(versions.length, "call")}`),
      h("span", { class: "spacer" }),
      h("a", { class: "btn small", href: trialHref(state.job, state.trialName, "replay", versions[versions.length - 1].id) }, icon("layout"), "Open in replay"),
    ),
    first && first.op === "modified" ? note("It existed before the trial: its first diff is against the file the task started with.") : null,
    versions.map((v, k) => {
      const event = state.events[v.event];
      const entry = event && event.changes ? event.changes.files.find((f) => f.path === path) : null;
      const stats = entry && entry.diff ? diffStats(entry.diff) : null;
      let same = null;
      if (state.hints && v.sha256) {
        const earlier = seen.find((x) => x.sha256 === v.sha256);
        if (original && v.sha256 === original && k > 0) same = "back to its content before the trial";
        else if (earlier && seen[seen.length - 1].sha256 !== v.sha256) same = `back to its content after #${earlier.call}`;
      }
      seen.push(v);
      const whole = h("div", { class: "whole-file" });
      const showWhole = h("button", { class: "btn small", onclick: async () => {
        put(whole, h("p", { class: "muted" }, "Loading…"));
        try {
          const view = await api(`/api/file/${enc(state.job)}/${enc(state.trialName)}?path=${enc(path)}&call=${enc(v.id)}`);
          if (view.text === null || view.text === undefined) put(whole, note(view.binary ? `A binary file (${view.size} bytes).` : view.note));
          else put(whole, h("p", { class: "muted small" }, view.note), fileView(view.text, { path, changed: view.changed_lines, max: 3000 }));
        } catch (error) {
          put(whole, note(error.message));
        }
        showWhole.remove();
      } }, `Whole file after this call`);
      return h(
        "article",
        { class: "card version" },
        h(
          "div",
          { class: "version-head" },
          event ? toolIcon(event.tool) : null,
          h("a", { class: "strong", href: trialHref(state.job, state.trialName, "trace", v.id) }, v.call ? `#${v.call} ${event ? event.tool : ""}` : "stop"),
          h("span", { class: `op op-${v.op}` }, OP[v.op] ? OP[v.op][0] : "?"),
          h("span", { class: "muted small" }, OP[v.op] ? OP[v.op][1] : v.op),
          stats ? h("span", { class: "diffstat" }, h("span", { class: "add" }, `+${stats.add}`), h("span", { class: "del" }, `−${stats.del}`)) : null,
          v.size !== null && v.size !== undefined ? h("span", { class: "muted small" }, fmtBytes(v.size)) : null,
          v.before_sha256 || v.sha256 ? h("span", { class: "muted small mono" }, `${shortHash(v.before_sha256) || "new"} → ${shortHash(v.sha256) || (v.op === "removed" ? "gone" : "?")}`) : null,
          v.skipped ? h("span", { class: "pill warn" }, v.skipped.replace(/_/g, " ")) : null,
          same ? h("span", { class: "hint inline" }, same) : null,
          h("span", { class: "spacer" }),
          v.op !== "removed" ? showWhole : null,
        ),
        entry && entry.diff ? diffView(entry.diff, { path, max: 600 }) : v.op === "removed" ? null : note(event && event.changes ? "No diff recorded." : "No diff recorded: paths only (no --extras)."),
        whole,
      );
    }),
  ];
}
