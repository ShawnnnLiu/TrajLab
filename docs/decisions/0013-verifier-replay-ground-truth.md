# ADR-0013: Ground truth for error localization by verifier replay

Status: proposed (2026-10-07).

## Context

The capture phase is done (build-order steps 1-7) and Experiment 1 (ADR-0012) has finished.
The course brief asks for a benchmark of analysis questions and a system that answers them with less effort than ad hoc workflows while preserving accuracy and provenance (`docs/project-brief.md`).
Scoring an analyst's answer to "where and when did this trial fail" needs ground truth.
Labels from human annotators are slow and vary with the annotator; a survey on 2026-10-07 found no public dataset with step-level failure labels and environment state for Claude Code on Terminal-Bench ("Failure as a Process", arXiv 2607.09510, labels TB 2.0 runs of other scaffolds and ships no trajectories; TrajErrBench, Who&When and TracerTraj are outside the terminal setting).

Three facts make a mechanical ground truth possible on `tb40-sonnet-v2`:

1. **The grade is a function of files the checkpoints contain.** All 23 TB 4.0 tasks of the corpus run their verifier in a separate environment (`[verifier] environment_mode = "separate"`): Harbor collects the task's declared artifacts (1 to 26 paths) from the agent's container and uploads them into a fresh verifier container (`harbor/trial/trial.py`, `_run_separate_verifier`).
   The convention artifacts dir `/logs/artifacts` is a bind mount and therefore absent from `docker commit` images, but it is empty in all 69 trials' `artifacts/manifest.json`.
   So every checkpoint holds everything the verifier would have seen had the trial ended there.
2. **Harbor already replays a verifier.** `harbor trial regrade` (`harbor/trial/regrade.py`, `RegradeTrial`) copies a trial's recorded `artifacts/` into a new trial dir and reruns the task's separate verifier with Harbor's own environment, network policy, user, and timeout; it never modifies the source trial.
   On 2026-10-07 it regraded `bun-sourcemap-leak__7vKyX9J` in 20 s and reproduced all 36 check statuses (27 passed, 9 failed).
3. **The checkpoints are still there.** The corpus has 69 first attempts: 56 failed with 703 checkpoints, 13 passed with 214; every checkpoint image and every trial's starting (derived) image is present.
   The 56 failed trials fail 202 checks in their recorded verifier output (median 2 per trial, maximum 10).
   Repair trials kept only their final checkpoint (ADR-0012 amendment) and are out of scope.

## Decision

### 1. Scope

- The analysis phase opens with one package, `src/trajlab/groundtruth/`, which produces ground truth and nothing else (no store, no query layer, no analyst).
  It imports only `trajlab.contracts` and Harbor; its models live in `contracts/groundtruth.py`.
  CLAUDE.md constraint 6 is amended accordingly.
- Input: the 69 first attempts of `tb40-sonnet-v2`. Failed trials get localization items; passed trials get first-pass times.
- Output: a new directory `groundtruth/` inside each trial dir, next to Harbor's files, which are never modified; a dataset manifest `corpus/manifests/tb40-sonnet-v2-gt-v1.json`.

### 2. Timeline points and artifact states

- A trial's **timeline** is its ordered **points**: `initial` (the derived image the trial started from, `trajlab-preinstall.json` → `image_id`), one point per checkpoint in `seq` order, and `final` (Harbor's recorded `artifacts/`, collected after the agent ended).
- At each point the task's declared artifacts are extracted the way Harbor collects them (`harbor/trial/artifact_handler.py`): files and directories without exclusions with `docker cp` from a created, never-started container; directories with exclusions with the same `tar czf - --exclude=... -C <dir> .` Harbor runs, in a throwaway `--network none` container of the image, extracted with the `data` filter.
  An artifact absent at a point is recorded with status `failed`, as Harbor records a failed collection.
- An **artifact state** is the extracted tree plus its manifest; its `state_id` is the sha256 of a canonical listing (entry types and statuses; per file: path, sha256, size, executable bit; per symlink: target).
  Points with the same `state_id` are replayed once.
- **Fidelity gate 1 (no verifier):** the last checkpoint's state must equal the `final` state.
  A mismatch means writes after the last checkpoint or an extraction difference; it is listed, and `final` stays a timeline point of its own, attributed to no tool call.

### 3. Replay

- `ReplayTrial` subclasses Harbor's `RegradeTrial` and changes one thing: artifacts recorded as `failed` (absent at that point) are accepted instead of refused; Harbor's uploader already skips absent paths, exactly as in a live trial whose agent never wrote the file.
- The verifier comes from the task package pinned by digest in the trial's `config.json`/`lock.json`; the verifier environment is Harbor's default Docker environment, which is what `PreinstalledDockerEnvironment` starts for separate verifiers (`is_agent_environment` is false for them).
- Each replay writes Harbor's regrade trial dir under `groundtruth/replays/<replay_id>/` and appends one `ReplayRecord` to `groundtruth/replays.jsonl`, with per-check results parsed from `ctrf.json` (pytest), `trace_results.json` (vba-userform-port), and `reward_details.json` (freecad and cad-model); the parser moves from `scripts/2026-10-02_repair_report.py` into the package.
- **Fidelity gate 2:** the replay of a trial's `final` state must reproduce the recorded reward and every recorded check status. A trial that fails it contributes no items and is listed with the differences.
- **Flakiness:** the `final` state is replayed twice (three samples with the original run); a check whose status differs across samples is `flaky` in that trial and contributes no item.
- At most 4 verifier environments run at once across all processes (`flock` slots under `corpus/jobs/_groundtruth/slots/`), within the server's 8 cores and 30 GB.
- **Order:** extract every point's state; replay every trial's `final` state (gate 2); then replay every distinct state of every trial if the estimate at 4 slots is at most 6 hours, otherwise sample whole trials per task and record which were left out.

### 4. Mechanical items (no LLM)

For every check that fails in the `final` state, its status across the timeline decides:

- **`regression`**: it passes at point k-1 and fails at every point from k to `final`.
  Both states are replayed twice more; if any of the six runs disagrees, the check is `flaky` instead.
  The blamed calls are checkpoint k's `covered_tool_call_ids`; a change of the failure message after k is flagged, since the cause may have changed while the status did not.
- Otherwise (it never passes, or it passes only before a later failure that is not stable) it goes to counterfactual labeling (decision 5).
- For passed trials, each check's **first pass** is the first point from which it passes through `final`.

### 5. Counterfactual items (Claude Code proposes, the verifier decides)

- A Claude Code labeler (Opus 5.5, on the host, outside any task container) works one failed trial at a time.
  It reads the task instruction, the trajectory, the timeline, every state's files, the verifier output, the task's tests and its reference solution, and it has one tool that changes anything: `trajlab gt try-fix`, which applies a unified diff to a state's artifacts, replays the result as a new state, and prints which checks changed status.
  Every try is recorded as a `counterfactual` replay.
- A label is **accepted** only if its fix replay turns the checks it claims from failed to passed and turns no passing check to failed.
  Nothing the labeler writes is ground truth until that replay exists.
- **Blame** is then mechanical: each file the fix changes is traced through its versions at every timeline point by successive line diffs; the lines the fix removes or replaces get the point that introduced them.
  - **`wrong_edit`**: the lines were introduced by checkpoint k; the blamed calls are k's covered calls.
  - **`missed_fix`**: the lines were already in the `initial` state; the agent never corrected pre-existing code. No call is blamed; the item names file and lines.
  - **`omission`**: the fix only adds lines; the item names file and insertion point, and the blame of the neighbouring line is kept as context only.
  - **`missing_artifact`**: the fix creates an artifact absent at `final`.
  - **`unconfirmed`**: no accepted fix; the labeler's explanation is kept for human review.
- The size of each fix (lines added plus removed) is recorded; a fix that rewrites most of a file localizes little, and scoring can filter on size.
- Regressions are labeled too; when the fix's blame and the transition disagree, the item is flagged `disagreement`.
- A second Claude Code agent per accepted item tries to refute it (the fix copies the reference solution wholesale; a smaller fix elsewhere passes; the blamed call did not write those lines). A refutation flags the item; it does not delete it.

### 6. Human review

A reviewer checks every `unconfirmed`, `disagreement`, and flagged item and a random 20% of the rest; a second reviewer repeats a subset to measure agreement. The decision is stored on the item (`review`). This is label checking, not timing.

### 7. Scoring for the later analysis benchmark (fixed now, implemented with the analyst arms)

An analyst's answer cites tool call ids, files and lines, and checkpoints. Against a trial's items:

- **Localization:** exact match of a blamed `tool_call_id`; tolerant match if the cited call is in the same checkpoint's covered calls or within 2 agent steps. Items without blamed calls (`missed_fix`, `omission`, `missing_artifact`) are scored on the file and lines only.
- **Root cause:** same file and overlapping lines.
- **Provenance:** every cited id exists in the trial and every quoted content occurs at the cited point.
- **Coverage:** recall over the trial's items; precision over the answer's claims (a claim matching no item is a false accusation).
- Only accepted and human-confirmed items are scored; the rest are reported as counts.

## Consequences

- Compute: about 1,000 timeline points before deduplication; regrade took 20 s on a fast task and the recorded verifier phases take 15 s to 10 min (mvcc-lsm-compaction). Disk: states are at most about 20 MB each.
- The ground truth covers what the verifier grades: the root cause is located in the artifact bytes. For a generated output (a results file, model weights) the blamed call is the one that wrote the bytes, not the earlier edit to the code that generated them; binary artifacts cannot be fixed by a diff and end `unconfirmed`.
- Brief question classes 2 (divergence and failure location), 4 (consequential environment changes: which calls changed graded files) and 5 (how early success or failure shows) get ground truth; classes 1 and 3 need a different source.
- The labeler sees what no analyst arm will see (reference solution, verifier messages, counterfactual replays); the verifier, not the labeler, is the ground truth, so the benchmark is not graded by the system it tests.
- Starting from the derived image assumes the agent install does not touch artifact paths; the `initial` replay of each trial shows which checks the untouched task already passes.
- Nothing in capture changes; no new capture `corpus_id`. The ground-truth dataset gets its own id, `tb40-sonnet-v2-gt-v1`.
