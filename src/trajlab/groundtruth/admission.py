"""Host-wide admission of verifier environments by declared resources (ADR-0013, decision 3).

Every process that starts a verifier environment or a counterfactual container (`trajlab gt
replay` batches, `trajlab gt try-fix`) registers a claim in one ledger, by default
`~/.cache/trajlab/gt-admission/ledger.json`, under an `flock`. A claim fits while the running
claims' declared CPUs stay within `CPU_CAPACITY` and their memory within `MEMORY_CAPACITY_MB`.
A quiet claim (a task whose checks race wall-clock limits) runs alone among quiet claims and with
at most `QUIET_COMPANY_CPUS` of other CPUs beside it. Interactive claims (a try-fix, a fix
confirmation) take precedence: while one waits, no batch claim starts. A claim whose process has
died is removed,
and its compose project or container torn down, by whoever takes the lock next, so a killed batch
neither leaks verifier environments nor frees capacity they still use.
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CPU_CAPACITY = 7.0  # of the server's 8 cores; one is left to the system and the watcher
MEMORY_CAPACITY_MB = 24 * 1024  # of 30 GB, with no swap
QUIET_COMPANY_CPUS = 2.0
POLL_S = 3.0
LEDGER_DIR_ENV = "TRAJLAB_GT_ADMISSION_DIR"


def ledger_dir() -> Path:
    return Path(os.environ.get(LEDGER_DIR_ENV, Path.home() / ".cache" / "trajlab" / "gt-admission"))


def process_start(pid: int) -> str | None:
    """The process's start time in clock ticks since boot, to tell a reused pid apart."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


@dataclass(frozen=True)
class Claim:
    claim_id: str
    cpus: float
    memory_mb: int
    quiet: bool = False
    project: str | None = None  # compose project to take down if the owner dies
    container: str | None = None  # container to remove if the owner dies
    pid: int = field(default_factory=os.getpid)
    pid_start: str | None = field(default_factory=lambda: process_start(os.getpid()))

    def alive(self) -> bool:
        return self.pid_start is not None and process_start(self.pid) == self.pid_start


def fits(running: list[Claim], claim: Claim) -> bool:
    used_cpus = sum(c.cpus for c in running)
    used_memory = sum(c.memory_mb for c in running)
    if used_cpus + claim.cpus > CPU_CAPACITY or used_memory + claim.memory_mb > MEMORY_CAPACITY_MB:
        return not running  # a claim larger than the host still runs, alone
    quiet = [c for c in running if c.quiet]
    if claim.quiet:
        return not quiet and used_cpus <= QUIET_COMPANY_CPUS
    if quiet:
        others = sum(c.cpus for c in running if not c.quiet)
        return others + claim.cpus <= QUIET_COMPANY_CPUS
    return True


def tear_down(claim: Claim) -> None:
    """Take down what a claim started: its compose project or its container."""
    if claim.project:
        subprocess.run(
            ["docker", "compose", "-p", claim.project, "down", "--volumes", "--remove-orphans"],
            capture_output=True,
            check=False,
            timeout=300,
        )
    if claim.container:
        subprocess.run(
            ["docker", "rm", "--force", claim.container], capture_output=True, check=False
        )


class Ledger:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or ledger_dir()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[list[Claim]]:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "ledger.json"
        with (self.directory / "admission.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                claims = (
                    [Claim(**c) for c in json.loads(path.read_text())] if path.is_file() else []
                )
                for dead in [c for c in claims if not c.alive()]:
                    log.warning("tearing down %s left by dead pid %d", dead.claim_id, dead.pid)
                    tear_down(dead)
                    claims.remove(dead)
                yield claims
                staged = path.with_suffix(".tmp")
                staged.write_text(json.dumps([asdict(c) for c in claims], indent=1))
                staged.replace(path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _waiting(self) -> list[Claim]:
        """Interactive claims waiting for capacity (`waiting.json`), live ones only."""
        path = self.directory / "waiting.json"
        if not path.is_file():
            return []
        return [w for w in (Claim(**c) for c in json.loads(path.read_text())) if w.alive()]

    def _set_waiting(self, waiting: list[Claim]) -> None:
        path = self.directory / "waiting.json"
        staged = path.with_suffix(".tmp")
        staged.write_text(json.dumps([asdict(c) for c in waiting], indent=1))
        staged.replace(path)

    def try_admit(self, claim: Claim, *, batch: bool = False) -> list[str] | None:
        """Register the claim if it fits; return the ids running beside it, else None.

        An interactive claim (a try-fix, a confirmation) that does not fit is put on the waiting
        list, and no batch claim is admitted while any live claim waits there, so a running
        batch drains and lets the interactive one in.
        """
        with self._locked() as claims:
            waiting = self._waiting()
            if batch and waiting:
                return None
            ahead = next((i for i, w in enumerate(waiting) if w.claim_id == claim.claim_id), None)
            ahead = len(waiting) if ahead is None else ahead
            # First come, first served among interactive claims: one that waits longer goes
            # first, so a quiet claim is not starved by smaller ones that keep fitting.
            if (not batch and ahead > 0) or not fits(claims, claim):
                if not batch and all(w.claim_id != claim.claim_id for w in waiting):
                    self._set_waiting([*waiting, claim])
                return None
            beside = [c.claim_id for c in claims]
            claims.append(claim)
            if any(w.claim_id == claim.claim_id for w in waiting):
                self._set_waiting([w for w in waiting if w.claim_id != claim.claim_id])
            return beside

    def release(self, claim_id: str) -> None:
        with self._locked() as claims:
            claims[:] = [c for c in claims if c.claim_id != claim_id]
            waiting = self._waiting()
            if any(w.claim_id == claim_id for w in waiting):
                self._set_waiting([w for w in waiting if w.claim_id != claim_id])

    def running(self) -> list[Claim]:
        with self._locked() as claims:
            return list(claims)


@contextlib.asynccontextmanager
async def admitted(claim: Claim, ledger: Ledger | None = None) -> AsyncIterator[list[str]]:
    """Wait until the claim fits, hold it for the block, and release it after."""
    ledger = ledger or Ledger()
    while (beside := ledger.try_admit(claim)) is None:
        await asyncio.sleep(POLL_S)
    try:
        yield beside
    finally:
        ledger.release(claim.claim_id)


@contextlib.contextmanager
def held(claim: Claim, ledger: Ledger | None = None) -> Iterator[list[str]]:
    """`admitted` for synchronous code."""
    ledger = ledger or Ledger()
    while (beside := ledger.try_admit(claim)) is None:
        time.sleep(POLL_S)
    try:
        yield beside
    finally:
        ledger.release(claim.claim_id)
