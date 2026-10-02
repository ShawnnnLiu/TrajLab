"""Validate the change detector (ADR-0010) against an independent ground truth.

Takes job dirs captured with `trajlab watch --gate audit --every 1`, where every hooked call
was measured and also checkpointed. A checkpoint image's top layer is the container's whole
writable layer at that moment, so comparing the top layers of consecutive checkpoints, file by
file and by content hash, says whether a call changed anything. That comparison shares no code
with the `find`-based detector.

For each call after a trial's baseline it reports the detector's verdict against the truth:

- content: some path's type, mode, owner, link target, or bytes differ (or it appeared or
  disappeared), outside the harness exclusions;
- touched: only modification times differ (e.g. bytes rewritten unchanged, `touch`);
- none: the layers are identical outside the exclusions.

A detector "unchanged" on a call with a content difference is a miss and would make the
paper's join unsound; there must be none. A detector "changed" on a touched-only call is the
expected conservative cost of comparing change times.

    uv run python scripts/2026-09-30_audit_detector.py corpus/jobs/<job> [...]
"""

import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

from trajlab.checkpoint.backends.docker_commit import REPOSITORY, image_tag
from trajlab.checkpoint.changes import excluded
from trajlab.contracts import CallRecord, CheckpointRecord

Entry = tuple[str, ...]


Layer = tuple[dict[str, tuple[Entry, int]], set[str]]


def layer_entries(layer: tarfile.TarFile) -> Layer:
    """path -> ((kind, mode, uid, gid, link, sha256), mtime), and the excluded paths.

    Whiteouts are deletions.
    """
    entries: dict[str, tuple[Entry, int]] = {}
    for member in layer:
        path = "/" + member.name.lstrip("./").rstrip("/")
        name = path.rsplit("/", 1)[-1]
        if name.startswith(".wh."):
            path = path.rsplit("/", 1)[0] + "/" + name.removeprefix(".wh.")
            entries[path] = (("whiteout",), 0)
            continue
        digest = ""
        if member.isfile():
            stream = layer.extractfile(member)
            assert stream is not None
            digest = hashlib.sha256(stream.read()).hexdigest()
        kind = "d" if member.isdir() else "l" if member.issym() else "h" if member.islnk() else "f"
        entry = (kind, oct(member.mode), str(member.uid), str(member.gid), member.linkname, digest)
        entries[path] = (entry, int(member.mtime) if kind != "d" else 0)
    harness = {p for p in entries if excluded(p)}
    return {p: e for p, e in entries.items() if p not in harness and p != "/"}, harness


def truth(before_layer: Layer, after_layer: Layer) -> str:
    (before, before_harness), (after, after_harness) = before_layer, after_layer
    content = {p for p in before.keys() ^ after.keys()} | {
        p for p in before.keys() & after.keys() if before[p][0] != after[p][0]
    }
    # A directory that came or went only to hold harness paths is not a change.
    harness = before_harness | after_harness

    def holds_only_harness(path: str) -> bool:
        prefix = path + "/"
        kind = (after.get(path) or before[path])[0][0]
        return (
            kind == "d"
            and any(h.startswith(prefix) for h in harness)
            and not any(other.startswith(prefix) for other in content)
        )

    content = {p for p in content if not holds_only_harness(p)}
    if content:
        return "content"
    if any(before[p][1] != after[p][1] for p in before.keys() & after.keys()):
        return "touched"
    return "none"


def top_layers(tags: list[str], work: Path) -> dict[str, Layer]:
    """Each checkpoint tag's top layer, from one `docker image save` (shared layers once)."""
    archive = work / "images.tar"
    subprocess.run(["docker", "image", "save", "-o", str(archive), *tags], check=True)
    result = {}
    with tarfile.open(archive) as outer:
        manifest = json.load(outer.extractfile("manifest.json"))  # type: ignore[arg-type]
        for image_manifest in manifest:
            top = outer.extractfile(image_manifest["Layers"][-1])
            with tarfile.open(fileobj=top) as layer:
                entries = layer_entries(layer)
            for tag in image_manifest.get("RepoTags") or []:
                result[tag] = entries
    archive.unlink()
    return result


def audit(trial: Path, work: Path, table: Counter[tuple[str, str]], misses: list[str]) -> None:
    checkpoints = trial / "agent" / "checkpoints"
    records = [
        CheckpointRecord.model_validate_json(line)
        for line in (checkpoints / "checkpoints.jsonl").read_text().splitlines()
    ]
    calls = {
        c.tool_call_id: c
        for c in (
            CallRecord.model_validate_json(line)
            for line in (checkpoints / "calls.jsonl").read_text().splitlines()
        )
    }
    tags = {r.seq: f"{REPOSITORY}:{image_tag(r.trial_name, r.seq)}" for r in records}
    layers = top_layers(list(tags.values()), work)
    for before, after in zip(records, records[1:], strict=False):
        call = calls[after.tool_call_id]
        if call.change not in ("changed", "unchanged"):
            continue
        actual = truth(layers[tags[before.seq]], layers[tags[after.seq]])
        table[(call.change, actual)] += 1
        if call.change == "unchanged" and actual == "content":
            misses.append(f"{trial.name} seq {after.seq} {after.tool_call_id}")
        print(f"  {trial.name} seq {after.seq:3d} {call.tool_name:6s} detector={call.change:9s} "
              f"truth={actual:7s} {list(call.changed_paths)[:3]}")  # fmt: skip


def main(job_dirs: list[Path]) -> int:
    table: Counter[tuple[str, str]] = Counter()
    misses: list[str] = []
    with tempfile.TemporaryDirectory(prefix="trajlab-audit-") as tmp:
        for job in job_dirs:
            for trial in sorted(
                p for p in job.iterdir() if (p / "agent/checkpoints/calls.jsonl").is_file()
            ):
                audit(trial, Path(tmp), table, misses)
    print("\ndetector \\ truth   content  touched  none")
    for verdict in ("changed", "unchanged"):
        row = [table[(verdict, t)] for t in ("content", "touched", "none")]
        print(f"{verdict:17s} {row[0]:8d} {row[1]:8d} {row[2]:5d}")
    print(f"misses (detector unchanged, content changed): {len(misses)} {misses}")
    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main([Path(p) for p in sys.argv[1:]]))
