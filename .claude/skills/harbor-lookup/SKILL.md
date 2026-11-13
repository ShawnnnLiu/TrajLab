---
name: harbor-lookup
description: "Answer any question about how Harbor behaves by reading docs/harbor-facts.md and then the pinned Harbor source in .venv, never from memory. Use this whenever the task touches Harbor's trial lifecycle, trial directory layout, ATIF trajectory models, the Claude Code agent (prompt delivery, hooks via --ak config, auth, env vars), the Docker environment (compose project names, teardown, mounts, images), the verifier, or any `harbor` CLI command; whenever you are about to write \"Harbor does X\" or \"Harbor probably X\"; and whenever a Harbor behavior seems to contradict the repo's docs. Also use it before subclassing or calling any harbor.* class."
---

# Looking things up in Harbor

Harbor is pinned (`harbor==0.23.0` in pyproject.toml), never vendored or patched (CLAUDE.md constraint 1).
Every trajlab piece attaches from outside, so correctness depends on knowing exactly what the pinned version does.
Harbor's public docs describe `main`, which drifts from 0.23.0; the installed source is the only authority.
Guessing has cost the repo real time before: an earlier version of the facts file conflated two session ids and the compose project name was wrong until someone read `docker.py`.

## Procedure

1. Read the matching section of `docs/harbor-facts.md` first.
   It is organized by module and marks items re-verified against 0.23.0 with **[0.23.0]**.
   If the fact is there and marked, cite it and move on.
2. If the fact is absent, unmarked, or the behavior looks different from what the file says, open the source.
   The package is at `.venv/lib/python3.12/site-packages/harbor/`.
   Confirm the pin before reading: `uv run python -c "import importlib.metadata as m; print(m.version('harbor'))"`.
3. Read the function, not the docstring.
   Use `grep -n` to find the symbol, then read the surrounding code.
   For behaviors that depend on runtime (Docker labels, compose names, file write order), prefer a probe over inference: the facts file records several probes (`docker ps --filter label=...`, a throwaway compose project).
4. Record what you found.
   If the answer is new or corrects the facts file, add or fix the entry in `docs/harbor-facts.md`, marked **[0.23.0]** with today's date, naming the module and function.
   If Harbor lacks something trajlab needs, write the workaround as a note in `docs/upstream-notes.md` (dated heading, what Harbor does, what we do instead).

## Where things live (0.23.0)

| Question about | Read |
|---|---|
| Trial phases, hook events, what runs when | `trial/trial.py`, `trial/single_step.py`, `trial/hooks.py` |
| Trial dir files, `agent/` `verifier/` `artifacts/` | `models/trial/paths.py` (`TrialPaths`) |
| `result.json`, rewards, token/cost fields, exceptions | `models/trial/result.py` (`TrialResult`) |
| `config.json`, `lock.json` | `models/trial/config.py` |
| ATIF steps, tool calls, observations, metrics | `models/trajectories/*.py`; the RFC is `rfcs/0001-trajectory-format.md` upstream |
| ATIF validation | `utils/trajectory_validator.py` |
| Claude Code install, run command, prompt env var, `--ak config=`, auth, native-to-ATIF conversion | `agents/installed/claude_code.py` |
| Installed-agent base (version pin, `_installed_claude_satisfies_version`, settings upload) | `agents/installed/base.py` |
| Docker compose project name, teardown, bind mounts, `--no-delete` | `environments/docker/docker.py` |
| Environment capabilities, extra mounts | `environments/base.py`, `environments/capabilities.py`, `environments/definition.py` |
| Verifier execution, rewards files, ctrf | `verifier/` |
| Job config JSON shape, `n_attempts`, timeout multiplier | `models/job/`, `job.py`, `job_plan.py` |
| `harbor run`, `harbor view`, `harbor analyze`, `harbor trial regrade` | `cli/` |
| Dataset names and digests | `hub/`, `models/dataset/` |

## Things not to go looking for

The facts file has a "Things Harbor does not have" list: no snapshot or restore in `BaseEnvironment`, no hook during the agent phase, no `context_management` typed field, no tool definitions in the Claude Code ATIF.
If a task seems to need one of these, the answer is a trajlab-side mechanism (the hook channel through `/logs/agent`, `contracts/` models), not a search for a Harbor feature.

## Writing code against Harbor

- Reuse Harbor's models (`Trajectory`, `TrialResult`, `TrialPaths`); never redefine them (constraint 7).
- Subclass, do not copy: `RepairClaudeCode` subclasses `harbor.agents.installed.claude_code.ClaudeCode`; `PreinstalledDockerEnvironment` wraps the Docker environment.
- Paths in the container are `PurePosixPath`, on the host `pathlib.Path`, as Harbor does.
- When a Harbor private helper (`_format_tool_result`, `_sanitize_docker_compose_project_name`) is the only correct implementation, call it and note the private import in a comment with the facts-file line that justifies it.
