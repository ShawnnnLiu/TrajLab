"""Just enough of a POSIX shell parser to read what a Bash call meant to write (ADR-0023).

It splits a command into simple commands, each with its words, redirects, and the operator that
ends it, and keeps where each one sits in the text. It never runs anything and never expands:
a word with a parameter, a command substitution, a glob, or `~` is returned as written and
marked, so callers can treat it as unknown instead of guessing.

Heredoc bodies are cut out, so text inside them is never read as a command or a path. So are
the bodies of `$(...)` and backticks, which are parsed separately as nested scripts (for
`kill $(pgrep -f ...)`); strings passed to `python -c` or `bash -c` stay single words.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# Operators that end a simple command; `(` and `)` open and close a subshell.
SEPARATORS = ("&&", "||", ";;", "|&", ";", "|", "&", "\n")
_REDIRECT = re.compile(r"(\d*)(>>|>\||>&|>|&>>|&>|<<<|<<-|<<|<&|<>|<)")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_GLOB = re.compile(r"[*?\[]")


class ShellParseError(ValueError):
    """The command is not shell this parser can read (an unclosed quote, heredoc, or bracket)."""


@dataclass(frozen=True)
class Word:
    """One shell word: `text` with quotes removed, and what in it the shell would expand."""

    text: str
    raw: str
    expands: bool = False  # holds $VAR, ${...}, $(...), or backticks outside single quotes
    globs: bool = False  # holds an unquoted *, ?, or [
    tilde: bool = False  # starts with an unquoted ~


@dataclass(frozen=True)
class Redirect:
    fd: str  # as written, "" for the default
    op: str
    target: Word


@dataclass
class SimpleCommand:
    words: list[Word]
    redirects: list[Redirect]
    start: int  # offsets into the parsed text
    end: int
    ended_by: str  # the operator after it, "" at the end
    depth: int  # subshell nesting: 0 at top level
    substitutions: list["Script"] = field(default_factory=list)

    @property
    def argv(self) -> list[str]:
        return [w.text for w in self.words]

    @property
    def name(self) -> str:
        """The command name, after leading assignments and `nohup`, `sudo`, `time`, `exec`."""
        return self.args_from()[0] if self.args_from() else ""

    def args_from(self) -> list[str]:
        argv = [w.text for w in self.words]
        i = 0
        while i < len(argv) and _NAME.match(argv[i]):
            i += 1
        while i < len(argv) and argv[i] in ("nohup", "sudo", "time", "exec", "setsid", "command"):
            i += 1
        return argv[i:]


@dataclass
class Script:
    text: str
    commands: list[SimpleCommand]
    heredocs: list[str]  # bodies, in order; never parsed


def parse(text: str) -> Script:
    return _Parser(text).parse()


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.commands: list[SimpleCommand] = []
        self.heredocs: list[str] = []
        self.pending: list[tuple[str, bool]] = []  # (delimiter, strip tabs)
        self.depth = 0

    def parse(self) -> Script:
        words: list[Word] = []
        redirects: list[Redirect] = []
        subs: list[Script] = []
        start = None
        text = self.text
        while True:
            self._skip_blanks()
            if self.i >= len(text):
                self._end(words, redirects, subs, start, self.i, "")
                break
            c = text[self.i]
            if c == "#" and (self.i == 0 or text[self.i - 1] in " \t\n;&|()"):
                while self.i < len(text) and text[self.i] != "\n":
                    self.i += 1
                continue
            op = next((s for s in SEPARATORS if text.startswith(s, self.i)), None)
            if op is not None:
                end = self.i
                self.i += len(op)
                self._end(words, redirects, subs, start, end, op)
                words, redirects, subs, start = [], [], [], None
                if op == "\n":
                    self._read_heredocs()
                continue
            if c in "()":
                end = self.i
                self.i += 1
                self._end(words, redirects, subs, start, end, c)
                words, redirects, subs, start = [], [], [], None
                self.depth += 1 if c == "(" else -1
                continue
            if start is None:
                start = self.i
            match = _REDIRECT.match(text, self.i)
            if match and (match.group(1) == "" or not words or self._word_boundary()):
                self.i = match.end()
                op = match.group(2)
                self._skip_blanks()
                target, target_subs = self._word()
                subs.extend(target_subs)
                if op in ("<<", "<<-"):
                    self.pending.append((target.text, op == "<<-"))
                redirects.append(Redirect(fd=match.group(1), op=op, target=target))
                continue
            word, word_subs = self._word()
            subs.extend(word_subs)
            words.append(word)
        if self.pending:
            raise ShellParseError(f"unterminated heredoc {self.pending[0][0]!r}")
        if self.depth != 0:
            raise ShellParseError("unbalanced parentheses")
        return Script(text=text, commands=self.commands, heredocs=self.heredocs)

    def _word_boundary(self) -> bool:
        return self.i == 0 or self.text[self.i - 1] in " \t\n;&|()"

    def _end(self, words, redirects, subs, start, end, op) -> None:  # type: ignore[no-untyped-def]
        if words or redirects:
            # `{` and `}` group commands; they are reserved words, not commands.
            words = [w for w in words if w.raw not in ("{", "}")]
            if words or redirects:
                self.commands.append(
                    SimpleCommand(words, redirects, start or 0, end, op, self.depth, subs)
                )

    def _skip_blanks(self) -> None:
        text = self.text
        while self.i < len(text):
            if text[self.i] in " \t":
                self.i += 1
            elif text.startswith("\\\n", self.i):
                self.i += 2
            else:
                break

    def _read_heredocs(self) -> None:
        """Consume the bodies of heredocs opened on the line just ended."""
        text = self.text
        for delimiter, strip_tabs in self.pending:
            body_start = self.i
            while True:
                if self.i >= len(text):
                    raise ShellParseError(f"unterminated heredoc {delimiter!r}")
                line_end = text.find("\n", self.i)
                line_end = len(text) if line_end < 0 else line_end
                line = text[self.i : line_end]
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    self.heredocs.append(text[body_start : self.i])
                    self.i = min(line_end + 1, len(text))
                    break
                self.i = line_end + 1
        self.pending = []

    def _word(self) -> tuple[Word, list[Script]]:
        text = self.text
        start = self.i
        out: list[str] = []
        subs: list[Script] = []
        expands = globs = False
        tilde = text.startswith("~", self.i)
        while self.i < len(text):
            c = text[self.i]
            if c in " \t\n;&|()<>":
                if c in "<>" and not out:
                    break
                break
            if c == "\\":
                if self.i + 1 < len(text) and text[self.i + 1] != "\n":
                    out.append(text[self.i + 1])
                self.i += 2
            elif c == "'":
                end = text.find("'", self.i + 1)
                if end < 0:
                    raise ShellParseError("unclosed single quote")
                out.append(text[self.i + 1 : end])
                self.i = end + 1
            elif c == '"':
                self.i += 1
                while True:
                    if self.i >= len(text):
                        raise ShellParseError("unclosed double quote")
                    d = text[self.i]
                    if d == '"':
                        self.i += 1
                        break
                    if d == "\\" and self.i + 1 < len(text) and text[self.i + 1] in '"\\$`\n':
                        out.append(text[self.i + 1])
                        self.i += 2
                    elif d == "$" or d == "`":
                        expands = True
                        piece, sub = self._expansion()
                        out.append(piece)
                        if sub is not None:
                            subs.append(sub)
                    else:
                        out.append(d)
                        self.i += 1
            elif c == "$" or c == "`":
                expands = True
                piece, sub = self._expansion()
                out.append(piece)
                if sub is not None:
                    subs.append(sub)
            else:
                if c in "*?[":
                    globs = True
                out.append(c)
                self.i += 1
        raw = text[start : self.i]
        return Word("".join(out), raw, expands, globs, tilde), subs

    def _expansion(self) -> tuple[str, "Script | None"]:
        """`$(...)`, `` `...` ``, `$((...))`, `${...}` or `$NAME`, as written; a command
        substitution's body is parsed as its own script."""
        text = self.text
        start = self.i
        if text.startswith("$((", self.i):
            self.i = self._balanced(self.i + 3, "((", "))")
            return text[start : self.i], None
        if text.startswith("$(", self.i):
            body_start = self.i + 2
            self.i = self._balanced(body_start, "(", ")")
            return text[start : self.i], _sub(text[body_start : self.i - 1])
        if text.startswith("`", self.i):
            end = self.i + 1
            while end < len(text) and text[end] != "`":
                end += 2 if text[end] == "\\" else 1
            if end >= len(text):
                raise ShellParseError("unclosed backtick")
            self.i = end + 1
            return text[start : self.i], _sub(text[start + 1 : end])
        if text.startswith("${", self.i):
            end = text.find("}", self.i)
            if end < 0:
                raise ShellParseError("unclosed ${")
            self.i = end + 1
            return text[start : self.i], None
        match = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])?").match(text, self.i)
        self.i = match.end() if match else self.i + 1
        return text[start : self.i], None

    def _balanced(self, i: int, open_: str, close: str) -> int:
        """The index just past the `close` that matches an already-opened `open_`, skipping
        quoted text."""
        text = self.text
        depth = 1
        while i < len(text):
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                end = text.find("'", i + 1)
                i = (end if end >= 0 else len(text)) + 1
                continue
            if c == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
                i += 1
                continue
            if text.startswith(close, i):
                depth -= 1
                i += len(close)
                if depth == 0:
                    return i
                continue
            if text.startswith(open_, i):
                depth += 1
                i += len(open_)
                continue
            i += 1
        raise ShellParseError(f"unclosed {open_}")


def _sub(body: str) -> "Script | None":
    try:
        return parse(body)
    except ShellParseError:
        return None


def resolve(path: str, cwd: str | None) -> str | None:
    """An absolute, normalized container path, or None if `path` is relative and `cwd` unknown."""
    if not path.startswith("/"):
        if cwd is None:
            return None
        path = f"{cwd.rstrip('/')}/{path}"
    normalized = os.path.normpath(path)
    return "/" + normalized.lstrip("/") if normalized != "/" else "/"


def basename(path: str) -> str:
    return PurePosixPath(path.rstrip("/")).name
