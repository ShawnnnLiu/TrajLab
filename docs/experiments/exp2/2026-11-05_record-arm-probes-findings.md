# Does the record help a retry? Findings of the Exp 2 probes (2026-11-02 to 2026-11-05)

A summary and reading of the Exp 2 probes run on the development Mac between the `tb40-exp2` interim result and the pilot preregistration.
Unlike the run note, this document interprets.
Where each mvcc attempt succeeded or stopped, attempt by attempt, is in `2026-11-05_repair-anatomy.md`.
The facts it rests on, with commands, per-attempt tables, and report files, are in `2026-11-02_engine-uptake-probe.md`.
The decisions the probes led to are ADR-0023 and its amendment of 2026-11-04.

## Why the probes ran

The full run `tb40-exp2` stopped at 102 of 168 chains (`tb40-exp2-interim-2026-10-30.md`).
By N = 2 control had repaired 8 of 56 failures, record-files 12, and record-engine 9, so the engine showed no gain.
Prof. Kaffes's advice: before concluding that the engine does not help, make sure the agents are encouraged to use it and know how.
The probes rerun retry attempts (k = 1, one earlier attempt) on local roots and read what the agents do with the record.

## What ran

| Date (UTC) | Run | Roots | Arms and conditions | Attempts | Passed | Credential |
|---|---|---|---|---|---|---|
| 2026-11-02 | First probe: six prompt variants | production-planning `PckZuoA`, mvcc `CsiwPnL` | both record arms | 13 | pp 0 of 6, mvcc 2 of 7 | subscription |
| 2026-11-03 | Whole files beside the diffs (`v2`) vs none (`v2-nofile`) | mvcc `CsiwPnL` | both record arms | 20 | 5 of 10 and 5 of 10 | subscription |
| 2026-11-04 | Control | mvcc `CsiwPnL` | control | 10 | 3 of 10 | subscription |
| 2026-11-05 | Whole-file hunks (`v3`) vs 3-line hunks (`v3-ctx3`) | mvcc `CsiwPnL`, pp `PckZuoA` | both record arms | 32 | mvcc 4 of 10 and 3 of 10; pp 0 of 6 and 0 of 6 | API key, no output cap |
| 2026-11-05 | Check of the `v4` text | the three mvcc roots | record-files | 5 | 1 of 5 | subscription |
| 2026-11-05 | Wider run (`v4`) | the three mvcc roots | control, record-engine, record-files | 60 | 2, 6, and 6 of 20 | subscription |
| 2026-11-05 | Wider run, vigenere (stopped) | vigenere `wWt6sCe` | control, record-engine | 2 | engine 1 of 1; control refused | subscription |

Settings throughout: Claude Sonnet 5.5 at medium effort, Claude Code 2.1.278, amd64 under emulation on an arm64 Mac, no write recorder.
New roots came from 7 first attempts on cad-model, layout-config-recreation2, and interleaved-vigenere (run note, "New roots").
Cost, as Claude Code reports it: $11.87, $2.79, $1.18, $40.05, and $8.20 for the runs that report a total; only the $40.05 was billed per token, the rest counted against the subscription's usage.

## Findings

**Main finding.**
On mvcc, both record arms passed three times as often as control in the one comparison run on the same day with the same setup: 6 of 20 each, against 2 of 20.
At this size the difference is not significant (Fisher exact, p = 0.235 for each record arm; three arms at once, p = 0.256).
Over three days, control has passed 5 of 30 and the record arms 29 of 80 under three texts (p = 0.064), but the days, texts, and one run's credential differ, so the pooled figure is a lead only.
Record-engine and record-files did not differ (6 of 20 each).
Production-planning failed all 18 record-arm retries; no control ran on it.

**The agents use the engine and know how.**
Every engine attempt queried in its first or second call, and none of the first probe's 33 queries failed.
Query counts stay at 2 to 5 whatever the prompt says, and on these short records two or three queries return the whole record.
So the interim result is not explained by agents failing to use the engine.

**The full run's prompt tilted against the engine arm, and ADR-0023 fixed that.**
The old protocol said to diagnose "from the files in the environment", which is the record only for record-files.
The engine prompt also carried a 14 KB schema text inline.
ADR-0023 gives both record arms the same text except the medium: a ready query in record-engine, a file pointer in record-files.

**Whole files reach agents only inside the diffs they already read.**
- Offered in a separate place (`text_after`, the bundle's `versions/`), whole files were read by 1 of 10 agents.
- Put inside every hunk (schema 6), whole files reached 18 of 20 record-engine agents in the wider run, all through diff queries the agents wrote themselves.
  None ran question 3's given diffs query, while all 20 ran question 1's.
- Record-files agents saw a diff in 10 of 20 attempts.
  Several of the others filtered the `.diff` files to changed lines with `grep '^[+-]'`, which removes exactly the context the whole-file hunks add.
  In the whole-file comparison the day before, most record-files agents read the root's calls in `steps.jsonl` instead of the diffs.

**Whether whole files help is still untested.**
Whole-file and 3-line hunks gave no different outcomes (mvcc 4 of 10 against 3 of 10, pp 0 of 6 against 0 of 6).
Neither local root can show a difference.
On mvcc, what separates passes from failures is a case the root's fix does not cover (two or more pending overwrites of one key), which no file shows (`2026-11-05_repair-anatomy.md`).
On production-planning, the failure is a wrong conclusion (10 early orders called infeasible) that no file shows.
A test needs roots whose failure is visible in a large file the retry cannot read fresh.

**What separates an mvcc pass is the part of the root's cause the root left unfixed.**
In the control run on `CsiwPnL`, all 10 attempts named the cause the root fixed (the snapshot builder ignores the published frontier), in a diagnosis written before changing a file.
The 3 that passed also named the part the root missed, the one the passing record-arm attempts found: unpublished versions above the frontier can be dropped before they are published.
None of the 7 that failed named it.
The reference solution fixes both parts with one loop in `src/snapshot_context.cc`; `2026-11-05_repair-anatomy.md` traces where each of the 122 mvcc attempts stopped, and corrects the earlier wording of a "second cause" in `flush_builder.cc`.

**Roots differ more than arms do.**
On `CsiwPnL`, the root of every earlier mvcc probe, record-files passed 5 of 7 against control's 0 of 7.
On `VTaE53Y` record-files passed none of 6.
One of nine root-by-arm cells, chosen after seeing the data, is not a finding.

**Having a record goes with skipping the diagnosis step.**
Control wrote `/logs/agent/diagnosis.md` in 20 of 20 attempts, record-engine in 2 of 20, and record-files in 7 of 20 (record arms together p < 0.001 against control).
Control's step asks for the same thing in nearly the same words and is always followed.
Agents read the step and skip it: in the `v4` check and the whole-file comparison, 10 of the 13 record-files agents that skipped it say so in their final message, and in the wider run 14 of the 31 record-arm agents that skipped it mention the diagnosis in theirs.
Small wording changes moved the rate a lot: record-engine went from 7 of 10 to 2 of 20 (p = 0.002, on different credentials) when "before your first command in the environment" became "Before you change any file".
So the step is not a stable measure across arms as written.

## Caveats

- **Small N.** Telling a 30% pass rate from a 50% one with 80% power at two-sided alpha 0.05 takes about 90 attempts per arm, so 10 to 20 attempts per cell find leads only.
- **One task carries the result.** The first mvcc root's record is 3 tool calls long and an mvcc retry takes 1 to 1.5 agent minutes at the median; production-planning, the only longer root, failed every retry.
- **Tasks that could not run.** cad-model and layout-config-recreation2 write 1.5 to 4 GB per checkpoint, so their retries were not run under the rule that checkpoint images are kept.
  Interleaved-vigenere ended in a safety refusal (`AgentSafetyRefusalError`, `[bio]`) in 2 of 6 sessions and was stopped.
- **One run on a different credential.** The whole-file comparison ran on an API key without ADR-0005's output cap, because its worktree had no `.env`.
  Its two conditions are comparable with each other, but not cleanly with other days' runs.
  `trajlab run` now refuses an env without a credential or the cap.
- **Probe, not the full loop.** k = 1 only, no chains, no write recorder, emulated amd64.

## Effect on the project's claim

- The direction on mvcc is consistent with the record helping a retry, in both record arms alike: 6 of 20 against 2 of 20 in the wider run.
- Nothing here separates the engine from the files: same pass rate, same diagnosis problem, and the engine's advantage is only in how reliably agents see the diffs.
- The interim null for the engine is not explained by non-use, so the paper's question becomes whether the record helps at all on harder roots, rather than whether agents use the engine.

## Decisions open for the owner

1. **Commit.** Everything since 7a4001a is uncommitted in `../trajlab-fullctx` on `sl/exp2-full-context-diffs`.
2. **Disk policy for heavy tasks.** Prune never-ingested probe images, or write an ADR for an ack-only probe watcher, before cad-model or layout roots can run.
3. **Vigenere.** Keep it, given the refusals, or drop it from the sample.
4. **Diagnosis step parity.** Make it the same required first file write in every arm, or drop it from all three.
5. **The capture host.** Start it to run `scripts/2026-11-04_large_rewrite_roots.py` on `tb40-sonnet-v4`'s store and pick roots that rewrote large files.

## Next experiment

A pilot under a new run name with its own preregistration: the corpus store rebuilt at schema 6, roots picked by `scripts/2026-11-04_large_rewrite_roots.py` plus short-record tasks, N = 1 in all three arms, on the branch with ADR-0023's amendment committed.
The run note's last section lists everything the preregistration needs.
It resolves whether whole-file history helps on roots where a file is the evidence, which neither local root can show.
