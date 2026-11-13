"""ADR-0021 item 5: the write recorder's event rate and cost on a build-heavy command.

Runs one container from an image and, in it, a command that creates, writes, and removes many
files (copies of /usr/lib and /usr/local/lib, their Python modules byte-compiled, 20,000 small
files written, then all removed), several times,
alternating without and with a recorder (`trajlab.checkpoint.writes.Recorder`, as `trajlab
record-writes` runs it) that knows the container as a throwaway trial. Prints per run the wall
time of the command, the events the recorder's fanotify group read host-wide, the lines it
wrote for the container, and the recorder's CPU time; then the medians.

Run as root, with no other recorder running (stop `trajlab record-writes` first: it would see
the same events), on the capture server:

    sudo PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/2026-10-27_adr0021_recorder_cost.py \\
        <image> [--runs 3]
"""

import argparse
import logging
import resource
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from trajlab.checkpoint.backends.docker_commit import DockerCommitBackend
from trajlab.checkpoint.watcher import TrialIdentity
from trajlab.checkpoint.writes import Recorder, writes_path

# Like a package install: copy two library trees, byte-compile them, write 20,000 small files,
# count, and remove it all.
WORKLOAD = (
    "rm -rf /tmp/w && mkdir -p /tmp/w && cp -a /usr/lib /tmp/w/lib "
    "&& cp -a /usr/local/lib /tmp/w/local "
    "&& (python3 -m compileall -q -f /tmp/w > /dev/null 2>&1; true) "
    "&& python3 -c \"[open(f'/tmp/w/s{i}.txt', 'w').write('x' * 200) for i in range(20000)]\" "
    "&& find /tmp/w -type f | wc -l && rm -rf /tmp/w"
)


class One:
    """The one container, as the recorder's `Containers`."""

    def __init__(self, container: str) -> None:
        self.container = container

    def container_for(self, compose_project: str) -> str:
        return self.container

    def init_pid(self, container: str) -> int:
        return DockerCommitBackend().init_pid(container)


def cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def workload(container: str) -> tuple[float, str]:
    start = time.monotonic()
    out = subprocess.run(
        ["docker", "exec", container, "bash", "-c", WORKLOAD],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return time.monotonic() - start, out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("image")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    container = subprocess.run(
        ["docker", "run", "-d", "--entrypoint", "sleep", args.image, "infinity"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    scratch = Path(tempfile.mkdtemp(prefix="adr0021-cost-"))
    trial = scratch / "jobs" / "job" / "cost__1"
    (trial / "agent").mkdir(parents=True)
    (trial / "config.json").write_text("{}")
    results: dict[str, list[tuple[float, int, int, float]]] = {"off": [], "on": []}
    try:
        workload(container)  # warm the page cache once
        for run in range(1, args.runs + 1):
            wall, files = workload(container)
            results["off"].append((wall, 0, 0, 0.0))
            print(f"run {run} off: {wall:6.1f} s ({files} files)", flush=True)

            recorder = Recorder(
                scratch / "jobs", lambda _: TrialIdentity("cost__1", "cost"), One(container)
            )
            stop = threading.Event()
            thread = threading.Thread(target=recorder.run, args=(stop,))
            thread.start()
            while not recorder._trials:
                time.sleep(0.1)
            before = cpu()
            wall, files = workload(container)
            time.sleep(1)  # let the reader drain the queue
            used = cpu() - before
            stop.set()
            thread.join()
            seen = recorder.stats["seen"]
            path = writes_path(trial)
            lines = sum(1 for _ in path.open("rb"))
            path.unlink()
            results["on"].append((wall, seen, lines, used))
            print(
                f"run {run} on:  {wall:6.1f} s ({files} files), {seen} events read, "
                f"{lines} lines written, recorder cpu {used:.1f} s",
                flush=True,
            )
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)

    off = statistics.median(r[0] for r in results["off"])
    on = statistics.median(r[0] for r in results["on"])
    seen = statistics.median(r[1] for r in results["on"])
    lines = statistics.median(r[2] for r in results["on"])
    used = statistics.median(r[3] for r in results["on"])
    print(
        f"median: off {off:.1f} s, on {on:.1f} s ({(on - off) / off:+.1%}); "
        f"{seen:.0f} events read ({seen / on:.0f}/s), {lines:.0f} lines ({lines / on:.0f}/s), "
        f"recorder cpu {used:.1f} s ({used / on:.0%} of one core)"
    )


if __name__ == "__main__":
    main()
