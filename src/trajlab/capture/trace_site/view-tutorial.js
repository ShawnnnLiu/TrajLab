// The tutorial: tutorial.md, with each ```sample block replaced by real pieces of the page, so
// what it explains looks the way it does in a trial.

import { h, icon, put, resultPill, setCrumbs, stale, viewRoot } from "./core.js";
import { diffView } from "./diff.js";
import { markdown } from "./md.js";

const SAMPLE = /^```sample\n([\w-]+)\n```$/m;

export async function showTutorial(r, seq) {
  const response = await fetch("/static/tutorial.md");
  const text = await response.text();
  if (stale(seq)) return;
  setCrumbs([{ text: "Tutorial" }]);
  const body = h("article", { class: "tutorial-body" });
  text.split(SAMPLE).forEach((part, i) => body.append(i % 2 ? sample(part) : markdown(part)));
  const links = [];
  for (const heading of body.querySelectorAll("h3.md-h")) {
    const id = heading.textContent.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    heading.id = id;
    links.push(h("a", { href: `#/tutorial/${id}` }, heading.textContent));
  }
  put(viewRoot("page tutorial-page"), h("div", { class: "tutorial" }, h("nav", { class: "tutorial-toc" }, h("div", { class: "toc-title" }, "On this page"), links), body));
  const target = r.section && document.getElementById(r.section);
  if (target) target.scrollIntoView({ block: "start" });
}

function example(caption, ...content) {
  return h("figure", { class: "sample" }, h("div", { class: "sample-body" }, content), caption ? h("figcaption", null, caption) : null);
}

const legendItem = (cls, text) => h("span", { class: "legend-item" }, h("span", { class: `cell ${cls}` }), text);

function sample(name) {
  switch (name) {
    case "pills":
      return example("A trial's result, and problems the trial ran into.", resultPill(1), resultPill(0), resultPill(null, false, true), h("span", { class: "pill warn" }, "not finished"), h("span", { class: "pill bad" }, "AgentTimeoutError"));
    case "strip":
      return example(
        "Each square is one hooked call. The line along its top: grey for not judged, green for none of the patterns, red for a pattern. A dot under a square marks the first wrong step.",
        h("div", { class: "strip sample-strip" }, ["st-baseline lab-no", "st-changed lab-no", "st-changed lab-yes", "st-unchanged lab-yes", "st-unchanged lab-yes wrong", "st-changed lab-no", "st-unchanged lab-open", "st-timeout lab-open", "st-changed lab-open"].map((cls) => h("span", { class: `cell ${cls}` }))),
        h("div", { class: "legend" }, legendItem("st-changed", "changed files"), legendItem("st-baseline", "baseline"), legendItem("st-unchanged", "no change"), legendItem("st-timeout", "timeout or no record")),
      );
    case "chips": {
      const row = (mark, text) => h("div", { class: "sample-row" }, h("span", { class: "sample-mark" }, mark), h("span", null, text));
      return example(
        null,
        row(h("span", { class: "chip st-changed" }, "+1 ~2"), "added 1 file and modified 2; a number in brackets is the total when more paths changed than were recorded"),
        row(h("span", { class: "chip st-baseline" }, "baseline"), "the first measured call"),
        row(h("span", { class: "chip st-unchanged" }, "no change"), "the filesystem did not change"),
        row(h("span", { class: "chip st-timeout" }, "exit 1"), "the command failed"),
        row(h("span", { class: "lab open" }, "○"), "you have not judged this call"),
        row(h("span", { class: "lab no" }, "✓"), "you judged it: none of the patterns"),
        row(h("span", { class: "lab yes" }, "Loop"), "you ticked a pattern (or several: \"2 patterns\")"),
        row(h("span", { class: "lab open" }, "? ✎"), "marked unsure, and has a note"),
        row(icon("flag", "flag-icon"), "the trial's first wrong step"),
      );
    }
    case "diff":
      return example(
        "Green lines were added and red lines removed; the darker marks are the words that changed inside the line.",
        h("div", { class: "file-card" }, diffView("--- a/app/parse.py\n+++ b/app/parse.py\n@@ -1,5 +1,5 @@\n import csv\n \n def parse(line):\n-    return line.split(\",\")\n+    return next(csv.reader([line]))\n # end", { path: "parse.py" })),
      );
    case "checklist":
      return example("An example: click the patterns to see how they look when ticked. It is not saved.", checklistDemo());
    case "explorer": {
      const row = (cls, badge, name, when) => h("div", { class: `tree-row file ${cls}` }, icon("file", "tree-icon"), h("span", { class: "tree-name" }, name), h("span", { class: "tree-when" }, when), badge ? h("span", { class: `op op-${{ A: "added", M: "modified", D: "removed" }[badge]}` }, badge) : null);
      return example(
        "A: added, M: modified, D: deleted, with the call that last changed it. Highlighted: this call changed it. Faded: a later call creates it.",
        h(
          "div",
          { class: "sample-tree" },
          h("div", { class: "tree-row dir now" }, icon("chevronDown", "tree-icon"), icon("folder", "tree-icon"), h("span", { class: "tree-name" }, "app"), h("span", { class: "tree-when" }, "4")),
          row("present", "A", "out.txt", "#3"),
          row("now", "M", "parse.py", "#12"),
          row("deleted", "D", "old.cfg", "#9"),
          row("future", "", "report.md", "later #20"),
        ),
      );
    }
    default:
      return h("p", { class: "note" }, `(missing example: ${name})`);
  }
}

function checklistDemo() {
  const chosen = new Set(["Loop"]);
  const names = ["Loop", "Stall", "No-effect write", "False edit claim", "False test claim", "Unverified completion", "Server state", "Forbidden action"];
  const panel = h("div", { class: "labelpanel compact sample-panel" });
  const draw = () => {
    const box = (key, text, hint, kind) => {
      const on = key === "none" ? chosen.size === 0 : chosen.has(key);
      const label = h("span", { class: `chip-check ${kind}${on ? " on" : ""}` }, h("span", null, text), h("kbd", null, hint));
      label.addEventListener("click", () => {
        if (key === "none") chosen.clear();
        else if (chosen.has(key)) chosen.delete(key);
        else chosen.add(key);
        draw();
      });
      return label;
    };
    const verdict = chosen.size ? [...chosen].join(", ") : "none";
    put(
      panel,
      h("div", { class: "lp-head" }, h("span", { class: "lp-title" }, "Your label for #12"), h("span", { class: `verdict ${chosen.size ? "yes" : "no"}` }, verdict)),
      h("div", { class: "lp-checks" }, names.map((n, i) => box(n, n, String(i + 1), "pattern")), h("span", { class: "divider" }), box("none", "None of these", "0", "none"), h("span", { class: "chip-check unsure" }, h("span", null, "Unsure"), h("kbd", null, "u")), h("span", { class: "chip-check wrong" }, icon("flag"), "Mark first wrong step", h("kbd", null, "w"))),
    );
  };
  draw();
  return panel;
}
