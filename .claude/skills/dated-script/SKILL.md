---
name: dated-script
description: "Create a one-off script under scripts/ in trajlab with the repo's dated naming, docstring, and tooling conventions, or decide that the task belongs in cli.py instead. Use this whenever you are about to write a script that reads corpus/jobs, talks to Docker, checks a hypothesis against real trials, produces a report or figure, or does any manual integration check; whenever the user says \"write a quick script\", \"check this against the corpus\", \"one-off\", or \"just a script for now\"; and whenever you notice an existing dated script being run a second time on new data, which is the signal to promote it."
---

# Dated scripts in trajlab

CLAUDE.md draws one line: a file is either a module in `src/` with tests, or a dated script in `scripts/`.
Scripts are how this repo does anything that needs Docker, the corpus, or an API key, because tests are offline and `src/` never shells out to `harbor run`.
The date in the name is the provenance: ADRs and run notes cite scripts by that name, so a script is never renamed or rewritten in place after it has produced a cited number.

## Decide first: script or command

Write a script when the task is a check, a measurement, or a report run against data that exists once.
Promote to a `cli.py` command when the same script is about to run a second time on a new job: the rule is literal ("if a script is run twice, promote it").
`trajlab repair` is the precedent; it started as `scripts/2026-10-02_repair_arms.py`.
Promotion means a typer command, a module under the right package, tests on the fixture, and the script left in place with a one-line note at the top of its docstring pointing at the command.

## Naming and location

`scripts/YYYY-MM-DD_<topic>.py` (or `.sh` for a shell check), today's date, lowercase, hyphens inside the topic.
Never imported by `src/`; never imports from another script.
It may import `trajlab.*` and `harbor.*` freely.

## Shape

```python
"""One-line purpose, as a sentence.

What it reads (which files of a trial dir, after which command), what it writes or prints, and
what each output field means. Then the exact invocation:

    uv run python scripts/2026-10-18_example.py corpus/jobs/<job> --out docs/research/...
"""
```

- Run with `uv run python scripts/...` when it needs only the project's dependencies.
  Use the inline-metadata header (`# /// script` with `dependencies = [...]`) and `uv run --script` when it needs something the project does not depend on, such as matplotlib or scipy; `2026-10-09_repair_figures.py` is the template.
  Never `pip install`.
- `argparse` for arguments; `logging` for progress; `print` only for the result itself (JSON to stdout is the usual contract, so the output can be redirected and cited).
- Type hints, `pathlib.Path`, pydantic models from `trajlab.contracts` and Harbor (`TrialResult`, `Trajectory`) to read trial files; never hand-parse `result.json`.
- Deterministic where randomness exists: fixed seeds, stated in the docstring, so a figure or a sample draw can be regenerated.
- Read-only against `corpus/jobs/` unless the docstring says what it writes, and then write beside Harbor's files, never over them.
- A Docker-touching check records its exact command and the environment it ran on in the ADR or run note that cites it, as `2026-10-18_reconcile_docker_check.py` does in ADR-0014's Validation section.

## Lint and tests

`make lint` covers `scripts/` (ruff check and format, line length 100), so the script must pass both before commit.
Scripts have no tests; if a function in a script is worth testing, that is the sign it belongs in `src/`.

## Example

Input: "count how many of the 56 failed first attempts in tb40-sonnet-v2 have a complete checkpoint chain".
Output: `scripts/2026-10-19_complete_chains.py`, reading each trial's `agent/checkpoints/{calls,checkpoints}.jsonl` and `.timeout` files through `trajlab.contracts`, printing a JSON object with the count and per-trial exclusion reasons, with the invocation in its docstring and its result pasted into `docs/experiments/exp2/sample.md`.
The second time the count is needed (after the next corpus), it becomes `trajlab store chains` or a flag on `trajlab store build`.
