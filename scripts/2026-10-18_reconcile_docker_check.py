"""Check DockerImageProvider and reconcile() against real Docker, with an independent oracle.

Manual integration check for the Exp 2 store (ADR-0014). Needs Docker and the local image
alpine:3.20. It starts an Alpine container, runs a scripted sequence of shell steps in it, and
after each step commits it the way the watcher does (`DockerCommitBackend.snapshot`, repository
`trajlab-checkpoint`, tag `reconcile-check__<run>.<seq:04d>`, the watcher's labels). Then:

- store side: `DockerImageProvider(...).chain(records)` and `reconcile(...)` give delta rows;
- oracle: alpine:3.20 and every checkpoint image are `docker create`d and `docker export`ed,
  each full root filesystem is parsed here with `tarfile` alone, and the expected delta of
  seq k is the difference between export k-1 and export k (export 0 is alpine:3.20). The
  oracle shares no code with reconcile; it reuses only `trajlab.store.exclusions.excluded`,
  the predicate that defines which paths a delta may hold.

Rows are compared per seq on (path, op, kind_before, kind_after, sha256_before, sha256_after,
mode_before, mode_after, owner_before, owner_after). It also checks is_text against the text
rule, file sizes, blob files, and that each hunk turns the earlier text into the later one,
and that reconciling seq k from only checkpoints k-1 and k gives the same rows as the whole
chain. The paths Docker manages in every container (/proc, /sys, /dev, /etc/hostname,
/etc/hosts, /etc/resolv.conf, /.dockerenv) are left out of the oracle.

Exit status: 0 all rows match, 1 any mismatch, 2 reconcile() is not implemented yet (the
provider and the oracle still run). Every container and image the script created is removed
in a finally block; it touches no other image.

    uv run python scripts/2026-10-18_reconcile_docker_check.py [--keep-work]
"""

import argparse
import hashlib
import logging
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trajlab.checkpoint.backends.docker_commit import REPOSITORY, DockerCommitBackend, image_tag
from trajlab.contracts import CheckpointRecord, DeltaRow
from trajlab.store.docker_images import DockerImageProvider
from trajlab.store.exclusions import excluded
from trajlab.store.reconcile import ImageChain, LayerSource, reconcile

logger = logging.getLogger("reconcile_docker_check")

BASE_IMAGE = "alpine:3.20"
TRIAL_PREFIX = "reconcile-check__"
# The text rule as the reconcile module docstring states it: at most 1 MiB, no NUL, UTF-8.
MAX_TEXT_BYTES = 1 << 20
IGNORED_TREES = ("/proc", "/sys", "/dev")
IGNORED_FILES = frozenset({"/etc/hostname", "/etc/hosts", "/etc/resolv.conf", "/.dockerenv"})
MAX_LISTED = 40


@dataclass(frozen=True)
class Step:
    name: str
    script: str
    # Hand-written expected delta (path -> op), checked against the oracle itself; None = large.
    expect: Mapping[str, str] | None = None


BULK = (
    "i=0; while [ $i -lt 40 ]; do d=/opt/bulk/d$i/sub; mkdir -p $d; j=0; "
    'while [ $j -lt 50 ]; do echo "$i $j" > $d/f$j; j=$((j+1)); done; i=$((i+1)); done; '
    "rm -rf /usr/bin"
)

STEPS: tuple[Step, ...] = (
    Step(
        "add-text",
        "printf 'hello\\nworld\\n' > /root/notes.txt && printf 'x\\n' > '/root/with space é.txt'"
        " && printf 'export A=1\\n' > /root/.bashrc && : > /root/empty.txt"
        " && printf 'no newline' > /root/nonl.txt && printf 'line one\\r\\nline two\\r\\n'"
        " > /root/crlf.txt",
        {
            "/root/notes.txt": "add",
            "/root/with space é.txt": "add",
            "/root/.bashrc": "add",
            "/root/empty.txt": "add",
            "/root/nonl.txt": "add",
            "/root/crlf.txt": "add",
        },
    ),
    Step("append-base", "echo 'export EDITOR=vi' >> /etc/profile", {"/etc/profile": "modify"}),
    Step("rm-base-file", "rm /etc/issue", {"/etc/issue": "delete"}),
    Step(
        "rm-rf-base-dirs",
        "rm -rf /etc/modprobe.d /etc/ssl",
        dict.fromkeys(
            [
                "/etc/modprobe.d",
                *(f"/etc/modprobe.d/{n}.conf" for n in ("aliases", "blacklist", "i386", "kms")),
                "/etc/ssl",
                "/etc/ssl/cert.pem",
                "/etc/ssl/certs",
                "/etc/ssl/certs/ca-certificates.crt",
                "/etc/ssl/ct_log_list.cnf",
                "/etc/ssl/ct_log_list.cnf.dist",
                "/etc/ssl/openssl.cnf",
                "/etc/ssl/openssl.cnf.dist",
                "/etc/ssl/private",
            ],
            "delete",
        ),
    ),
    Step(
        "replace-base-dir",
        "rm -rf /usr/share/apk && mkdir /usr/share/apk && printf 'new\\n' > /usr/share/apk/x",
    ),
    Step(
        "chmod-only",
        "chmod 0600 /etc/fstab && chmod 4755 /root/notes.txt && chmod 0700 /etc/periodic/daily",
        {"/etc/fstab": "modify", "/root/notes.txt": "modify", "/etc/periodic/daily": "modify"},
    ),
    Step(
        "chown-only",
        "chown 1000:1000 /root/notes.txt && chown 65534:65534 /etc/sysctl.conf"
        " && chown -h 123:456 /etc/ssl1.1/certs",
        {"/root/notes.txt": "modify", "/etc/sysctl.conf": "modify", "/etc/ssl1.1/certs": "modify"},
    ),
    Step(
        "same-bytes-and-touch",
        "cat /etc/os-release > /root/osr && cat /root/osr > /etc/os-release && rm /root/osr"
        " && touch /etc/inittab /root/empty.txt",
        {},
    ),
    Step("symlink-create", "ln -s /root/notes.txt /root/link", {"/root/link": "add"}),
    Step("symlink-retarget", "ln -sfn /etc/profile /root/link", {"/root/link": "modify"}),
    Step(
        "hard-links",
        "ln /root/notes.txt /root/notes.hard && ln /etc/passwd /root/passwd.hard",
        {"/root/notes.hard": "add", "/root/passwd.hard": "add"},
    ),
    Step(
        "binary",
        "head -c 4096 /dev/urandom > /root/bin.dat && printf 'text\\0with nul\\n' > /root/nul.txt",
        {"/root/bin.dat": "add", "/root/nul.txt": "add"},
    ),
    Step(
        "big-text",
        "yes 'the quick brown fox' | head -c 1100000 > /root/big.txt && chmod 0600 /root/bin.dat",
        {"/root/big.txt": "add", "/root/bin.dat": "modify"},  # a binary chmod: no blob
    ),
    Step(
        "mkdir-and-temp-file",
        "mkdir /root/emptydir && echo tmp > /root/ephemeral && mkfifo /root/fifo",
        {"/root/emptydir": "add", "/root/ephemeral": "add", "/root/fifo": "add"},
    ),
    Step(
        "rmdir-and-rm-temp-file",
        "rmdir /root/emptydir && rm /root/ephemeral",
        {"/root/emptydir": "delete", "/root/ephemeral": "delete"},
    ),
    Step(
        "excluded-only",
        "mkdir -p /root/.cache/claude /tmp/claude-0 /logs/agent"
        " && echo '{}' > /root/.cache/claude/state.json && echo x > /tmp/claude-0/out"
        " && echo log > /logs/agent/x.log",
        {},
    ),
    Step(
        "cache-sibling",
        "echo other > /root/.cache/other.txt",
        {"/root/.cache/other.txt": "add"},
    ),
    Step(
        "text-edits",
        "printf 'hello\\nthere\\nworld\\n' > /root/notes.txt"
        " && printf 'line one\\r\\nline 2\\r\\n' > /root/crlf.txt"
        " && printf 'no newline\\nmore' > /root/nonl.txt && echo filled > /root/empty.txt"
        " && printf 'binary\\0now\\n' > '/root/with space é.txt'",
        {
            "/root/notes.txt": "modify",
            "/root/notes.hard": "modify",
            "/root/crlf.txt": "modify",
            "/root/nonl.txt": "modify",
            "/root/empty.txt": "modify",
            "/root/with space é.txt": "modify",
        },
    ),
    Step(
        "kind-changes",
        "rm /etc/shells && mkdir /etc/shells && echo x > /etc/shells/inner"
        " && rm -rf /etc/periodic && echo file > /etc/periodic"
        " && rm /root/link && mkdir /root/link && rm -rf /media && ln -s /tmp /media",
        {
            "/etc/shells": "modify",
            "/etc/shells/inner": "add",
            "/etc/periodic": "modify",
            **{
                f"/etc/periodic/{n}": "delete"
                for n in ("15min", "daily", "hourly", "monthly", "weekly")
            },
            "/root/link": "modify",
            "/media": "modify",
            **{f"/media/{n}": "delete" for n in ("cdrom", "floppy", "usb")},
        },
    ),
    Step(
        "rm-cache-dir",
        "rm -rf /root/.cache",
        {"/root/.cache": "delete", "/root/.cache/other.txt": "delete"},
    ),
    Step("bulk", BULK),
    Step(
        "after-bulk",
        "echo changed > /opt/bulk/d1/sub/f1 && rm -rf /opt/bulk/d2 && mkdir /usr/bin"
        " && printf '#!/bin/sh\\n' > /usr/bin/mytool && chmod 0755 /usr/bin/mytool",
    ),
)


# -- docker ------------------------------------------------------------------------------


def docker(*args: str, timeout: float = 600) -> str:
    completed = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout.strip()


@dataclass
class Created:
    """Everything this run made in Docker, so the finally block can remove exactly that."""

    trial_name: str
    containers: list[str]
    tags: list[str]

    def cleanup(self) -> None:
        for container in self.containers:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
        labelled = subprocess.run(
            ["docker", "images", "-q", "--filter", f"label=trajlab.trial_name={self.trial_name}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        for ref in [*self.tags, *dict.fromkeys(labelled)]:
            subprocess.run(["docker", "rmi", "-f", ref], capture_output=True, check=False)
        left = subprocess.run(
            ["docker", "images", "-q", "--filter", f"label=trajlab.trial_name={self.trial_name}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        if left:
            logger.error("images left behind: %s", left)


# -- the oracle: full exports, parsed here -------------------------------------------------


@dataclass(frozen=True)
class Entry:
    kind: str  # file, dir, symlink, other
    mode: str  # permission bits, 4 octal digits
    owner: str  # uid:gid
    sha256: str | None  # of a file's bytes or a symlink's target; None for dir and other
    size: int | None


def _member_path(name: str) -> str:
    if name.startswith("./"):
        name = name[2:]
    return "/" + name.strip("/")


def _ignored(path: str) -> bool:
    return path in IGNORED_FILES or any(
        path == tree or path.startswith(tree + "/") for tree in IGNORED_TREES
    )


def parse_export(archive: Path, contents: dict[str, bytes]) -> dict[str, Entry]:
    """path -> Entry for a `docker export` tar; contents gains sha256 -> bytes."""
    entries: dict[str, Entry] = {}
    with tarfile.open(archive, mode="r:") as tar:
        for member in tar:
            path = _member_path(member.name)
            if path == "/" or _ignored(path):
                continue
            mode = f"{member.mode & 0o7777:04o}"
            owner = f"{member.uid}:{member.gid}"
            if member.islnk():
                target = entries[_member_path(member.linkname)]
                entries[path] = Entry("file", mode, owner, target.sha256, target.size)
            elif member.isreg():
                stream = tar.extractfile(member)
                assert stream is not None
                data = stream.read()
                digest = hashlib.sha256(data).hexdigest()
                contents.setdefault(digest, data)
                entries[path] = Entry("file", mode, owner, digest, len(data))
            elif member.issym():
                data = member.linkname.encode("utf-8", "surrogateescape")
                digest = hashlib.sha256(data).hexdigest()
                contents.setdefault(digest, data)
                entries[path] = Entry("symlink", mode, owner, digest, len(data))
            elif member.isdir():
                entries[path] = Entry("dir", mode, owner, None, None)
            else:
                entries[path] = Entry("other", mode, owner, None, None)
    return entries


def export_image(
    image: str, work: Path, created: Created, contents: dict[str, bytes]
) -> dict[str, Entry]:
    container = docker("create", image)
    created.containers.append(container)
    archive = work / f"export-{container[:12]}.tar"
    try:
        docker("export", "-o", str(archive), container)
        return parse_export(archive, contents)
    finally:
        archive.unlink(missing_ok=True)
        docker("rm", container)
        created.containers.remove(container)


Expected = dict[str, tuple[str, Entry | None, Entry | None]]


def expected_delta(before: Mapping[str, Entry], after: Mapping[str, Entry]) -> Expected:
    """path -> (op, before, after) for every path whose entry differs, after exclusions."""
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    kept = {p for p in changed if not excluded(p)}
    dropped = changed - kept

    def only_excluded_below(path: str) -> bool:
        prefix = path + "/"
        return any(p.startswith(prefix) for p in dropped) and not any(
            p.startswith(prefix) for p in kept
        )

    result: Expected = {}
    for path in kept:
        old, new = before.get(path), after.get(path)
        op = "add" if old is None else "delete" if new is None else "modify"
        entry = new if old is None else old if new is None else None
        if entry is not None and entry.kind == "dir" and only_excluded_below(path):
            continue
        result[path] = (op, old, new)
    return result


# -- comparison ----------------------------------------------------------------------------

CORE_FIELDS = (
    "op",
    "kind_before",
    "kind_after",
    "sha256_before",
    "sha256_after",
    "mode_before",
    "mode_after",
    "owner_before",
    "owner_after",
)


def core_values(op: str, old: Entry | None, new: Entry | None) -> dict[str, object]:
    return {
        "op": op,
        "kind_before": old.kind if old else None,
        "kind_after": new.kind if new else None,
        "sha256_before": old.sha256 if old else None,
        "sha256_after": new.sha256 if new else None,
        "mode_before": old.mode if old else None,
        "mode_after": new.mode if new else None,
        "owner_before": old.owner if old else None,
        "owner_after": new.owner if new else None,
    }


def version_is_text(entry: Entry | None, contents: Mapping[str, bytes]) -> bool | None:
    if entry is None or entry.kind == "symlink":
        return True
    if entry.kind != "file":
        return None
    assert entry.sha256 is not None
    data = contents[entry.sha256]
    if len(data) > MAX_TEXT_BYTES or b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def split_lines(text: str) -> list[str]:
    """Lines split on "\\n" only, each keeping its "\\n"; a last line may lack one."""
    lines = [line + "\n" for line in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    return lines if lines[-1] else lines[:-1]


HUNK_RANGE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_diff(before: str, diff: str) -> str:
    """Apply a unified diff's hunks (headers already checked) to `before`; raise on conflict."""
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    old = split_lines(before)
    result: list[str] = []
    position = 0
    index = 2
    while index < len(lines):
        match = HUNK_RANGE.match(lines[index])
        if match is None:
            raise ValueError(f"expected a hunk range, got {lines[index]!r}")
        start, count = int(match[1]), int(match[2]) if match[2] is not None else 1
        first = start - 1 if count > 0 else start
        if first < position:
            raise ValueError("hunks overlap or go backwards")
        result.extend(old[position:first])
        position = first
        index += 1
        body: list[tuple[str, str]] = []
        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line.startswith("\\"):
                tag, text = body[-1]
                body[-1] = (tag, text.removesuffix("\n"))
            elif line == "":
                body.append((" ", "\n"))
            else:
                body.append((line[0], line[1:] + "\n"))
            index += 1
        for tag, text in body:
            if tag in " -":
                if position >= len(old) or old[position] != text:
                    found = old[position] if position < len(old) else None
                    raise ValueError(f"line {position + 1}: expected {text!r}, found {found!r}")
                if tag == " ":
                    result.append(text)
                position += 1
            elif tag == "+":
                result.append(text)
            else:
                raise ValueError(f"bad diff line tag {tag!r}")
    result.extend(old[position:])
    return "".join(result)


def secondary_problems(
    row: DeltaRow,
    old: Entry | None,
    new: Entry | None,
    contents: Mapping[str, bytes],
    blobs_dir: Path,
) -> list[str]:
    """is_text, sizes, blob_ref, and hunk of a row whose core fields matched."""
    problems: list[str] = []
    for side, entry, got in (("before", old, row.bytes_before), ("after", new, row.bytes_after)):
        if entry is None and got is not None:
            problems.append(f"bytes_{side}: expected None, got {got}")
        elif entry is not None and entry.kind == "file" and got != entry.size:
            problems.append(f"bytes_{side}: expected {entry.size}, got {got}")
    # The text rule: a version without content (absent, a directory, another kind) counts as
    # text; a row with no file or symlink version has is_text null.
    with_content = [e for e in (old, new) if e is not None and e.kind in ("file", "symlink")]
    expected_text: bool | None = (
        all(version_is_text(e, contents) for e in with_content) if with_content else None
    )
    new_text = version_is_text(new, contents)
    if row.is_text != expected_text:
        problems.append(f"is_text: expected {expected_text}, got {row.is_text}")
        return problems
    content_changed = (old.sha256 if old else None) != (new.sha256 if new else None)
    if expected_text and content_changed:
        problems += hunk_problems(row, old, new, contents)
    elif row.hunk is not None:
        problems.append(f"hunk: expected none, got {row.hunk[:80]!r}")
    new_content_changed = old is None or old.kind != "file" or old.sha256 != new_sha256(new)
    if new is not None and new.kind == "file" and new_text is False and new_content_changed:
        assert new.sha256 is not None
        blob = blobs_dir / new.sha256
        if row.blob_ref != f"blobs/{new.sha256}":
            problems.append(f"blob_ref: expected blobs/{new.sha256}, got {row.blob_ref}")
        elif not blob.is_file() or hashlib.sha256(blob.read_bytes()).hexdigest() != new.sha256:
            problems.append(f"blob {blob} is missing or does not hold the later version")
    elif row.blob_ref is not None:
        problems.append(f"blob_ref: expected none (text, or content unchanged), got {row.blob_ref}")
    return problems


def new_sha256(entry: Entry | None) -> str | None:
    return entry.sha256 if entry is not None else None


def hunk_problems(
    row: DeltaRow, old: Entry | None, new: Entry | None, contents: Mapping[str, bytes]
) -> list[str]:
    before = contents[old.sha256].decode() if old and old.sha256 else ""
    after = contents[new.sha256].decode() if new and new.sha256 else ""
    if row.hunk is None:
        return ["hunk: expected a diff, got none"]
    if row.hunk == "":
        if split_lines(before) or split_lines(after):
            return ["hunk: empty, but a version has lines"]
        return []
    lines = row.hunk.split("\n")
    # "/dev/null" for a side without content: absent, or a directory or other kind.
    has_content = {"file", "symlink"}
    head = f"--- a{row.path}" if old and old.kind in has_content else "--- /dev/null"
    tail = f"+++ b{row.path}" if new and new.kind in has_content else "+++ /dev/null"
    if lines[:2] != [head, tail]:
        return [f"hunk headers: expected {[head, tail]}, got {lines[:2]}"]
    try:
        rebuilt = apply_diff(before, row.hunk)
    except ValueError as error:
        return [f"hunk does not apply: {error}"]
    if rebuilt != after:
        return [f"hunk applied gives {rebuilt[:60]!r}, not {after[:60]!r}"]
    return []


def compare(
    expected: Expected,
    got: Sequence[DeltaRow],
    contents: Mapping[str, bytes],
    blobs_dir: Path,
) -> list[str]:
    problems: list[str] = []
    by_path = {row.path: row for row in got}
    if len(by_path) != len(got):
        problems.append("rows repeat a path")
    for path in sorted(expected.keys() - by_path.keys()):
        op, old, new = expected[path]
        problems.append(f"missing {op} {path} ({old} -> {new})")
    for path in sorted(by_path.keys() - expected.keys()):
        row = by_path[path]
        note = " (a path the oracle ignores)" if _ignored(path) else ""
        problems.append(f"unexpected {row.op} {path}{note}: {row.kind_before} -> {row.kind_after}")
    for path in sorted(expected.keys() & by_path.keys()):
        op, old, new = expected[path]
        row = by_path[path]
        want = core_values(op, old, new)
        wrong = [
            f"{name} expected {want[name]!r} got {getattr(row, name)!r}"
            for name in CORE_FIELDS
            if getattr(row, name) != want[name]
        ]
        if wrong:
            problems.append(f"{path}: " + "; ".join(wrong))
            continue
        problems += [f"{path}: {p}" for p in secondary_problems(row, old, new, contents, blobs_dir)]
    return problems


# -- the run -------------------------------------------------------------------------------


def take_checkpoints(
    created: Created, container: str, backend: DockerCommitBackend
) -> list[CheckpointRecord]:
    records = []
    for seq, step in enumerate(STEPS, start=1):
        requested_at = datetime.now(UTC)
        docker("exec", container, "sh", "-c", step.script)
        tool_call_id = f"toolu_reconcile_check_{seq:04d}"
        tag = image_tag(created.trial_name, seq)
        created.tags.append(f"{REPOSITORY}:{tag}")
        snapshot = backend.snapshot(
            container,
            tag=tag,
            labels={
                "trajlab.trial_name": created.trial_name,
                "trajlab.tool_call_id": tool_call_id,
                "trajlab.seq": str(seq),
            },
        )
        records.append(
            CheckpointRecord(
                checkpoint_id=snapshot.checkpoint_id,
                trial_name=created.trial_name,
                tool_call_id=tool_call_id,
                seq=seq,
                covered_tool_call_ids=(tool_call_id,),
                tool_name="Bash",
                capture_ms=snapshot.capture_ms,
                bytes=snapshot.bytes,
                requested_at=requested_at,
                captured_at=snapshot.captured_at,
            )
        )
        logger.info("seq %2d %-24s committed in %5d ms", seq, step.name, snapshot.capture_ms)
    return records


def oracle(created: Created, work: Path, contents: dict[str, bytes]) -> dict[int, Expected]:
    start = time.monotonic()
    views = [export_image(BASE_IMAGE, work, created, contents)]
    views += [export_image(tag, work, created, contents) for tag in created.tags]
    logger.info("oracle: %d exports parsed in %.1f s", len(views), time.monotonic() - start)
    return {seq: expected_delta(views[seq - 1], views[seq]) for seq in range(1, len(views))}


def oracle_sanity(expected: Mapping[int, Expected]) -> list[str]:
    """The oracle against the hand-written expectations of the steps."""
    problems = []
    for seq, step in enumerate(STEPS, start=1):
        if step.expect is None:
            continue
        found = {path: op for path, (op, _, _) in expected[seq].items()}
        if found != dict(step.expect):
            problems.append(f"seq {seq} {step.name}: oracle {found} != hand {dict(step.expect)}")
    return problems


def check_provider(chain: ImageChain, records: Sequence[CheckpointRecord]) -> list[str]:
    """Every checkpoint has a readable top layer and the base is alpine's one layer."""
    problems = []
    if len(chain.base_layers) != 1:
        problems.append(f"expected alpine's 1 base layer, got {len(chain.base_layers)}")
    if sorted(chain.tops) != [r.seq for r in records]:
        problems.append(f"tops for seqs {sorted(chain.tops)}, expected all {len(records)}")
    for seq, source in sorted(chain.tops.items()):
        with source.open() as stream, tarfile.open(fileobj=stream, mode="r|*") as tar:
            if not any(True for _ in tar):
                problems.append(f"seq {seq}: top layer has no members")
    return problems


def run_reconcile(
    chain: ImageChain,
    records: Sequence[CheckpointRecord],
    owner: Mapping[str, int],
    work: Path,
    blobs: Path,
) -> tuple[tuple[DeltaRow, ...], tuple[int, ...], float]:
    blobs.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=work, prefix="reconcile-"))
    start = time.monotonic()
    try:
        result = reconcile(
            chain, records, owner, trial_id=uuid.uuid4(), blobs_dir=blobs, work_dir=scratch
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return result.deltas, result.computed_seqs, time.monotonic() - start


def strip_trial(rows: Sequence[DeltaRow]) -> list[tuple[object, ...]]:
    """Rows without their trial id, ordered by (step_idx, path)."""
    dumped = [row.model_dump(exclude={"trial_id"}) for row in rows]
    return [tuple(d.values()) for d in sorted(dumped, key=lambda d: (d["step_idx"], d["path"]))]


def main(keep_work: bool) -> int:
    run = secrets.token_hex(4)
    created = Created(trial_name=f"{TRIAL_PREFIX}{run}", containers=[], tags=[])
    work = Path(tempfile.mkdtemp(prefix="trajlab-reconcile-check-"))
    status = 0
    try:
        container = f"trajlab-reconcile-check-{run}"
        docker("run", "-d", "--network", "none", "--name", container, BASE_IMAGE, "sleep", "3600")
        created.containers.append(container)
        records = take_checkpoints(created, container, DockerCommitBackend())
        owner = {r.tool_call_id: 2 * r.seq + 1 for r in records}
        seq_of_step = {2 * r.seq + 1: r.seq for r in records}
        contents: dict[str, bytes] = {}
        expected = oracle(created, work, contents)
        for problem in oracle_sanity(expected):
            logger.error("oracle sanity: %s", problem)
            status = 1

        provider_log = logging.getLogger("trajlab.store.docker_images")
        with DockerImageProvider(work / "saves", base_image=BASE_IMAGE) as provider:
            provider_log.setLevel(logging.INFO)
            start = time.monotonic()
            chain = provider.chain(records)
            save_s = time.monotonic() - start
            provider_log.setLevel(logging.WARNING)
            for problem in check_provider(chain, records):
                logger.error("provider: %s", problem)
                status = 1
            try:
                rows, computed, reconcile_s = run_reconcile(
                    chain, records, owner, work, work / "blobs"
                )
            except NotImplementedError:
                logger.warning("reconcile() is not implemented yet: end-to-end comparison pending")
                for seq, step in enumerate(STEPS, start=1):
                    logger.info(
                        "seq %2d %-24s expected %5d rows", seq, step.name, len(expected[seq])
                    )
                logger.info("provider chain (inspect + save + manifest) took %.2f s", save_s)
                return status or 2

        got: dict[int, list[DeltaRow]] = {seq: [] for seq in expected}
        for row in rows:
            got[seq_of_step[row.step_idx]].append(row)
        if list(computed) != sorted(expected):
            logger.error("computed seqs %s, expected all of %s", computed, sorted(expected))
            status = 1

        logger.info(
            "%3s %-24s %8s %5s %10s %7s %11s", "seq", "step", "expected", "got", "mismatches",
            "save_s", "reconcile_s",
        )  # fmt: skip
        details: list[tuple[int, list[str]]] = []
        with DockerImageProvider(work / "saves-one", base_image=BASE_IMAGE) as one:
            previous: LayerSource | None = None
            for record, step in zip(records, STEPS, strict=True):
                seq = record.seq
                start = time.monotonic()
                single = one.chain([record])
                seq_save_s = time.monotonic() - start
                tops = {seq: single.tops[seq]} | ({seq - 1: previous} if previous else {})
                pair = ImageChain(base_layers=single.base_layers, tops=tops)
                pair_rows, _, seq_reconcile_s = run_reconcile(
                    pair, records, owner, work, work / "blobs-one"
                )
                previous = single.tops[seq]
                problems = compare(expected[seq], got[seq], contents, work / "blobs")
                mine = [row for row in pair_rows if row.step_idx == owner[record.tool_call_id]]
                if strip_trial(mine) != strip_trial(got[seq]):
                    problems.append("rows from checkpoints k-1 and k alone differ from the chain's")
                logger.info(
                    "%3d %-24s %8d %5d %10d %7.2f %11.2f", seq, step.name, len(expected[seq]),
                    len(got[seq]), len(problems), seq_save_s, seq_reconcile_s,
                )  # fmt: skip
                if problems:
                    details.append((seq, problems))
                    status = 1
        for seq, problems in details:
            logger.info("seq %d mismatches:", seq)
            for problem in problems[:MAX_LISTED]:
                logger.info("  %s", problem)
            if len(problems) > MAX_LISTED:
                logger.info("  ... and %d more", len(problems) - MAX_LISTED)
        logger.info(
            "whole chain: %d checkpoints, %d rows; provider (inspect + save + manifest) %.2f s, "
            "reconcile %.2f s",
            len(records),
            len(rows),
            save_s,
            reconcile_s,
        )
        logger.info("result: %s", "PASS" if status == 0 else "FAIL")
        return status
    finally:
        created.cleanup()
        if keep_work:
            logger.info("work dir kept: %s", work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--keep-work", action="store_true", help="keep the temporary work dir")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    logging.getLogger("trajlab").setLevel(logging.WARNING)
    sys.exit(main(arguments.keep_work))
