# ADR-0022: Checker v0, the rule-based detector of ADR-0015, and its shadow run

Status: proposed (2026-11-03).
Narrows ADR-0015 §1 and §3 to five checks, fixed on the 40-trial `tb40-detector-off-v1` corpus.
The number 0022 is also used on the unmerged `sl/exp2-retry-loop` branch (`0022-exp2-retry-loop-as-built.md`); whichever merges second renumbers.

## TL;DR

- **What:** `trajlab.checkpoint.patterns.Checker`, a deterministic checker with five checks: `self_kill`, `silent_no_write` and `masked_error` at each hooked call, and `unverified_completion` and `missing_output` at the stop. No model calls.
- **Inputs:** only what the watcher holds when a call's hook fires: the hook input (`.call.json`), the `CallRecord`, the extras (`CallChanges`, `ProcessSnapshot`), and the checker's own memory of earlier calls. The stop also reads the instruction and two short read-only `docker run` probes.
- **Prefix rule:** the flags for the first N calls are the same whether the checker stops at N or reads the whole trial. A watcher restarted mid-trial rebuilds the checker from the records first.
- **Live:** `trajlab watch --extras --detect`. It runs after the call's `CallRecord` is appended and before its ack, and writes `<trial>/trajlab/flags.jsonl` and `checks.jsonl`. Shadow mode: nothing reaches the agent, and a failing check never holds the ack.
- **Offline:** `trajlab patterns <job-dir> --out <dir>` replays finished trials through the same code.
- **Shadow run:** `configs/harbor/tb40-detector-shadow-v1.json`, 2 trials (`vba-userform-port` and `layout-config-recreation2`, one attempt each), with the off corpus's agent and settings.

## Context

- ADR-0015 §1 lists seven patterns, and §3 a detector that runs in the watcher.
- The off corpus (40 trials: 11 passed, 29 failed) was captured with `--extras` and hand-labeled (ADR-0015 §8).
- Amina-agent's reference labels give 16 self-kill calls and 104 claimed writes.
- Every check here was replayed on those 40 trials before it ran live. The replay report, with every number below, is kept outside the repo (`~/checker-v0/report.md` on the trajlab server), because it quotes trial data.

## Decision

### 1. Checks

| Check | Fires when | Message |
|---|---|---|
| `self_kill` | `pkill -f`, `pgrep -f`, or `kill $(pgrep -f ...)` whose pattern matches the call's own command text (read as a Python regex), on a call that exited 143 or 144. `-x` and the bracket trick (`[u]vicorn`) never match. | "Your `pkill -f <pattern>` also matched this command's own shell, so the command was killed and nothing after it ran." |
| `silent_no_write` | A write target leaves its path unchanged on a call that exited 0, with a complete record. Targets come from Write/Edit/NotebookEdit, or from a shell redirect, `tee`, `sed -i`, `cp` or `mv`. | "`<path>` didn't change on this call, although the command looked like it wrote it." |
| `masked_error` | A Bash call exits 0, but its output shows a crash. | "This command exited 0, but its output shows an error: <first error line>." |
| `unverified_completion` | At the stop: no check ran after the last file-changing call (`no_test`), or the last check failed (`last_test_failed`). | "You're stopping, but no test ran after your last change (<path>, call <n>)." / "You're stopping, but your last test (call <n>) failed." |
| `missing_output` | At the stop: a path the instruction names, absent from the start image, is absent from the stop's checkpoint. | "The task asks for `<path>`, and it doesn't exist." |

**`silent_no_write`**
- The command is parsed by `checkpoint/shell.py`, a standard-library parser. It never reads text in heredoc bodies, `python -c` strings or command substitutions as a path. `cd` is tracked per subshell.
- A target with an expansion, a glob or `~` is never guessed; it is recorded as unknown.
- The following are not flagged:
  - identical rewrites;
  - targets a later `rm` or `mv` in the same command takes away;
  - excluded paths (ADR-0010);
  - incomplete records (baseline, capped);
  - a background sign with a new agent process: the write is recorded as held;
  - visible non-zero exits.
- Each of these is recorded in the call's `CheckRecord`, as a note, an unknown or a held write.

**`masked_error`**
- The crash markers are:
  - a Python traceback, unless its only exception is `BrokenPipeError`;
  - a `SyntaxError`/`IndentationError`/`TabError` line;
  - `can't open file ... [Errno 2]`;
  - `Segmentation fault` or `core dumped`;
  - `npm ERR!`;
  - a compiler's `file:line:col: error:`;
  - `make: ***`.
- `command not found` and `No such file or directory` are left out, because the agent is mostly probing.
- **Import probes are skipped.** A probe is a `python -c` whose code only imports modules and prints names, attributes or literals, such as `python -c "import cadquery; print(cadquery.__version__)"`. If it fails with `ImportError` or `ModuleNotFoundError` naming a module it imports, that line is the answer the agent asked for. Another crash in the same output is still flagged.

**`unverified_completion`: what counts as a check**
- The shared test-command list, `contracts.TEST_COMMAND_PATTERN`, unchanged.
- A command the instruction shows: a backticked span, or a `$ ` line, whose program is a build or test runner, a shell or an interpreter. It must be the same program in the same `-C` directory, with all its operands, for example `make -C /app repro`.
- A provided script: an absolute path the instruction names that exists in the start image and has a script suffix. Running or importing it counts, for example `/app/render.py`.
- These three count from the last change's own call on.
- The task-agnostic rule (`uses_changed_file`) counts only a later call. That call must run a file the last change touched, either as the program or as an operand of an interpreter, shell or comparer (`diff`, `cmp`, `compare`). Interpreter code that names the file or imports it as a Python module also counts. `cat`, `ls`, `grep` and editors do not.
- A call whose only changes are bytecode or test caches is not a change.
- If a task has no instruction check and the task-agnostic rule is off, the stop is recorded as `no check defined` and not flagged, so the flag tracks the agent rather than the task. The shared list alone does not define a check: the off runs never use it.
- Checks learned from the off runs' agents were dropped, because they fit the rule to the data it is tested on: `test*.py`, `playwright`, `curl` to localhost, and the agents' own `score.sh`.

**Output paths** are absolute paths in the instruction, minus directories and URL paths, that are absent from the start image. Live, the instruction comes from the trial's `config.json` and Harbor's cached task (`harbor.models.task.task.Task.instruction`). On the off corpus it equals the trajectory's first user step in 40 of 40 trials.

### 2. Records

- **Models.** In `contracts/patterns.py`:
  - `FlagRecord`: `tool_call_id`, `call_seq`, `event`, `pattern`, `subtype`, `evidence`, `message`, `detector_version`, `flagged_at`.
  - `CheckRecord`: one per answered call, with `check_ms`, `held`, `unknown` and `notes`.
  - `detector_version` is `checker-v0`.
- **Files.** `<trial>/trajlab/flags.jsonl` and `checks.jsonl`, next to ADR-0015's extras. They are outside `/logs/agent`, so the agent cannot read them.
- **Order.** The checker runs in `Watcher.answer()` between `append_call` and `write_ack`. It needs the `CallRecord`, and the agent is still waiting for its ack.
  - Extras are passed to the checker only once they are written, so the checker reads exactly what a replay of the records reads.
  - Any exception is logged ("no check for ...") and the ack is written as usual.
- **Stop probes.** Each is a `docker run --rm --network none` with a 15 s timeout: one on the start image, one on the stop's checkpoint image.
- **Contracts constraint.** `contracts/` may import only typing, uuid, datetime and pydantic. So the shared regexes `TEST_COMMAND_PATTERN` and `HARNESS_PROCESS_PATTERN` live there as strings, and are compiled where they are used.

### 3. Out of v0's scope

| Pattern (ADR-0015 §1) | Why not in v0 |
|---|---|
| `loop`, `stall` | The off corpus's six repeated-command runs were deliberate undos. |
| `false_edit_claim` | Only 2 of the 104 missing writes were claims in the agent's own words. |
| `false_test_claim` | Covered for now by `unverified_completion`. |
| `server_state` | Agents kill and restart their own servers on purpose, so it needs labels first. |
| `forbidden_action` | Test edits are sometimes asked for, which needs task-specific rules. |

- **Writes made inside a program.** 83 of the 104 reference write cases are outside `silent_no_write`'s definition. Examples are a heredoc script's `json.dump(open(..., "w"))`, `render.py ... out.png` and `gcc -o`.
  - Reading what a program writes means guessing.
  - Most of these cases were harmless: the agent saw the error or redid the write.
  - No script parsing is planned for v1.
- **Late writes.** ADR-0020's attribution of late writes uses later calls, so a live checker cannot use it.

### 4. Results of the replay on the off corpus

| Check | Flags | Runs | In passed runs | Against the reference |
|---|---|---|---|---|
| `self_kill` | 16 | 10 | 9 | 16 of 16 matched on the same call; 0 missed, 0 new |
| `silent_no_write` | 2 | 2 | 0 | both match a reference case on its expected call |
| `masked_error` | 21 (40 before the import-probe rule) | 12 | 4 | no reference list |
| `unverified_completion` | 23 | 23 | 3 | no reference list |
| `missing_output` | 0 | 0 | 0 | every output exists at every stop |

**`unverified_completion` per task.** "Flagged" counts stops, out of 5 runs per task; the task-agnostic rule is on.

| Task | Instruction check | Flagged |
|---|---|---|
| cad-model, vba-userform-port | none | 5 each |
| vf2-speedup-networkx | none | 4 |
| layout-config-recreation | `/app/render.py` | 3 |
| layout-config-recreation2 | `/app/render.py` | 1 |
| pretrain-shard-corruption | `bash /app/run_pretrain.sh` | 4 |
| sound-change-cascade | `/app/engine/apply.py` | 1 |
| mvcc-lsm-compaction | `make -C /app test`, `make -C /app repro` | 0 |

- With the task-agnostic rule off, the 15 stops of cad, vba and vf2 are `no check defined`, and 9 stops are flagged.
- The rule recognized one check across those three tasks. 13 of the 23 flagged stops wrote and ran in one call, which the rule does not count by design. In vba, the last change is a background server's log or a cleanup `rm` of the database.
- With the rule on, the flag tracks those three tasks, not the agent. Whether to keep it on is open (below).

**Speed and the prefix rule.**
- A call check takes 0.21 ms median and 7.6 ms at most.
- A stop takes about 280 ms, almost all of it the two Docker probes.
- The 40-trial replay takes 12 s.
- The prefix rule held for 120 of 120 random cuts (seed 6113).
- The watcher's tests check that live flags equal a replay of the same records across a watcher restart (FakeBackend).

### 5. Shadow run

- **Config.** `configs/harbor/tb40-detector-shadow-v1.json`: the off corpus's agent, version, settings and environment.
- **Tasks.**
  - `vba-userform-port`.
  - `layout-config-recreation2`. It ties with vba for the most flags per run in the replay above (3.0).
- **Size.** One attempt each, so 2 trials.
- **Commands.** The watcher runs for the whole job:

```
uv run trajlab watch corpus/jobs --every 1 --gate change --extras --detect
uv run trajlab run configs/harbor/tb40-detector-shadow-v1.json --corpus-id tb40-detector-shadow-v1 --storage /srv/trajlab/jobs
```

- **What a trial records.** It changes only by the two new files under `<trial>/trajlab/`, which is why it gets a new corpus_id.
- **Acceptance.**
  - Every answered call has one `CheckRecord`.
  - No `.timeout`.
  - `trajlab patterns` on the finished job gives the same flags as the live files, ignoring timestamps.

## Open questions

- **The task-agnostic rule.** Keep it, which flags cad, vba and vf2 almost always? Or turn it off, which records `no check defined` for those tasks?
- **Write-and-run.** Should the task-agnostic rule count a later command in the same call, such as `cat > m.py ...; python3 m.py`? v0 counts only later calls, as the rule was specified.
- **Background writes.** Should changes written by a background process, such as a server's log, count as the agent's last change?
- **Import probes beyond `-c`.** Should heredoc probes (`python3 - <<EOF` with only imports and prints) also be skipped? v0 skips only `-c` one-liners.

## Consequences

- `checkpoint/` gains `patterns.py` and `shell.py`. `contracts/` gains the flag and check models.
- The watcher gains `--detect`, which needs `--extras` and a gate.
- A trial run with `--detect` has two more files under `<trial>/trajlab/`, and nothing else changes.
