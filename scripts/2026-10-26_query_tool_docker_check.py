"""Check the query tool end to end in real containers (ADR-0019; Exp 2 build-order step 4).

Part 1, no API: a store (built from tests/fixtures/hello-world-trial with `--no-images`
unless --store names one, which must be a schema-2 store), `trajlab store serve` on a temp job
dir in a thread, and for each image (default ubuntu:24.04, hello-world's base, and alpine:3.20
for BusyBox sh) a `docker run` with the trial's agent dir bind-mounted at /logs/agent as Harbor
mounts it, CLAUDE_CONFIG_DIR as Claude Code has it, and user 65534 (a user that is not the
host's, as in a trial). The container runs /logs/agent/bin/query three times: a SELECT, a DELETE,
and two statements. Checks: the rows and the count line on stdout, the exit codes, the refusals on
stderr, the files under queries/, and one log entry per call with the right outcome.

Part 2, `--harbor`, one hello-world trial through Harbor (one API call): a job under --jobs-dir
whose agent is `RepairClaudeCode` with a note asking for one query before the task, the server
on the job dir started first, as the retry loop will do. Checks: the trial finished with reward
1, the query log has one entry with rows, and the trajectory has a Bash step that ran
/logs/agent/bin/query whose observation holds the answer. Needs an env file with credentials
(`.env` by default) and the stock Claude Code install, so about 2 minutes.

Prints one JSON object per part; exits 1 if any check fails. Containers are removed; the job dir
is left for the record.

    uv run python scripts/2026-10-26_query_tool_docker_check.py [--store DIR] [--image IMG ...]
    uv run python scripts/2026-10-26_query_tool_docker_check.py --harbor [--env-file .env]
        [--jobs-dir DIR] [--job NAME]
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from harbor.models.trajectories import Trajectory
from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult

from trajlab.atif.postprocess import postprocess_trial
from trajlab.capture.harbor_runner import harbor_executable
from trajlab.capture.pins import CLAUDE_CODE_VERSION
from trajlab.contracts import QUERIES_DIRNAME, QUERY_LOG_FILENAME, QUERY_TOOL_RELPATH, QueryLogEntry
from trajlab.store.build import build_trial
from trajlab.store.serve import Server

logger = logging.getLogger("query_tool_docker_check")

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests/fixtures/hello-world-trial"
IMAGES = ["ubuntu:24.04", "alpine:3.20"]
NOBODY = "65534:65534"
TASK = "hello-world/hello-world"
MODEL = "anthropic/claude-sonnet-5-5"
QUERY = "SELECT count(*) AS n FROM steps"
NOTE = (
    "Before anything else, run this exact command with the Bash tool and quote its whole output "
    f'in your final message: /logs/agent/bin/query "{QUERY}" . Then do the task below.'
)
# What the container runs: the three calls, each followed by its exit status on stdout.
SCRIPT = f"""
q=/logs/agent/bin/query
$q "{QUERY}"; echo "exit=$?"
$q "DELETE FROM steps"; echo "exit=$?"
$q "SELECT 1; SELECT 2"; echo "exit=$?"
"""


def fixture_store(work: Path) -> Path:
    trial = Path(shutil.copytree(FIXTURE, work / FIXTURE.name))
    postprocess_trial(trial)
    store = work / "store"
    build_trial(trial, store, images=None, work_dir=work / "build")
    return store


def entries(agent_dir: Path) -> list[QueryLogEntry]:
    text = (agent_dir / QUERY_LOG_FILENAME).read_text()
    return [QueryLogEntry.model_validate_json(line) for line in text.splitlines()]


def part_container(store: Path, images: list[str], work: Path) -> dict[str, Any]:
    job_dir = work / "job"
    report: dict[str, Any] = {"part": "container", "store": str(store), "images": {}}
    stop = threading.Event()
    server = Server(store, job_dir, poll_s=0.05)
    job_dir.mkdir()
    thread = threading.Thread(target=server.run, args=(stop,))
    thread.start()
    try:
        for image in images:
            name = "check__" + image.replace(":", "-").replace("/", "-")
            agent_dir = job_dir / name / "agent"
            agent_dir.mkdir(parents=True)
            deadline = time.monotonic() + 10
            while not (agent_dir / QUERY_TOOL_RELPATH).is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            start = time.monotonic()
            run = subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-u",
                    NOBODY,
                    "-v",
                    f"{agent_dir}:/logs/agent",
                    "-e",
                    "CLAUDE_CONFIG_DIR=/logs/agent/sessions",
                    "-e",
                    "TRAJLAB_QUERY_WAIT=30",
                    image,
                    "sh",
                    "-c",
                    SCRIPT,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            seconds = round(time.monotonic() - start, 1)
            log = entries(agent_dir)
            found = sorted(p.name.split(".", 1)[1] for p in (agent_dir / QUERIES_DIRNAME).iterdir())
            checks = {
                "docker_exit_0": run.returncode == 0,
                "rows_and_count": run.stdout.startswith('{"n":2}\n-- 1 row\nexit=0\n'),
                "delete_refused": "refused: only a SELECT or WITH query" in run.stderr
                and "\nexit=1\n" in run.stdout,
                "two_statements_refused": "refused: one statement at a time, not 2" in run.stderr
                and run.stdout.endswith("exit=1\n"),
                "files": found == ["err", "err", "result", "sql", "sql", "sql"],
                "three_entries": [(e.rows, e.error is None) for e in log]
                == [(1, True), (None, False), (None, False)],
                "entries_name_the_trial": all(e.trial_name == name for e in log),
            }
            report["images"][image] = {
                "ok": all(checks.values()),
                "checks": checks,
                "seconds": seconds,
                "stdout": run.stdout,
                "stderr": run.stderr,
                "latency_ms": [e.latency_ms for e in log],
            }
            logger.info("%s: %s in %s s", image, "ok" if all(checks.values()) else "FAIL", seconds)
    finally:
        stop.set()
        thread.join()
    report["ok"] = all(image["ok"] for image in report["images"].values())
    return report


def part_harbor(store: Path, jobs_dir: Path, job: str, env_file: Path) -> dict[str, Any]:
    job_dir = jobs_dir / job
    if job_dir.exists():
        raise SystemExit(f"{job_dir} exists; pick another --job")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "job_name": job,
        "jobs_dir": str(jobs_dir),
        "n_concurrent_trials": 1,
        "agent_timeout_multiplier": 2.0,
        "agents": [
            {
                "import_path": "trajlab.capture.repair:RepairClaudeCode",
                "model_name": MODEL,
                "kwargs": {"version": CLAUDE_CODE_VERSION, "repair_note": NOTE},
            }
        ],
        "tasks": [{"name": TASK, "ref": "latest"}],
    }
    config_path = jobs_dir / f"{job}.config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    # As the retry loop will: the server on the job dir first, then Harbor.
    job_dir.mkdir()
    stop = threading.Event()
    thread = threading.Thread(target=Server(store, job_dir).run, args=(stop,))
    thread.start()
    command = [
        str(harbor_executable()),
        "run",
        "--config",
        str(config_path),
        "--env-file",
        str(env_file),
    ]
    logger.info("running: %s", " ".join(command))
    start = time.monotonic()
    try:
        returncode = subprocess.run(command, check=False).returncode
    finally:
        stop.set()
        thread.join()
    seconds = round(time.monotonic() - start)
    [trial_dir] = [p for p in job_dir.iterdir() if p.is_dir() and (p / "agent").is_dir()]
    paths = TrialPaths(trial_dir)
    result = TrialResult.model_validate_json(paths.result_path.read_text())
    reward = (
        (result.verifier_result.rewards or {}).get("reward") if result.verifier_result else None
    )
    log = entries(paths.agent_dir)
    trajectory = Trajectory.model_validate_json((paths.agent_dir / "trajectory.json").read_text())
    query_steps = [
        step
        for step in trajectory.steps
        for tool_call in step.tool_calls or []
        if tool_call.function_name == "Bash"
        and QUERY_TOOL_RELPATH in json.dumps(tool_call.arguments)
    ]
    answered = any(
        '{"n":' in (result_.content or "")
        for step in query_steps
        for result_ in (step.observation.results if step.observation else [])
    )
    checks = {
        "harbor_exit_0": returncode == 0,
        "reward_1": reward == 1.0,
        "no_exception": result.exception_info is None,
        "one_log_entry_with_rows": len(log) >= 1 and log[0].rows == 1 and log[0].error is None,
        "query_is_a_bash_step": len(query_steps) >= 1,
        "answer_in_the_observation": answered,
    }
    return {
        "part": "harbor",
        "ok": all(checks.values()),
        "checks": checks,
        "trial_dir": str(trial_dir),
        "seconds": seconds,
        "log": [e.model_dump(mode="json") for e in log],
        "query_steps": [s.step_id for s in query_steps],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--store", type=Path, help="A schema-2 store; default: built from the fixture."
    )
    parser.add_argument("--image", action="append", help=f"Image(s) for part 1; default {IMAGES}.")
    parser.add_argument("--harbor", action="store_true", help="Also run part 2.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--jobs-dir", type=Path, default=Path("corpus/jobs"))
    parser.add_argument("--job", default="query-tool-check-v1")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    work = Path(tempfile.mkdtemp(prefix="trajlab-query-check-"))
    ok = True
    try:
        store = args.store or fixture_store(work)
        report = part_container(store, args.image or IMAGES, work)
        print(json.dumps(report, indent=2))
        ok &= report["ok"]
        if args.harbor:
            report = part_harbor(store, args.jobs_dir, args.job, args.env_file)
            print(json.dumps(report, indent=2))
            ok &= report["ok"]
    finally:
        if args.keep_work:
            logger.info("work kept at %s", work)
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
