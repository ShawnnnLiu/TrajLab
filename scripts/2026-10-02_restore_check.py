"""Restore a trial's checkpoints into fresh containers and check that each holds the right state.

Manual: needs Docker and the trial's checkpoint images (`trajlab-checkpoint:<trial>.<seq>`).
Run after `trajlab postprocess`, since it reads the enriched trajectory.

For each checkpoint k it starts a container from the image (no Harbor, no agent) and checks:

1. Files written by the agent: for every `Write` call covered by checkpoints 1..k, the file's
   bytes equal the call's `content`, and for every `Edit` call its `new_string` is in the file,
   unless a later call up to k changed that path again (per the calls' `changed_paths`).
2. Paths, from the calls' measured `changed_paths`: every path a call up to k added (`+path`)
   and no later call up to k removed is present, and every path a call after k added that no
   call up to k touched is absent. This covers files written through Bash, which check 1 cannot.
3. With `--verify`, the task's own verifier (`tests/test.sh` from Harbor's task cache) runs in the
   restored container; on the last checkpoint its reward must equal the trial's reward in
   result.json, which shows the trial can be graded, and so resumed, from a checkpoint alone.

    uv run python scripts/2026-10-02_restore_check.py corpus/jobs/<job>/<trial> [--verify]

Prints one JSON object per checkpoint and a summary; exits 1 if any check fails.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import TrialResult

from trajlab.contracts import CallRecord, CheckpointRecord

TASK_CACHE = Path.home() / ".cache/harbor/tasks/packages"


def docker(*args: str, check: bool = True, input: bytes | None = None) -> bytes:
    done = subprocess.run(["docker", *args], capture_output=True, input=input, check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)}: {done.stderr.decode(errors='replace')}")
    return done.stdout


def read_jsonl[M](path: Path, model: type[M]) -> list[M]:
    return [model.model_validate_json(line) for line in path.read_text().splitlines() if line]  # type: ignore[attr-defined]


def path_of(entry: str) -> str:
    return entry[1:]


def tool_calls(enriched: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        call["tool_call_id"]: call
        for step in enriched["steps"]
        for call in step.get("tool_calls") or []
    }


def check_checkpoint(
    container: str,
    seq: int,
    calls: list[CallRecord],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Checks 1 and 2 for the container restored from checkpoint `seq`."""
    covered = [c for c in calls if c.checkpoint_seq is not None and c.checkpoint_seq <= seq]
    covered_ids = {c.tool_call_id for c in covered}
    later = [c for c in calls if c.tool_call_id not in covered_ids and c.trigger == "tool_call"]
    order = {c.tool_call_id: c.call_seq for c in calls}

    def touched_after(path: str, call_seq: int) -> bool:
        return any(
            c.call_seq > call_seq and any(path_of(p) == path for p in c.changed_paths)
            for c in covered
        )

    content_ok, content_bad = 0, []
    for call_id in sorted(covered_ids, key=order.__getitem__):
        call = by_id.get(call_id)
        if call is None or call["function_name"] not in ("Write", "Edit"):
            continue
        args = call["arguments"]
        path = args["file_path"]
        if touched_after(path, order[call_id]):
            continue
        data = docker("exec", container, "cat", path, check=False)
        if call["function_name"] == "Write":
            ok = data == args["content"].encode()
        else:
            ok = args["new_string"].encode() in data
        if ok:
            content_ok += 1
        else:
            content_bad.append(f"{call['function_name']} {path}")

    def exists(path: str) -> bool:
        return subprocess.run(["docker", "exec", container, "test", "-e", path]).returncode == 0

    added: dict[str, bool] = {}
    for c in sorted(covered, key=lambda c: c.call_seq):
        for entry in c.changed_paths:
            if entry[0] in "+-":
                added[path_of(entry)] = entry[0] == "+"
    expected = sorted(path for path, alive in added.items() if alive)
    missing = [p for p in expected if not exists(p)]

    touched_by_covered = {path_of(p) for c in covered for p in c.changed_paths}
    future = sorted(
        {path_of(p) for c in later for p in c.changed_paths if p.startswith("+")}
        - touched_by_covered
    )
    present = [p for p in future if exists(p)]
    return {
        "agent_files_matching": content_ok,
        "agent_files_wrong": content_bad,
        "added_paths_checked": len(expected),
        "added_paths_missing": missing,
        "future_paths_checked": len(future),
        "future_paths_present": present,
    }


def run_verifier(container: str, tests_dir: Path) -> tuple[float | None, float]:
    """Run the task's tests/test.sh as Harbor does; return (reward, seconds)."""
    docker("exec", container, "mkdir", "-p", "/logs/verifier", "/tests")
    docker("cp", f"{tests_dir}/.", f"{container}:/tests")
    started = time.monotonic()
    docker("exec", container, "bash", "/tests/test.sh", check=False)
    seconds = time.monotonic() - started
    raw = docker("exec", container, "cat", "/logs/verifier/reward.txt", check=False).strip()
    return (float(raw) if raw else None), seconds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trial_dir", type=Path)
    parser.add_argument("--verify", action="store_true", help="Run the task verifier as well.")
    parser.add_argument(
        "--verify-seqs",
        type=int,
        nargs="*",
        help="Checkpoints to run the verifier in (default: the middle one and the last).",
    )
    args = parser.parse_args()
    trial: Path = args.trial_dir
    config = TrialConfig.model_validate_json((trial / "config.json").read_text())
    result = TrialResult.model_validate_json((trial / "result.json").read_text())
    reward = (
        (result.verifier_result.rewards or {}).get("reward") if result.verifier_result else None
    )
    records = read_jsonl(trial / "agent/checkpoints/checkpoints.jsonl", CheckpointRecord)
    calls = read_jsonl(trial / "agent/checkpoints/calls.jsonl", CallRecord)
    enriched = json.loads((trial / "agent/trajectory.enriched.json").read_text())
    by_id = tool_calls(enriched)
    task_dirs = sorted((TASK_CACHE / result.task_name).glob("*/tests"))
    if args.verify and len(task_dirs) != 1:
        raise SystemExit(f"expected one cached task for {result.task_name}, found {task_dirs}")
    verify_seqs = set(
        args.verify_seqs
        if args.verify_seqs is not None
        else {records[(len(records) - 1) // 2].seq, records[-1].seq}
    )

    failed = False
    for record in records:
        started = time.monotonic()
        container = (
            docker(
                "run",
                "-d",
                "--rm",
                "--label",
                "trajlab.restore_check=1",
                "--entrypoint",
                "sleep",
                record.checkpoint_id,
                "infinity",
            )
            .decode()
            .strip()
        )
        restore_s = time.monotonic() - started
        try:
            outcome: dict[str, Any] = {
                "seq": record.seq,
                "trigger": record.trigger,
                "tool_call_id": record.tool_call_id,
                "image": record.checkpoint_id,
                "restore_s": round(restore_s, 2),
            }
            outcome |= check_checkpoint(container, record.seq, calls, by_id)
            if args.verify and record.seq in verify_seqs:
                got, seconds = run_verifier(container, task_dirs[0])
                outcome |= {"verifier_reward": got, "verifier_s": round(seconds, 1)}
                if record.seq == records[-1].seq:
                    outcome["trial_reward"] = reward
                    failed |= got != reward
            failed |= bool(
                outcome["agent_files_wrong"]
                or outcome["added_paths_missing"]
                or outcome["future_paths_present"]
            )
            print(json.dumps(outcome))
        finally:
            docker("rm", "-f", container, check=False)
    print(json.dumps({"trial": config.trial_name, "checkpoints": len(records), "ok": not failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
