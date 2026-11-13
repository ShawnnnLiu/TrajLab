---
name: review-pr
description: "Review a trajlab branch or PR before it lands on main, with the repo's own constraints layered on top of the built-in code-review skill. Use this whenever the user says \"review this\", \"ready for PR?\", \"check my branch\", \"review PR #N\", or asks you to look over changes before merging; whenever you are about to open a PR yourself; and whenever a stacked PR is involved, because this repo has merged stacked PRs into the wrong base before. Runs the built-in code-review and then the trajlab-specific checks below."
---

# Reviewing a trajlab change

The built-in `code-review` skill finds correctness bugs.
This repo also has seven non-negotiable constraints in CLAUDE.md, an ADR rule, offline-only tests, and a PR process with one reviewer, and a review that misses those costs more than a missed bug: a trial captured under an undocumented change cannot be cited, and a PR merged into the wrong base silently drops work from `main` (PRs #16 to #19 did exactly that on 2026-10-02).

## 1. Establish the target

```
git branch --show-current
git fetch origin main --quiet && git rev-list --count origin/main..HEAD
gh pr view --json baseRefName,headRefName,title,url 2>/dev/null
```
The base must be `main`.
If the PR's base is another feature branch, say so first: merging it there will show MERGED on GitHub while `main` never gets the commits.
Retarget to `main` before the lower PR merges, or merge bottom-up with each PR retargeted after the one below lands.

## 2. Run the built-in review

Invoke `code-review` with the user's effort level (or `medium` when none was given) on the branch or PR number.
Pass `--fix` only if the user asked for fixes to be applied.
Keep its findings; the checks below are additions, not replacements.

## 3. trajlab checks

Walk the diff against each item and report only violations, with file and line.

**Constraints (CLAUDE.md)**
- Harbor unpatched: no edits under `.venv/`, no copied Harbor code; new behavior attaches via `--env`, an agent subclass, `--ak config=`, or postprocessing. A needed Harbor change is a note in `docs/upstream-notes.md`.
- `contracts/` holds pydantic models only: no I/O, no imports from other trajlab packages.
- `capture/`, `checkpoint/`, `atif/` import each other only through `contracts/`; `store/` imports only `contracts/`, `atif/`, Harbor, and DuckDB. Grep the new imports.
- No trial data: nothing from `corpus/jobs/`, no `*.jsonl` outside `tests/fixtures/` and `corpus/manifests/`, no `.env`, no notebook outputs.
- Recording changes carry an ADR: any change to the hook JSON, watcher, `docker_commit` backend, `postprocess` output, store `SCHEMA_VERSION`, derived columns, or the query tool needs a file in `docs/decisions/` in this PR and a new `corpus_id` on the next run.
- No analysis beyond `store/`: no `analysis/`, `bench/`, no LLM judge, no database outside `store/`.
- Harbor models reused, not redefined: `Trajectory`, `TrialResult`, `TrialPaths`.

**Tests**
- Nothing in `tests/` calls Docker, the network, or an API; nothing runs `harbor run`. Grep for `subprocess`, `docker`, `httpx`, `requests`, `anthropic` in the test diff.
- New behavior has tests against `tests/fixtures/hello-world-trial/` or synthetic data built in `conftest.py`; a Docker-touching check is a dated script, documented as manual.
- `make test` and `make lint` are green locally (run them if the branch is checked out).

**Code conventions**
- `logging`, never `print`, in `src/`; `typer` for CLI; pydantic v2; `pathlib.Path` on the host, `PurePosixPath` in the container.
- Type hints everywhere; line length 100; no `from __future__ import annotations`.
- Harbor's trial files are never renamed or rewritten; new files sit next to them.

**Docs that must move with the code**
- `docs/glossary.md` for a new or changed term; `docs/harbor-facts.md` for a newly verified Harbor behavior; `docs/paper-notes.md` when a number or threat changes; `CLAUDE.md`'s layout table for a new package or CLI command.
- Markdown body text: one sentence per line.

**Commit and PR hygiene**
- Subject imperative and one line, optional body; the ADR number in the message when there is one.
- No `Co-Authored-By` lines naming an assistant (the owner's rule).
- Branch name `<initials>/<topic>`; one reviewer; PR description names the acceptance check that was run and its result.

## 4. Report

Findings first, ordered by severity, each with file:line and what breaks.
Then the base-branch verdict and the test/lint status.
Say plainly when nothing was found in a category; do not pad.
