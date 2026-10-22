// A small Markdown renderer for agent messages and the labeling guide. It builds DOM nodes
// directly, so text from a trial can never become HTML. It covers what agents write: headings,
// paragraphs, lists, block quotes, fenced code, tables, rules, and inline code, bold, italics,
// strike-through and links (shown as text with the address, not as live links).

import { h } from "./core.js";
import { highlight } from "./code.js";

const INLINE =
  /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])|(?<![\w_])_(?!\s)([^_\n]+?)_(?![\w_])|~~([\s\S]+?)~~|\[([^\]\n]+)\]\(([^)\s]+)\)/g;

export function inline(text) {
  // A fresh regex each call: bold and italics recurse, and a shared global regex would have its
  // position reset by the inner call and loop forever.
  const re = new RegExp(INLINE.source, "g");
  const out = [];
  let last = 0;
  for (let m = re.exec(text); m; m = re.exec(text)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(h("code", { class: "md-code" }, m[2].replace(/^ (.*) $/, "$1")));
    else if (m[3] !== undefined || m[4] !== undefined) out.push(h("strong", null, inline(m[3] ?? m[4])));
    else if (m[5] !== undefined || m[6] !== undefined) out.push(h("em", null, inline(m[5] ?? m[6])));
    else if (m[7] !== undefined) out.push(h("del", null, inline(m[7])));
    else out.push(h("span", { class: "md-link", title: m[9] }, inline(m[8]), h("span", { class: "md-url" }, ` (${m[9]})`)));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const FENCE = /^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
}

export function markdown(text, { compact = false } = {}) {
  const lines = (text || "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = line.match(FENCE);
    if (fence) {
      const close = fence[2];
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(close)) body.push(lines[i++]);
      i++;
      const lang = fence[3] || "";
      blocks.push(h("pre", { class: "md-pre code-block" }, h("code", null, highlight(body.join("\n"), lang))));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (heading) {
      const level = Math.min(6, heading[1].length + (compact ? 3 : 1));
      blocks.push(h(`h${level}`, { class: "md-h" }, inline(heading[2])));
      i++;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push(h("hr", { class: "md-hr" }));
      i++;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      const head = tableCells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(tableCells(lines[i++]));
      blocks.push(
        h(
          "div",
          { class: "md-table-wrap" },
          h(
            "table",
            { class: "md-table" },
            h("thead", null, h("tr", null, head.map((c) => h("th", null, inline(c))))),
            h("tbody", null, rows.map((r) => h("tr", null, r.map((c) => h("td", null, inline(c)))))),
          ),
        ),
      );
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quoted = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quoted.push(lines[i++].replace(/^\s*>\s?/, ""));
      blocks.push(h("blockquote", { class: "md-quote" }, markdown(quoted.join("\n"), { compact })));
      continue;
    }
    if (LIST_ITEM.test(line)) {
      const items = [];
      while (i < lines.length && (LIST_ITEM.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        const m = lines[i].match(LIST_ITEM);
        if (m) items.push({ indent: m[1].length, ordered: /\d/.test(m[2]), text: m[3] });
        else items[items.length - 1].text += ` ${lines[i].trim()}`;
        i++;
      }
      blocks.push(listOf(items));
      continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !FENCE.test(lines[i]) && !/^#{1,6}\s/.test(lines[i]) && !LIST_ITEM.test(lines[i]) && !/^\s*>/.test(lines[i])) {
      paragraph.push(lines[i++]);
    }
    blocks.push(h("p", { class: "md-p" }, paragraph.flatMap((l, k) => (k ? [h("br"), inline(l)] : [inline(l)]))));
  }
  return h("div", { class: `md${compact ? " md-compact" : ""}` }, blocks);
}

// Nested lists from items with indents: deeper items go into the previous item.
function listOf(items) {
  const root = { children: [], indent: -1 };
  const stack = [root];
  for (const item of items) {
    while (stack.length > 1 && item.indent <= stack[stack.length - 1].indent) stack.pop();
    const node = { ...item, children: [] };
    stack[stack.length - 1].children.push(node);
    stack.push(node);
  }
  const render = (nodes) => {
    const ordered = nodes.length && nodes[0].ordered;
    return h(ordered ? "ol" : "ul", { class: "md-list" }, nodes.map((n) => h("li", null, inline(n.text), n.children.length ? render(n.children) : null)));
  };
  return render(root.children);
}
