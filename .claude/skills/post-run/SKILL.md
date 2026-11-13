---
name: post-run
description: "The checklist after a Harbor job, repair run, or Exp 2 chain finishes in trajlab: postprocess the trials, validate, write or update the corpus manifest, produce the report, record the run in docs/research/ or docs/experiments/exp2/, and update docs/paper-notes.md. Use this whenever a run has just finished, whenever the user says \"the job is done\", \"write up the run\", \"record these numbers\", \"update the paper notes\", or asks what to do with a finished job dir; and before any numbers from a run are quoted in a document or PR. Trial data never goes into git."
---

# After a run finishes

A run is only useful once its numbers are traceable: every figure in the paper must cite a `corpus_id`, every claim in `docs/paper-notes.md` must point at a script and a manifest, and the enriched trajectory must exist before the store can ingest the trial.
Doing these steps in order, in the same PR as the run's config, is what makes a run citable months later.

## 1. Confirm the run is really over

Every trial dir has `result.json` and no container with the job's trial names is running (`docker ps | grep __env-main-1`).
Count infra errors (`exception.txt`) separately from reward-0 trials; they are not failures of the task.
If anything is still running, use the `run-watch` skill instead.

## 2. Postprocess and validate

```
uv run trajlab postprocess corpus/jobs/<job>
uv run trajlab validate corpus/jobs/<job>/<trial>      # spot-check a few, or loop over all
```
`postprocess` writes `agent/trajectory.enriched.json` next to Harbor's file and refuses a trial whose checkpoint records do not all join; a refusal is a finding, not something to work around.
Never rename or rewrite `trajectory.json`, `result.json`, or `lock.json` (CLAUDE.md).
Note trials with `.timeout` files under `agent/checkpoints/`: their chains are incomplete and Exp 2 cannot use them (ADR-0014 item 8).

## 3. Manifest

```
uv run trajlab manifest corpus/jobs/<job> --config configs/harbor/<config>.json
```
The manifest goes in `corpus/manifests/` and is committed; it records the Harbor version, repo sha, config, task list, model, attempts, capture policy, and storage location.
If the run used a changed hook, watcher, backend, or postprocess, it needs a new `corpus_id` and the ADR that justified the change (constraint 5).
Tag the repo sha the manifest records if it is not on `main` yet (`corpus/<name>` tags exist for this).

## 4. Report

For capture runs: `uv run python scripts/2026-10-02_trial_report.py <trial> > report.json` per trial for the timeline and the hook's hold time.
For repair runs: `scripts/2026-10-02_repair_report.py` (own-token cost per repair) and `scripts/2026-10-07_early_stop_table.py`.
For Exp 2: `trajlab exp2 score` once it exists; until then a dated script in `scripts/`.
If a report script is being run for the second time with a new job, that is the signal to promote it to a `cli.py` command (see the `dated-script` skill).
Figures go through the `figures` skill.

## 5. Write the run note

One Markdown file, `docs/research/YYYY-MM-DD_<job-or-topic>.md` (Exp 2 runs go under `docs/experiments/exp2/`).
It is facts, not interpretation; interpretation gets its own "findings" note, as `2026-10-09_tb40-repair-v2-findings.md` does.
Contents, in this order:

- the exact command(s) and the config file, the repo sha, the `corpus_id`;
- the machine (Mac under Rosetta vs the Linux server) and Docker version, since timings differ;
- counts: trials, pass/fail, infra errors, early stops, excluded tasks and why;
- per-trial table when there are few trials, otherwise the aggregate and a pointer to the report JSON;
- anything surprising, with the evidence file named;
- what the next run should change.

One sentence per line.
Numbers come from the report script's output, pasted, not retyped.

## 6. Update the paper notes

`docs/paper-notes.md` holds claims C1..C7 with their evidence, reproducibility facts, threats to validity, and open work.
For every claim the run touches, add a dated evidence line under that claim pointing at the run note, the script, and the `corpus_id`.
If the run exposes a threat (a background write, a platform difference, a setup difference from a published number), add it under "Threats to validity".
If the run closes an item under "Open work", mark it done with the date; do not delete it.
Superseded entries are marked, never removed, so the paper's history stays readable.

## 7. Commit

- Committed: manifest, run note, paper-notes change, any report JSON that is small and summary-level, figures under `docs/research/figures/`.
- Never committed: `corpus/jobs/`, session JSONLs, `.env`, checkpoint images, anything under `corpus/exports/`.
- Run `make test` and `make lint` before the PR; one PR per run, branch `<initials>/<topic>`, imperative one-line subject naming the job (`Record the traj-text arm of repair round 1 (tb40-repair-v2)`).
