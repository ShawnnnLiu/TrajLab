"""The preregistration (technical spec §5.3): `trajlab exp2 prereg` writes it, `trajlab exp2 run`
runs it.

The file is Markdown for people and one fenced JSON block, the run's `Exp2Settings`, for the
loop: the sample with each exclusion's reason, N, the arms, the primary comparison and its
test, the store's schema version and derived id, the sha256 of the protocol steps and of every
prompt prefix (arm by number of prior attempts; the whole prompt is a prefix and the root
failure's own instruction, whose sha256 is listed per failure), the hooks file, the stop hook
(none, ADR-0022), the query tool's caps, the model and its settings, and the repo sha.

`exp2 run` refuses to start a record-engine attempt unless the file, as it is on disk, is the
version committed at HEAD (`committed`), and refuses any run whose settings the current code
would not reproduce (`drift`): a changed protocol, evidence sentence, schema text, or derived
view changes a hash, and the file has to be written and committed again.
"""

import hashlib
import json
import re
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trajlab.capture.corpus import ManifestError, repo_root, repo_state
from trajlab.capture.repair_launcher import HOOKS
from trajlab.contracts import (
    SCHEMA_VERSION,
    ArmConfig,
    Exclusion,
    Exp2Arm,
    Exp2Settings,
    RootFailure,
)
from trajlab.exp2 import leaks
from trajlab.exp2.arms import arm_config
from trajlab.exp2.prompt import PROTOCOL_STEPS, prefix, render, sha256
from trajlab.store.derive import VIEWS, derived_id
from trajlab.store.describe import describe
from trajlab.store.schema import connect, connect_agent, heldout_path, record_path

PREREG_PATH = Path("docs/experiments/exp2/preregistration.md")
PRIMARY = "record-engine against record-files: repair rate at N = 3, paired by failure"
_SETTINGS_BLOCK = re.compile(r"^## Settings\n.*?^```json\n(.*?)^```$", re.MULTILINE | re.DOTALL)


class PreregError(ValueError):
    """The preregistration cannot be written, read, or run."""


def usable_failures(corpus_store: Path) -> tuple[list[RootFailure], list[Exclusion]]:
    """The store's failed first attempts whose checkpoint chain is complete (design spec §5),
    in trial-name order, and every other failure with the reason it is not usable."""
    with connect(record_path(corpus_store), read_only=True) as con:
        trials = con.execute(
            "SELECT trial_name, trial_id, task, reward, prompt FROM trials "
            "WHERE attempt_idx = 0 AND (reward IS NULL OR reward < 1) ORDER BY trial_name"
        ).fetchall()
    with connect(heldout_path(corpus_store), read_only=True) as con:
        chains = {
            trial_id: (complete, reasons)
            for trial_id, complete, reasons in con.execute(
                "SELECT trial_id, complete, reasons FROM chains"
            ).fetchall()
        }
    usable: list[RootFailure] = []
    excluded: list[Exclusion] = []
    for name, trial_id, task, reward, prompt in trials:
        complete, reasons = chains.get(trial_id, (False, ["no chain report"]))
        if reward is None:
            excluded.append(Exclusion(trial_name=name, reason="no reward: the harness failed"))
        elif not complete:
            excluded.append(Exclusion(trial_name=name, reason="; ".join(reasons)))
        else:
            usable.append(
                RootFailure(
                    trial_name=name, trial_id=trial_id, task=task, instruction_sha256=sha256(prompt)
                )
            )
    return usable, excluded


def instructions(corpus_store: Path, sample: Sequence[RootFailure]) -> dict[str, str]:
    """Each root failure's instruction (`trials.prompt` of attempt 0), by trial name."""
    with connect(record_path(corpus_store), read_only=True) as con:
        found = dict(
            con.execute(
                "SELECT trial_name, prompt FROM trials WHERE attempt_idx = 0 AND trial_name IN "
                f"({', '.join('?' for _ in sample)})",
                [root.trial_name for root in sample],
            ).fetchall()
        )
    missing = [root.trial_name for root in sample if root.trial_name not in found]
    if missing:
        raise PreregError(f"{corpus_store} has no first attempt {', '.join(missing)}")
    return found


def schema_text(store: Path) -> str:
    with connect_agent(store) as con:
        return describe(con)


def prefix_hashes(arms: Sequence[Exp2Arm], budget: int, text: str) -> dict[str, str]:
    return {
        f"{arm}/{k}": sha256(prefix(arm, k, text)) for arm in arms for k in range(1, budget + 1)
    }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def settings(
    *,
    run: str,
    kind: str,
    corpus_job: Path,
    corpus_store: Path,
    corpus_config: Path,
    trials: Sequence[str] | None,
    budget: int,
    arms: Sequence[Exp2Arm],
    test: str,
    query: ArmConfig | None = None,
    repo_dir: Path | None = None,
) -> Exp2Settings:
    """The run's settings. `trials` names the root failures (a dry run's); None takes every
    usable failure. A named trial that is not usable is refused, with its reason."""
    usable, excluded = usable_failures(corpus_store)
    if trials is None:
        sample = usable
    else:
        by_name = {root.trial_name: root for root in usable}
        reasons = {exclusion.trial_name: exclusion.reason for exclusion in excluded}
        unusable = [
            f"{name} ({reasons.get(name, 'not a failed first attempt')})"
            for name in trials
            if name not in by_name
        ]
        if unusable:
            raise PreregError(f"not usable failures: {', '.join(unusable)}")
        sample = [by_name[name] for name in trials]
        excluded = []
    config: dict[str, Any] = json.loads(corpus_config.read_text())
    agent = config["agents"][0]
    kwargs = {k: str(v) for k, v in (agent.get("kwargs") or {}).items() if k != "config"}
    root = repo_root(repo_dir or Path.cwd())
    if "record-engine" in arms and query is None:
        query = arm_config("record-engine")
    return Exp2Settings(
        run=run,
        kind=kind,  # type: ignore[arg-type]
        corpus_job=str(corpus_job),
        corpus_store=str(corpus_store),
        corpus_config=str(corpus_config),
        sample=tuple(sample),
        exclusions=tuple(excluded),
        budget=budget,
        report_at=tuple(range(1, budget + 1)),
        arms=tuple(arms),
        primary=PRIMARY,
        test=test,
        schema_version=SCHEMA_VERSION,
        derived=derived_id(),
        derived_views=tuple(view.name for view in VIEWS),
        protocol_sha256=sha256(PROTOCOL_STEPS),
        prefix_sha256=prefix_hashes(arms, budget, schema_text(corpus_store)),
        hooks_sha256=_file_sha256(root / HOOKS),
        stop_hook=None,
        query=query if "record-engine" in arms else None,
        model=agent["model_name"],
        agent_kwargs=kwargs,
        agent_timeout_multiplier=config.get("agent_timeout_multiplier"),
        repo_sha=repo_state(root).sha,
    )


def prompt_leaks(found: Exp2Settings, needles_of: Any = leaks.needles) -> dict[str, list[str]]:
    """Every rendered prompt of the run checked against its task's tests: root failure to its
    leaks, for those with any. `needles_of(trial_dir)` reads a root failure's needles."""
    text = schema_text(Path(found.corpus_store))
    instruction = instructions(Path(found.corpus_store), found.sample)
    out: dict[str, list[str]] = {}
    for root in found.sample:
        needles = needles_of(Path(found.corpus_job) / root.trial_name)
        prompts = [
            (
                render(prefix(arm, k, text), instruction[root.trial_name]),
                instruction[root.trial_name],
            )
            for arm in found.arms
            for k in range(1, found.budget + 1)
        ]
        hit = leaks.all_leaks(prompts, needles)
        if hit:
            out[root.trial_name] = hit
    return out


def drift(found: Exp2Settings) -> list[str]:
    """What the current code would write differently: empty when it renders the same prompts
    and views as the file fixes."""
    store = Path(found.corpus_store)
    now = {
        "schema_version": SCHEMA_VERSION,
        "derived": derived_id(),
        "derived_views": tuple(view.name for view in VIEWS),
        "protocol_sha256": sha256(PROTOCOL_STEPS),
        "prefix_sha256": prefix_hashes(found.arms, found.budget, schema_text(store)),
        "hooks_sha256": _file_sha256(repo_root(Path.cwd()) / HOOKS),
    }
    problems = [
        f"{key}: the file has {getattr(found, key)!r}, the code {value!r}"
        for key, value in now.items()
        if getattr(found, key) != value
    ]
    current = {root.trial_name: root.instruction_sha256 for root in usable_failures(store)[0]}
    problems += [
        f"{root.trial_name}: not a usable failure of {store} with this instruction"
        for root in found.sample
        if current.get(root.trial_name) != root.instruction_sha256
    ]
    return problems


def render_markdown(found: Exp2Settings, written_at: datetime | None = None) -> str:
    """The preregistration file."""
    written_at = written_at or datetime.now(UTC)
    tasks = sorted({root.task for root in found.sample})
    lines = [
        f"# Exp 2 preregistration: `{found.run}`"
        + (" (dry run)" if found.kind == "dry-run" else ""),
        "",
        f"Written {written_at:%Y-%m-%d %H:%M} UTC by `trajlab exp2 prereg` at `{found.repo_sha}`.",
        "It is committed before the first `record-engine` attempt runs: `trajlab exp2 run` refuses "
        "that arm until this file is the version at HEAD, and refuses any run whose prompts or "
        "derived views the code no longer reproduces (technical spec §5.3, ADR-0022).",
        "The block under Settings is what the loop runs; the prose repeats it.",
        "",
        "## Sample",
        "",
        f"{len(found.sample)} failed first attempts of `{found.corpus_job}`, from {len(tasks)} "
        f"tasks; each roots one chain per arm. Store `{found.corpus_store}`.",
        "",
        "| Root failure | Task | Instruction sha256 |",
        "|---|---|---|",
        *(
            f"| `{r.trial_name}` | `{r.task}` | `{r.instruction_sha256[:16]}` |"
            for r in found.sample
        ),
        "",
    ]
    if found.exclusions:
        lines += ["Excluded:", ""]
        lines += [f"- `{e.trial_name}`: {e.reason}" for e in found.exclusions]
        lines.append("")
    else:
        lines += ["Excluded: none.", ""]
    lines += [
        "## Budget, arms, and comparison",
        "",
        f"- N = {found.budget}, reported at N = {', '.join(map(str, found.report_at))}.",
        f"- Arms: {', '.join(f'`{arm}`' for arm in found.arms)}; a fresh environment and a fresh "
        "Claude Code session for every attempt in every arm; no stop hook.",
        f"- Primary comparison: {found.primary}.",
        f"- Test: {found.test}.",
        "",
        "## Record, prompt, and agent",
        "",
        f"- Store schema {found.schema_version}, derived `{found.derived}`: "
        f"{', '.join(f'`{v}`' for v in found.derived_views)}.",
        f"- Protocol steps sha256 `{found.protocol_sha256}`; hooks file sha256 "
        f"`{found.hooks_sha256}`.",
    ]
    if found.query is not None:
        lines.append(
            f"- Query tool: cap {found.query.query_cap} rows, timeout "
            f"{found.query.query_timeout_s} s, memory {found.query.query_memory_limit}, "
            f"{found.query.query_threads} threads."
        )
    lines += [
        f"- Model `{found.model}`, "
        + ", ".join(f"{k} `{v}`" for k, v in sorted(found.agent_kwargs.items()))
        + f", agent timeout multiplier {found.agent_timeout_multiplier}; config "
        f"`{found.corpus_config}`.",
        "",
        "| Prompt prefix (arm / prior attempts) | sha256 |",
        "|---|---|",
        *(f"| `{key}` | `{value}` |" for key, value in found.prefix_sha256.items()),
        "",
        "## Settings",
        "",
        "```json",
        json.dumps(found.model_dump(mode="json"), indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def parse(path: Path) -> Exp2Settings:
    match = _SETTINGS_BLOCK.search(path.read_text())
    if match is None:
        raise PreregError(f"{path} has no Settings block")
    return Exp2Settings.model_validate_json(match.group(1))


def _git(repo: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def committed(path: Path) -> str | None:
    """The commit that last changed `path`, if the file on disk is the version at HEAD; None
    when it is uncommitted or changed since (technical spec §5.3)."""
    try:
        repo = repo_root(path.resolve().parent)
    except ManifestError:
        return None
    relative = path.resolve().relative_to(repo).as_posix()
    at_head = _git(repo, "rev-parse", f"HEAD:{relative}")
    on_disk = _git(repo, "hash-object", str(path.resolve()))
    if at_head is None or on_disk is None or at_head != on_disk:
        return None
    return _git(repo, "log", "-1", "--format=%H", "--", relative) or None
