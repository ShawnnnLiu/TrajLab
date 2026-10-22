// Syntax colors for commands, files, and diffs: a small tokenizer per language. It returns
// [class, text] pairs, painted as text nodes inside spans, so code is never parsed as HTML.

import { h } from "./core.js";

const LIMIT = 300_000; // characters; longer texts are shown plain

const RULES = {
  python: [
    ["comment", /#[^\n]*/y],
    ["string", /[rbuRBUfF]{0,2}(?:"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')/y],
    ["keyword", /(?:def|class|return|if|elif|else|for|while|in|not|and|or|is|import|from|as|with|try|except|finally|raise|pass|break|continue|lambda|yield|global|nonlocal|assert|del|async|await)\b/y],
    ["constant", /(?:True|False|None|self|cls)\b/y],
    ["number", /(?:0[xob][\da-f_]+|\d[\d_]*(?:\.\d*)?(?:e[+-]?\d+)?j?)\b/iy],
    ["decorator", /@[\w.]+/y],
    ["func", /[A-Za-z_]\w*(?=\s*\()/y],
  ],
  bash: [
    ["comment", /(?<![^\s;|&(])#[^\n]*/y],
    ["string", /"(?:\\[\s\S]|[^"\\])*"|'[^']*'/y],
    ["variable", /\$(?:\{[^}\n]*\}|\([^)\n]*\)|[A-Za-z_]\w*|[0-9@#?*!$-])/y],
    ["keyword", /(?:if|then|else|elif|fi|for|while|until|do|done|case|esac|in|function|return|exit|export|local|source|set|unset)\b(?![-.\w])/y],
    ["builtin", /(?:cd|echo|printf|cat|grep|sed|awk|ls|cp|mv|rm|mkdir|touch|chmod|chown|python3?|pip3?|uv|git|make|npm|npx|node|pytest|curl|wget|tee|head|tail|find|xargs|sort|uniq|wc|diff|tar|sudo|apt-get|apt|kill|pkill|ps|nohup|timeout|sleep|which|env|bash|sh|docker)\b(?![-.\w])/y],
    ["flag", /(?<=\s)--?[A-Za-z][\w-]*/y],
    ["operator", /2>&1|&&|\|\||;;|[|;&]|[<>]{1,2}/y],
    ["number", /\d+\b/y],
  ],
  js: [
    ["comment", /\/\/[^\n]*|\/\*[\s\S]*?\*\//y],
    ["string", /`(?:\\[\s\S]|[^`\\])*`|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'/y],
    ["keyword", /(?:const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|class|extends|import|from|export|default|async|await|try|catch|finally|throw|typeof|instanceof|in|of|this|super|yield|delete|void|interface|type|enum|implements|public|private|protected|readonly)\b/y],
    ["constant", /(?:true|false|null|undefined|NaN|Infinity)\b/y],
    ["number", /(?:0[xob][\da-f_]+|\d[\d_]*(?:\.\d*)?(?:e[+-]?\d+)?n?)\b/iy],
    ["func", /[A-Za-z_$][\w$]*(?=\s*\()/y],
  ],
  json: [
    ["key", /"(?:\\.|[^"\\\n])*"(?=\s*:)/y],
    ["string", /"(?:\\.|[^"\\\n])*"/y],
    ["constant", /(?:true|false|null)\b/y],
    ["number", /-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/iy],
  ],
  yaml: [
    ["comment", /(?<!\S)#[^\n]*/y],
    ["key", /[\w./-]+(?=\s*:(?:\s|$))/y],
    ["string", /"(?:\\.|[^"\\\n])*"|'[^'\n]*'/y],
    ["constant", /(?:true|false|null|yes|no)\b/iy],
    ["number", /-?\d+(?:\.\d+)?\b/y],
  ],
  toml: [
    ["comment", /(?<!\S)[#;][^\n]*/y],
    ["section", /\[[^\]\n]*\]/y],
    ["key", /[\w.-]+(?=\s*=)/y],
    ["string", /"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|'[^'\n]*'/y],
    ["constant", /(?:true|false)\b/y],
    ["number", /-?\d[\d_]*(?:\.\d+)?\b/y],
  ],
  c: [
    ["comment", /\/\/[^\n]*|\/\*[\s\S]*?\*\//y],
    ["string", /"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])+'/y],
    ["preproc", /#\s*\w+/y],
    ["keyword", /(?:if|else|for|while|do|switch|case|break|continue|return|goto|struct|union|enum|typedef|static|const|extern|void|int|char|float|double|long|short|unsigned|signed|bool|auto|class|public|private|protected|virtual|template|typename|namespace|using|new|delete|try|catch|throw|import|package|func|fn|let|mut|impl|trait|pub|use|mod|match|var|val|interface|extends|implements|final|this|self|nil|null|true|false)\b/y],
    ["number", /(?:0[xob][\da-f_]+|\d[\d_]*(?:\.\d*)?(?:e[+-]?\d+)?[fFuUlL]*)\b/iy],
    ["func", /[A-Za-z_]\w*(?=\s*\()/y],
  ],
  html: [
    ["comment", /<!--[\s\S]*?-->/y],
    ["tag", /<\/?[\w:.-]+|\/?>/y],
    ["attr", /[\w:.-]+(?==)/y],
    ["string", /"[^"]*"|'[^']*'/y],
  ],
  sql: [
    ["comment", /--[^\n]*|\/\*[\s\S]*?\*\//y],
    ["string", /'(?:''|[^'])*'/y],
    ["keyword", /(?:select|from|where|and|or|not|insert|into|values|update|set|delete|create|table|drop|alter|index|join|left|right|inner|outer|on|group|by|order|having|limit|as|distinct|union|all|null|is|in|like|between|case|when|then|else|end|primary|key|foreign|references|default|exists)\b/iy],
    ["number", /\d+(?:\.\d+)?\b/y],
  ],
  css: [
    ["comment", /\/\*[\s\S]*?\*\//y],
    ["string", /"[^"\n]*"|'[^'\n]*'/y],
    ["key", /[\w-]+(?=\s*:[^:])/y],
    ["number", /-?\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|s|ms)?\b/y],
  ],
  vba: [
    ["comment", /(?:'|Rem\b)[^\n]*/iy],
    ["string", /"(?:""|[^"\n])*"/y],
    ["keyword", /(?:Sub|End|Function|Dim|As|If|Then|Else|ElseIf|For|Each|Next|To|Step|Do|Loop|While|Wend|Until|Select|Case|With|Private|Public|Static|Const|Set|Let|Call|Exit|Return|New|Nothing|True|False|And|Or|Not|Is|Me|ByVal|ByRef|Optional|Property|Get|Option|Explicit|On|Error|Resume|GoTo|Type|Enum|Integer|Long|String|Double|Boolean|Variant|Object|Single|Byte|Date|Currency)\b/iy],
    ["number", /\d+(?:\.\d+)?\b/y],
  ],
};

const ALIASES = {
  py: "python", python3: "python", sh: "bash", shell: "bash", console: "bash", zsh: "bash", bash: "bash",
  javascript: "js", ts: "js", typescript: "js", jsx: "js", tsx: "js", mjs: "js",
  yml: "yaml", ini: "toml", cfg: "toml", conf: "toml",
  cpp: "c", "c++": "c", cc: "c", h: "c", hpp: "c", java: "c", go: "c", rust: "c", rs: "c", kotlin: "c", kt: "c", swift: "c", scala: "c", cs: "c", csharp: "c",
  xml: "html", svg: "html", vue: "html", htm: "html",
  vb: "vba", bas: "vba", cls: "vba", frm: "vba", vbs: "vba",
  scss: "css", jsonl: "json",
};

const EXTENSIONS = {
  py: "python", pyi: "python", sh: "bash", bash: "bash", zsh: "bash", js: "js", mjs: "js", cjs: "js", ts: "js", tsx: "js", jsx: "js",
  json: "json", jsonl: "json", yaml: "yaml", yml: "yaml", toml: "toml", ini: "toml", cfg: "toml", conf: "toml",
  c: "c", h: "c", cc: "c", cpp: "c", cxx: "c", hpp: "c", java: "c", go: "c", rs: "c", kt: "c", swift: "c", scala: "c", cs: "c",
  html: "html", htm: "html", xml: "html", svg: "html", vue: "html", sql: "sql", css: "css", scss: "css",
  bas: "vba", cls: "vba", frm: "vba", vba: "vba", vbs: "vba",
};

export function normalize(lang) {
  const key = (lang || "").toLowerCase();
  if (RULES[key]) return key;
  return ALIASES[key] || null;
}

export function langOf(path) {
  const name = (path || "").split("/").pop().toLowerCase();
  if (name === "makefile" || name === "dockerfile") return "bash";
  const ext = name.includes(".") ? name.split(".").pop() : "";
  return EXTENSIONS[ext] || null;
}

const PLAIN = /[A-Za-z0-9_]+|\s+|[\s\S]/y;

export function tokens(text, lang) {
  const rules = RULES[normalize(lang)];
  if (!rules || !text || text.length > LIMIT) return [[null, text || ""]];
  const out = [];
  let plain = "";
  let i = 0;
  scan: while (i < text.length) {
    for (const [cls, re] of rules) {
      re.lastIndex = i;
      const m = re.exec(text);
      if (m && m[0].length) {
        if (plain) out.push([null, plain]);
        plain = "";
        out.push([cls, m[0]]);
        i += m[0].length;
        continue scan;
      }
    }
    PLAIN.lastIndex = i;
    const word = PLAIN.exec(text)[0];
    plain += word;
    i += word.length;
  }
  if (plain) out.push([null, plain]);
  return out;
}

export function highlight(text, lang) {
  return tokens(text, lang).map(([cls, t]) => (cls ? h("span", { class: `tk-${cls}` }, t) : t));
}

// Like highlight, with character ranges [start, end) also marked (the changed words of a line).
export function paint(text, lang, marks = []) {
  if (!marks.length) return highlight(text, lang);
  const out = [];
  let offset = 0;
  for (const [cls, t] of tokens(text, lang)) {
    let start = 0;
    while (start < t.length) {
      const at = offset + start;
      const inside = marks.find(([a, b]) => at >= a && at < b);
      const next = inside ? inside[1] : Math.min(...marks.map(([a]) => a).filter((a) => a > at), offset + t.length);
      const end = Math.min(t.length, next - offset);
      const piece = t.slice(start, end);
      const classes = [cls ? `tk-${cls}` : "", inside ? "wd" : ""].filter(Boolean).join(" ");
      out.push(classes ? h("span", { class: classes }, piece) : piece);
      start = end;
    }
    offset += t.length;
  }
  return out;
}
