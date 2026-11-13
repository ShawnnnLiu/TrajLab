"""Run Exp 2 retry chains to a first pass or N retries on the development Mac, and report
solved-by-k per arm (the pass@k probe of 2026-11-10, run note
`docs/experiments/exp2/2026-11-10_passk-probe.md`).

Each chain is the loop's own (`trajlab.exp2.loop.Exp2Runner`, ADR-0022): an attempt in a fresh
environment, the verifier's reward, and for record-engine the failed attempt built into the
chain's store (`trajlab.exp2.chain.ingest`: its calls, per-call deltas, and final message) before
the next attempt's query server starts, so attempt t queries attempts 0 to t-1. Control gets no
record; its note counts the earlier attempts, as in `tb40-exp2`. The prompts are
`trajlab.exp2.prompt.prefix` unchanged.

What differs from `trajlab exp2 run`, which cannot start on macOS, and only that:

- no preregistration: every launch records `PROBE_LABEL` where a run records its commit;
- no write recorder (it needs fanotify): launches hold only while no watcher holds the jobs dir;
- `trajlab run --allow-dirty`, manifests under `<chains root>/_manifests`, nothing committed;
- `ANTHROPIC_API_KEY` is removed from each attempt's environment, so trials run on the
  subscription token of `.env`;
- several runs (one per lane: a run name, a corpus store, its job dir, its root failures) share
  `--max-running` attempts, a free place going to the lane whose next attempt has the lowest
  index, lanes in plan order on a tie, so chains advance level by level.

    plan    write the lanes, the budget N, and the arms as one JSON file.
    check   without Docker: build each lane's settings (refused unless every root is a usable
            failure of its store), check every prompt of every k against the task's tests
            (`trajlab.exp2.leaks`), and print one JSON line per lane.
    run     run the chains until each has passed or counted N attempts. Restartable: chains
            resume from their logs. Once `<plan>.stop` exists nothing new is launched; the
            attempts under way finish and are ingested, then it exits. Progress goes to the
            log (stderr) and to `<chains root>/status.json`.
    report  print JSON: per chain its counted attempts (reward, minutes, cost, tool calls,
            queries, the graded number where the task has one) and the index of its first
            pass; per task and arm, for k = 1..N, `solved` (chains whose first pass is at or
            before k) over `known` (chains that passed by k or have k counted attempts), and
            the attempts to a first pass with chains that never passed listed as censored.
            `graded` is the verifier's `Geomean speedup` (vf2-speedup-networkx) or `combined`
            (freecad-platform-drawing); `checks_passed` of `checks_total` are the verifier's
            tests (`verifier/ctrf.json`). `cut_reads` counts query calls whose output Claude Code
            cut to a preview; `attempt_filters` counts queries that filter on `attempt_idx`.
            `api_key_sources` must be `["none"]`: the subscription.

A watcher must hold the jobs dir with the corpus's policy, since each failed record-engine
attempt is ingested from its per-call checkpoints:

    caffeinate -dimsu uv run trajlab watch corpus/exp2/passk-jobs --every 1 --gate change
    uv run python scripts/2026-11-10_exp2_passk_chains.py plan --out corpus/exp2/passk/plan.json \\
        --lane passk-mv-a corpus/stores/mvcc-local-v6 corpus/jobs/probe-mvcc-first \\
            mvcc-lsm-compaction__CsiwPnL mvcc-lsm-compaction__JVTs2ov
    uv run python scripts/2026-11-10_exp2_passk_chains.py check corpus/exp2/passk/plan.json
    caffeinate -dimsu uv run python scripts/2026-11-10_exp2_passk_chains.py run \\
        corpus/exp2/passk/plan.json
    uv run python scripts/2026-11-10_exp2_passk_chains.py report corpus/exp2/passk/plan.json
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajlab.atif.load import load_trial_trajectory
from trajlab.checkpoint.watcher import watcher_running
from trajlab.contracts import QUERY_LOG_FILENAME, AttemptResult, Exp2Settings, QueryLogEntry
from trajlab.exp2 import chain as chains
from trajlab.exp2.loop import Exp2Runner
from trajlab.exp2.prereg import instructions, prompt_leaks
from trajlab.exp2.prereg import settings as exp2_settings
from trajlab.exp2.prompt import QUERY_COMMAND, prefix, render

logger = logging.getLogger("exp2_passk_chains")

CORPUS_CONFIG = Path("configs/harbor/tb40-sonnet-v4.json")
PROBE_LABEL = "none: probe of 2026-11-10 (scripts/2026-11-10_exp2_passk_chains.py)"
PRIMARY = "record-engine against control: attempts to a first pass, censored at N"
TEST = "none fixed: a descriptive probe (solved-by-k per arm and attempts to a first pass)"
STATUS_FILENAME = "status.json"
SPEEDUP = re.compile(r"Geomean speedup:\s*([\d.]+)x")
ATTEMPT_FILTER = re.compile(r"attempt_idx\s*(=|<|>|in\b|between\b)", re.IGNORECASE)
API_KEY_SOURCE = re.compile(r'"apiKeySource":"([^"]*)"')
CUT_MARK = "Output too large"


def plan(args: argparse.Namespace) -> None:
    if any(len(lane) < 4 for lane in args.lane):
        raise SystemExit("a lane is a run name, a store, a job dir, and at least one trial")
    lanes = [
        {"run": run, "store": store, "job": job, "trials": trials}
        for run, store, job, *trials in args.lane
    ]
    for lane in lanes:
        if not (Path(lane["store"]) / "record.duckdb").is_file():
            raise SystemExit(f"{lane['store']} is not a store")
        for trial in lane["trials"]:
            if not (Path(lane["job"]) / trial / "result.json").is_file():
                raise SystemExit(f"{lane['job']}/{trial} is not a finished trial")
    if len({lane["run"] for lane in lanes}) != len(lanes):
        raise SystemExit("each lane needs its own run name")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    out = {"budget": args.budget, "arms": args.arms, "lanes": lanes}
    args.out.write_text(json.dumps(out, indent=2) + "\n")
    logger.info("%d lanes, N = %d, arms %s in %s", len(lanes), args.budget, args.arms, args.out)


def lane_settings(found: dict[str, Any]) -> list[Exp2Settings]:
    """One run's settings per lane, as `trajlab exp2 prereg` would fix them, with this probe's
    comparison in place of the full run's."""
    out = []
    for lane in found["lanes"]:
        made = exp2_settings(
            run=lane["run"],
            kind="dry-run",
            corpus_job=Path(lane["job"]),
            corpus_store=Path(lane["store"]),
            corpus_config=CORPUS_CONFIG,
            trials=lane["trials"],
            budget=found["budget"],
            arms=found["arms"],
            test=TEST,
        )
        out.append(made.model_copy(update={"primary": PRIMARY}))
    return out


def check(args: argparse.Namespace) -> None:
    for made in lane_settings(json.loads(args.plan.read_text())):
        instruction = instructions(Path(made.corpus_store), made.sample)
        sizes = [
            len(render(prefix(arm, k), instruction[root.trial_name]).encode("utf-8"))
            for root in made.sample
            for arm in made.arms
            for k in range(1, made.budget + 1)
        ]
        row = {
            "run": made.run,
            "roots": [root.trial_name for root in made.sample],
            "arms": list(made.arms),
            "budget": made.budget,
            "prompts": len(sizes),
            "max_prompt_bytes": max(sizes),
            "leaks": prompt_leaks(made),
        }
        print(json.dumps(row))


def spawn(command: list[str], log_path: Path) -> subprocess.Popen[bytes]:
    """`trajlab run` as the loop starts it, for a tree with uncommitted changes, and without
    the shell's API key: the trial's credential is the env file's subscription token."""
    env = {name: value for name, value in os.environ.items() if name != "ANTHROPIC_API_KEY"}
    with log_path.open("ab") as log:
        return subprocess.Popen(
            [*command, "--allow-dirty"], stdout=log, stderr=subprocess.STDOUT, env=env
        )


def watcher_only(jobs_dir: Path) -> str | None:
    if watcher_running(jobs_dir):
        return None
    return f"no watcher holds {jobs_dir}: start `trajlab watch {jobs_dir} --every 1 --gate change`"


def next_attempt(runner: Exp2Runner) -> int | None:
    """The lowest attempt index among the runner's chains that are ready to launch."""
    busy = {id(entry.chain) for entry in runner.running.values()}
    ready = [
        state.attempt_idx
        for chain in runner.chains
        if chain.name not in runner.ingesting and id(chain) not in busy
        for state in [runner.state(chain)]
        if state.kind == "launch"
    ]
    return min(ready, default=None)


def one_pass(runners: list[Exp2Runner], cap: int, launching: bool) -> None:
    for runner in runners:  # record what finished and start its ingest; launch nothing
        runner.max_running = 0
        runner.step()
    held: set[int] = set()
    while launching and sum(len(r.running) for r in runners) < cap:
        ready = [
            (depth, i)
            for i, runner in enumerate(runners)
            if i not in held and (depth := next_attempt(runner)) is not None
        ]
        if not ready:
            break
        _, i = min(ready)
        before = len(runners[i].running)
        runners[i].max_running = before + 1
        runners[i].step()
        if len(runners[i].running) == before:  # held (disk, watcher, a pause) or its chain stopped
            held.add(i)


def write_status(path: Path, runners: list[Exp2Runner], launching: bool) -> None:
    out = {
        "updated_at": datetime.now(UTC).isoformat(),
        "launching": launching,
        "running": sorted(name for r in runners for name in r.running),
        "runs": [r.status() for r in runners],
    }
    path.write_text(json.dumps(out, indent=2) + "\n")


def run(args: argparse.Namespace) -> None:
    if Path.cwd().resolve() != Path(__file__).resolve().parent.parent:
        sys.exit("run from the repo root")
    args.chains_root.mkdir(parents=True, exist_ok=True)
    args.jobs_dir.mkdir(parents=True, exist_ok=True)
    runners = [
        Exp2Runner(
            settings=made,
            preregistration=PROBE_LABEL,
            root=args.chains_root,
            jobs_dir=args.jobs_dir,
            manifests_dir=args.chains_root / "_manifests",
            work_dir=args.chains_root / "_work",
            env_file=Path(".env"),
            max_running=0,
            hold_below_gb=args.hold_below_gb,
            retry_failed_ingests=True,
            spawn=spawn,
            capture_problem=watcher_only,
            commit_manifests=lambda _: [],
        )
        for made in lane_settings(json.loads(args.plan.read_text()))
    ]
    stop_file = args.plan.with_name(args.plan.name + ".stop")
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    with ThreadPoolExecutor(max_workers=1) as executor:
        for runner in runners:
            runner.executor = executor
        launching = True
        while True:
            if launching and (stop.is_set() or stop_file.exists()):
                logger.warning("stopping: nothing new is launched")
                launching = False
            one_pass(runners, args.max_running, launching)
            write_status(args.chains_root / STATUS_FILENAME, runners, launching)
            idle = not any(r.running or r.ingesting for r in runners)
            if idle and (not launching or all(r.done() for r in runners)):
                break
            if launching:
                stop.wait(args.poll)
            else:
                time.sleep(args.poll)
        for runner in runners:
            runner.executor = None
    for runner in runners:
        for chain in runner.chains:
            state = runner.state(chain)
            logger.info(
                "%s %s: %s at attempt %d %s",
                runner.settings.run,
                chain.name,
                state.kind,
                state.attempt_idx,
                state.reason,
            )


def graded(trial: Path) -> float | None:
    stdout = trial / "verifier" / "test-stdout.txt"
    if stdout.is_file() and (found := SPEEDUP.search(stdout.read_text(errors="replace"))):
        return float(found.group(1))
    details = trial / "verifier" / "reward_details.json"
    if details.is_file():
        value = json.loads(details.read_text()).get("combined")
        return float(value) if value is not None else None
    return None


def attempt_row(attempt: AttemptResult, jobs_dir: Path) -> dict[str, Any]:
    row: dict[str, Any] = {
        "attempt": attempt.attempt_idx,
        "job": attempt.job_name,
        "outcome": attempt.outcome,
        "reward": attempt.reward,
        "minutes": round((attempt.finished_at - attempt.started_at).total_seconds() / 60, 1),
        "prompt_delivered": attempt.prompt_delivered,
    }
    if attempt.trial_name is None:
        return row
    trial = jobs_dir / attempt.job_name / attempt.trial_name
    row["graded"] = graded(trial)
    ctrf = trial / "verifier" / "ctrf.json"
    if ctrf.is_file():
        tests = json.loads(ctrf.read_text())["results"]["tests"]
        row["checks_passed"] = sum(test["status"] == "passed" for test in tests)
        row["checks_total"] = len(tests)
    stream = trial / "agent" / "claude-code.txt"
    if stream.is_file():
        sources = set(API_KEY_SOURCE.findall(stream.read_text(errors="replace")))
        row["api_key_sources"] = sorted(sources)
    if (trial / "agent" / "trajectory.json").is_file():
        trajectory = load_trial_trajectory(trial)
        metrics = trajectory.final_metrics
        row["cost_usd"] = round(metrics.total_cost_usd or 0, 4) if metrics else None
        calls = cut = 0
        for step in trajectory.steps:
            results = {
                r.source_call_id: r.content
                for r in (step.observation.results if step.observation else [])
            }
            for call in step.tool_calls or []:
                calls += 1
                command = (call.arguments or {}).get("command") or ""
                shown = results.get(call.tool_call_id)
                if QUERY_COMMAND in command and isinstance(shown, str) and CUT_MARK in shown[:400]:
                    cut += 1
        row["tool_calls"] = calls
        if attempt.arm == "record-engine":
            row["cut_reads"] = cut
    log = trial / "agent" / QUERY_LOG_FILENAME
    if attempt.arm == "record-engine" and log.is_file():
        queries = [
            QueryLogEntry.model_validate_json(line) for line in log.read_text().splitlines() if line
        ]
        row["queries"] = len(queries)
        row["row_capped"] = sum(bool(q.truncated) for q in queries)
        row["attempt_filters"] = sum(bool(ATTEMPT_FILTER.search(q.sql)) for q in queries)
    return row


def report(args: argparse.Namespace) -> None:
    found = json.loads(args.plan.read_text())
    budget: int = found["budget"]
    chain_rows: list[dict[str, Any]] = []
    for made in lane_settings(found):
        for root in made.sample:
            for arm in made.arms:
                directory = chains.chain_dir(args.chains_root, made.run, root.trial_name, arm)
                attempts = chains.read_attempts(directory)
                counted = [a for a in attempts if a.counted]
                first = next((a.attempt_idx for a in counted if a.outcome == "passed"), None)
                chain_rows.append(
                    {
                        "run": made.run,
                        "task": root.task.split("/")[-1],
                        "root": root.trial_name,
                        "arm": arm,
                        "counted": len(counted),
                        "first_pass": first,
                        "finished": first is not None or len(counted) >= budget,
                        "not_counted": [
                            f"{a.job_name}: {a.outcome} ({a.exception_type or a.note})"
                            for a in attempts
                            if not a.counted
                        ],
                        "ingests_failed": [
                            i.error for i in chains.read_ingests(directory) if not i.ok
                        ],
                        "attempts": [attempt_row(a, args.jobs_dir) for a in counted],
                    }
                )
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in chain_rows:
        cells.setdefault((row["task"], row["arm"]), []).append(row)
    curves = []
    for (task, arm), rows in sorted(cells.items()):
        solved_by_k = []
        for k in range(1, budget + 1):
            solved = sum(r["first_pass"] is not None and r["first_pass"] <= k for r in rows)
            known = sum(
                (r["first_pass"] is not None and r["first_pass"] <= k) or r["counted"] >= k
                for r in rows
            )
            solved_by_k.append({"k": k, "solved": solved, "known": known})
        curves.append(
            {
                "task": task,
                "arm": arm,
                "chains": len(rows),
                "first_pass": sorted(r["first_pass"] for r in rows if r["first_pass"] is not None),
                "censored_at": sorted(r["counted"] for r in rows if r["first_pass"] is None),
                "attempts_run": sum(r["counted"] for r in rows),
                "solved_by_k": solved_by_k,
            }
        )
    sources = sorted(
        {s for r in chain_rows for a in r["attempts"] for s in a.get("api_key_sources", [])}
    )
    out = {"budget": budget, "api_key_sources": sources, "curves": curves, "chains": chain_rows}
    print(json.dumps(out, indent=1))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Write the lanes of a run.")
    p.add_argument(
        "--lane",
        nargs="+",
        action="append",
        required=True,
        metavar="RUN STORE JOB_DIR TRIAL",
        help="A run name, its corpus store, the job dir of its root failures, and their trial "
        "dir names; repeat.",
    )
    p.add_argument("--budget", type=int, default=20, help="N: retries per chain at most.")
    p.add_argument("--arms", nargs="+", default=["control", "record-engine"])
    p.add_argument("--out", type=Path, required=True)
    for name, text in (
        ("check", "Check every lane and prompt, without Docker."),
        ("run", "Run the chains."),
        ("report", "Report the chains so far."),
    ):
        c = sub.add_parser(name, help=text)
        c.add_argument("plan", type=Path)
        if name == "check":
            continue
        c.add_argument("--chains-root", type=Path, default=Path("corpus/exp2/passk"))
        c.add_argument("--jobs-dir", type=Path, default=Path("corpus/exp2/passk-jobs"))
        if name == "run":
            c.add_argument("--max-running", type=int, default=2, help="Attempts at once.")
            c.add_argument("--hold-below-gb", type=float, default=15.0)
            c.add_argument("--poll", type=float, default=20.0, help="Seconds between passes.")
    args = parser.parse_args()
    {"plan": plan, "check": check, "run": run, "report": report}[args.command](args)


if __name__ == "__main__":
    main()
