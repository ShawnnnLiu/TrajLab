// The labeling guide: a drawer beside every page, from the Markdown file `trajlab trace --guide`
// names. Headings named after a pattern also become the checklist's definitions.

import { $, api, h, icon, put, state } from "./core.js";
import { markdown } from "./md.js";

export async function loadGuide() {
  try {
    state.guide = await api("/api/guide");
  } catch {
    state.guide = { path: null, text: null, sections: {} };
  }
}

export function toggleGuide(open = !state.guideOpen) {
  state.guideOpen = open;
  const drawer = $("#guide-drawer");
  drawer.hidden = !open;
  document.body.classList.toggle("guide-open", open);
  $("#guide-button").classList.toggle("active", open);
  if (open) renderGuide();
}

export function renderGuide() {
  const drawer = $("#guide-drawer");
  const guide = state.guide || {};
  const body = guide.text
    ? markdown(guide.text)
    : h(
        "div",
        null,
        h("p", { class: "note" }, `No guide yet. Write ${guide.path || "docs/labeling-guide.md"} in Markdown, and restart the site. A heading that starts with a pattern's name (## Loop) makes its first paragraph that pattern's definition in the checklist.`),
        h("h4", { class: "md-h" }, "Current definitions (ADR-0015 §1)"),
        h("dl", { class: "defs" }, (state.meta ? state.meta.patterns : []).map((p) => [h("dt", null, p.title), h("dd", null, p.help)])),
      );
  put(
    drawer,
    h("div", { class: "drawer-head" }, icon("book"), h("strong", null, "Labeling guide"), guide.path ? h("span", { class: "muted mono small", title: guide.path }, guide.path.split("/").pop()) : null, h("button", { class: "icon-btn", title: "Close (g)", onclick: () => toggleGuide(false) }, icon("x"))),
    h("div", { class: "drawer-body" }, body),
  );
}
