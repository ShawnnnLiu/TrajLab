"""Plan and run many Exp 2 probe attempts unattended: several roots, all three arms, N reps.

Built for the overnight run of 2026-11-05 (the run note `docs/experiments/exp2/
2026-11-02_engine-uptake-probe.md`, "Wider run"): control, record-engine, and record-files on
every root failure given, each attempt launched by `scripts/2026-11-02_exp2_engine_probe.py
launch` exactly as the earlier probes were.

    plan  write the attempts, one JSON object per line: rep by rep, and within a rep task by
          task and arm by arm, so cells stay balanced if the run stops early. A task with more
          than one root takes them in turn, rep by rep. Control runs the probe's `baseline`
          variant (control's text), the record arms `--record-variant` (default `v4`).
          Names are `<prefix>-<task tag>-<control|engine|files>-<rep>`.
    run   launch the planned attempts, `--workers` at a time, each holding one of the
          `--max-slots` slot files in `--slots` while it runs; another driver, or a Harbor job
          whose pid is written into a slot file by hand, shares the same slots, so the Mac never
          runs more trial containers than slots. A slot whose pid is gone is taken over. An
          attempt that finished (its job dir has a trial with result.json) is skipped; one that
          started and did not finish is reported and left alone. No attempt starts while the
          disk has less than `--min-free-gb` free (the run stops launching instead), or once
          the file `<plan>.stop` exists. Each start and end is printed as one line,
          `<name>: launching at <time>` and `<name>: exit <code>, "reward": <r>`.
    report  for the finished attempts of one or more plans: the probe's `summary` of each (one
          JSON line each, written to `--out`), then per task and arm the attempts counted (an
          attempt Harbor ended with an exception, such as a safety refusal, is listed apart and
          not counted), passes, diagnoses written, attempts that read a diff, median record
          characters, and cost; and, per task, Fisher's exact test (two-sided) of each record
          arm against control and of record-engine against record-files, and the exact test of
          all three arms at once (Freeman-Halton, the 2 x 3 table's probability ordering).

A watcher must hold the jobs dir (`uv run trajlab watch corpus/exp2/probe-jobs --every 1000
--gate none`). Paths are repo paths (corpus/exp2 may be a symlink): `trajlab run` takes only a
config inside the repo.

    uv run python scripts/2026-11-05_exp2_wide_run.py plan --prefix probe-wide --reps 10 \\
        --root mv corpus/stores/mvcc-local-v6 \\
            corpus/jobs/probe-mvcc-first/mvcc-lsm-compaction__CsiwPnL \\
        --out corpus/exp2/probe-jobs/_plans/wide-mvcc.jsonl
    uv run python scripts/2026-11-05_exp2_wide_run.py run \\
        corpus/exp2/probe-jobs/_plans/wide-mvcc.jsonl
"""

import argparse
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from math import comb, prod
from pathlib import Path
from queue import Empty, Queue
from typing import Any

logger = logging.getLogger("exp2_wide_run")

PROBE = "scripts/2026-11-02_exp2_engine_probe.py"
ARM_SHORT = {"control": "control", "record-engine": "engine", "record-files": "files"}
_print_lock = threading.Lock()


def say(line: str) -> None:
    with _print_lock:
        print(line, flush=True)


def plan(args: argparse.Namespace) -> None:
    roots: dict[str, list[tuple[str, str]]] = {}
    for tag, store, root in args.root:
        if (
            not (Path(store) / "record.duckdb").is_file()
            or not (Path(root) / "result.json").is_file()
        ):
            raise SystemExit(f"missing store or root: {store} {root}")
        roots.setdefault(tag, []).append((store, root))
    lines = []
    for rep in range(1, args.reps + 1):
        for tag, options in roots.items():
            store, root = options[(rep - 1) % len(options)]
            for arm in args.arms:
                lines.append(
                    {
                        "name": f"{args.prefix}-{tag}-{ARM_SHORT[arm]}-{rep}",
                        "task": tag,
                        "arm": arm,
                        "variant": "baseline" if arm == "control" else args.record_variant,
                        "rep": rep,
                        "store": store,
                        "root": root,
                    }
                )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(line) + "\n" for line in lines))
    logger.info("%d attempts planned in %s", len(lines), args.out)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_slot(slots: Path, max_slots: int, stop: threading.Event) -> Path | None:
    """A slot file this process created, holding its pid; None once `stop` is set."""
    slots.mkdir(parents=True, exist_ok=True)
    while not stop.is_set():
        for i in range(max_slots):
            slot = slots / f"slot-{i}"
            try:
                fd = os.open(slot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                try:
                    pid = int(slot.read_text().split()[0])
                except (OSError, ValueError, IndexError):
                    continue  # being written; look again next round
                if not _alive(pid):
                    logger.warning("taking over %s from gone pid %d", slot, pid)
                    slot.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as out:
                out.write(f"{os.getpid()} {threading.get_ident()}\n")
            return slot
        stop.wait(15)
    return None


def finished(jobs: Path, name: str) -> bool:
    job = jobs / name
    return job.is_dir() and any((t / "result.json").is_file() for t in job.iterdir() if t.is_dir())


def launch(item: dict[str, Any], jobs: Path, chains: Path) -> None:
    name = item["name"]
    logs = jobs / "_exp2-probe"
    logs.mkdir(parents=True, exist_ok=True)
    say(f"{name}: launching at {datetime.now(UTC):%H:%M:%SZ}")
    with (
        (logs / f"{name}.report.json").open("w") as out,
        (logs / f"{name}.launch.log").open("w") as err,
    ):
        code = subprocess.run(
            [
                "uv",
                "run",
                "python",
                PROBE,
                "launch",
                "--corpus-store",
                item["store"],
                "--root",
                item["root"],
                "--jobs-dir",
                str(jobs),
                "--chains",
                str(chains),
                "--arm",
                item["arm"],
                "--variant",
                item["variant"],
                "--name",
                name,
            ],
            stdout=out,
            stderr=err,
        ).returncode
    reward = "no reward"
    try:
        reward = f'"reward": {json.loads((logs / f"{name}.report.json").read_text())["reward"]}'
    except (OSError, ValueError, KeyError):
        pass
    say(f"{name}: exit {code}, {reward}")


def run(args: argparse.Namespace) -> None:
    items = [json.loads(line) for line in args.plan.read_text().splitlines() if line.strip()]
    stop_file = args.plan.with_name(args.plan.name + ".stop")
    queue: Queue[dict[str, Any]] = Queue()
    for item in items:
        if finished(args.jobs_dir, item["name"]):
            say(f"{item['name']}: done before; skipped")
        elif (args.jobs_dir / item["name"]).exists() or (
            args.jobs_dir / "_exp2-probe" / item["name"]
        ).exists():
            say(f"{item['name']}: started before and not finished; left alone")
        else:
            queue.put(item)
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            if stop_file.exists():
                say(f"stop file {stop_file} found: no more launches")
                stop.set()
                return
            free_gb = shutil.disk_usage("/").free / 1e9
            if free_gb < args.min_free_gb:
                say(f"disk low ({free_gb:.1f} GB free < {args.min_free_gb}): no more launches")
                stop.set()
                return
            slot = acquire_slot(args.slots, args.max_slots, stop)
            if slot is None:
                return
            try:
                try:
                    item = queue.get_nowait()
                except Empty:
                    return
                if stop_file.exists():
                    say(f"stop file {stop_file} found: no more launches")
                    stop.set()
                    return
                launch(item, args.jobs_dir, args.chains)
            finally:
                slot.unlink(missing_ok=True)

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    for thread in threads:
        thread.start()
        time.sleep(5)  # stagger the first launches
    for thread in threads:
        thread.join()
    say(f"run of {args.plan} ended; {queue.qsize()} planned attempts not launched")


def _exception(jobs: Path, name: str) -> str | None:
    for trial in (jobs / name).iterdir():
        result = trial / "result.json"
        if trial.is_dir() and result.is_file():
            info = json.loads(result.read_text()).get("exception_info") or {}
            return info.get("exception_type")
    return None


def fisher_2x2(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p of [[a, b], [c, d]] (passed, failed per row)."""
    return freeman_halton([(a, b), (c, d)])


def freeman_halton(rows: list[tuple[int, int]]) -> float:
    """Exact p of an r x 2 table with fixed margins: the probability of every table no more
    likely than the observed one."""
    n_row = [p + f for p, f in rows]
    passed = sum(p for p, _ in rows)
    total = sum(n_row)

    def prob(xs: tuple[int, ...]) -> float:
        return prod(comb(n, x) for n, x in zip(n_row, xs, strict=True)) / comb(total, passed)

    observed = prob(tuple(p for p, _ in rows))
    out = 0.0

    def walk(i: int, left: int, xs: tuple[int, ...]) -> None:
        nonlocal out
        if i == len(rows) - 1:
            if 0 <= left <= n_row[i]:
                q = prob((*xs, left))
                if q <= observed * (1 + 1e-9):
                    out += q
            return
        for x in range(0, min(n_row[i], left) + 1):
            walk(i + 1, left - x, (*xs, x))

    walk(0, passed, ())
    return min(1.0, out)


def report(args: argparse.Namespace) -> None:
    items = [
        json.loads(line)
        for plan_path in args.plans
        for line in plan_path.read_text().splitlines()
        if line.strip()
    ]
    done = [item for item in items if finished(args.jobs_dir, item["name"])]
    summary = subprocess.run(
        ["uv", "run", "python", PROBE, "summary", *(str(args.jobs_dir / i["name"]) for i in done)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = {json.loads(line)["job"]: json.loads(line) for line in summary.splitlines() if line}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(summary)
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    excluded: list[str] = []
    for item in done:
        kind = _exception(args.jobs_dir, item["name"])
        if kind is not None:
            excluded.append(f"{item['name']} ({kind})")
            continue
        cells.setdefault((item["task"], item["arm"]), []).append(rows[item["name"]])
    out: dict[str, Any] = {"excluded": excluded, "cells": [], "tests": []}
    for (task, arm), rs in sorted(cells.items()):
        out["cells"].append(
            {
                "task": task,
                "arm": arm,
                "attempts": len(rs),
                "passed": sum(int(r["reward"] or 0) for r in rs),
                "diagnosis_written": sum(bool(r["diagnosis_written"]) for r in rs),
                "read_a_diff": sum(r["hunk_reads"] > 0 for r in rs),
                "median_record_chars": statistics.median(r["record_chars"] for r in rs),
                "median_agent_minutes": statistics.median(
                    r["agent_minutes"] for r in rs if r["agent_minutes"] is not None
                ),
                "cost_usd": round(sum(r["cost_usd"] or 0 for r in rs), 2),
            }
        )
    for task in sorted({task for task, _ in cells}):
        by_arm = {
            arm: (sum(int(r["reward"] or 0) for r in rs), len(rs))
            for (t, arm), rs in cells.items()
            if t == task
        }
        pairs = [("record-engine", "control"), ("record-files", "control")]
        pairs.append(("record-engine", "record-files"))
        for first, second in pairs:
            if first in by_arm and second in by_arm:
                (p1, n1), (p2, n2) = by_arm[first], by_arm[second]
                out["tests"].append(
                    {
                        "task": task,
                        "test": f"{first} vs {second}",
                        "p": round(fisher_2x2(p1, n1 - p1, p2, n2 - p2), 3),
                    }
                )
        if len(by_arm) == 3:
            table = [(p, n - p) for p, n in by_arm.values()]
            out["tests"].append(
                {"task": task, "test": "all three arms", "p": round(freeman_halton(table), 3)}
            )
    print(json.dumps(out, indent=1))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Write the attempts of a run.")
    p.add_argument("--prefix", required=True)
    p.add_argument("--reps", type=int, required=True)
    p.add_argument(
        "--root",
        nargs=3,
        action="append",
        required=True,
        metavar=("TAG", "STORE", "ROOT_TRIAL_DIR"),
        help="A root failure; repeat, more than one per tag rotates them by rep.",
    )
    p.add_argument(
        "--arms",
        nargs="+",
        default=["control", "record-engine", "record-files"],
        choices=sorted(ARM_SHORT),
    )
    p.add_argument("--record-variant", default="v4")
    p.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run", help="Launch the planned attempts.")
    r.add_argument("plan", type=Path)
    r.add_argument("--workers", type=int, default=2)
    r.add_argument("--slots", type=Path, default=Path("corpus/exp2/probe-jobs/_slots"))
    r.add_argument("--max-slots", type=int, default=2)
    r.add_argument("--min-free-gb", type=float, default=15.0)
    r.add_argument("--jobs-dir", type=Path, default=Path("corpus/exp2/probe-jobs"))
    r.add_argument("--chains", type=Path, default=Path("corpus/exp2/probe"))
    t = sub.add_parser("report", help="Summarize finished attempts of one or more plans.")
    t.add_argument("plans", type=Path, nargs="+")
    t.add_argument("--jobs-dir", type=Path, default=Path("corpus/exp2/probe-jobs"))
    t.add_argument("--out", type=Path, required=True, help="Where the summary lines go.")
    args = parser.parse_args()
    if args.command == "report":
        report(args)
        return
    if args.command == "plan":
        plan(args)
    else:
        if Path.cwd().resolve() != Path(__file__).resolve().parent.parent:
            sys.exit("run from the repo root")
        run(args)


if __name__ == "__main__":
    main()
