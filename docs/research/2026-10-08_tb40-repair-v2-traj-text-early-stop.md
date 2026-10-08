# Round 1 early-stop table, `traj-text` arm (tb40-repair-v2), one row per trial

The table is `corpus/jobs/tb40-sonnet-v2/early-stop.traj-text.csv` on the Linux server (`/srv/trajlab/jobs`; not in git). This note is its README, also written there as `early-stop.traj-text.README.md`. It has the columns of round 1's table for the four original arms (`early-stop.csv`, `docs/research/2026-10-07_tb40-repair-v2-early-stop.md`), computed by the same script; the two tables can be concatenated, dropping one table's `original` rows.

132 rows: 69 first attempts (`arm = original`, identical to their rows in `early-stop.csv`) and 63 `traj-text` repairs.
Generated 2026-10-08 by `uv run python scripts/2026-10-07_early_stop_table.py corpus/jobs/tb40-sonnet-v2 --prefix tb40-repair-v2 --arms traj-text`, from `repair-report.traj-text.json` and `repair-checks.traj-text.json`. Run without `--arms`, the script still writes round 1's `early-stop.csv` byte for byte.
Lists are joined with `;`. An empty cell means "not applicable" or "unknown"; the columns below say which.

## Identity and outcome
| Column | Meaning |
|---|---|
| `task`, `trial` | Task name; Harbor trial name (`<task>__<id>`). |
| `arm` | `original` (first attempt) or the repair arm. |
| `source` | The failed first attempt the repair started from; equals `trial` for first attempts. |
| `task_first_attempt_passes` | First attempts of this task with reward 1, out of 3 (the ADR-0012 stratum). |
| `reward`, `passed` | Verifier reward; `passed` is 1 when reward is 1.0. Empty reward: no verifier result (infra error). |
| `exception` | Harbor's exception type, empty if none (e.g. `AgentSafetyRefusalError`, `AgentTimeoutError`). |

## Effort
| Column | Meaning |
|---|---|
| `agent_s`, `trial_s` | Agent seconds; whole-trial seconds. |
| `api_calls`, `tool_calls` | The trial's own API calls and tool calls (resumed arms exclude the source's history). |
| `cost_usd_own`, `tok_output`, `tok_cache_read` | Own cost at Sonnet 5.5 list prices, and two token counts (from `repair-report.traj-text.json`). `traj-text` resumes no session, so all its tokens are its own. |

## File changes (change gate, ADR-0010)
| Column | Meaning |
|---|---|
| `hooked_calls` | Tool calls the PostToolUse hook reported. |
| `watcher_timeouts` | Hook requests the watcher did not answer in time. |
| `checkpoints` | Records in `checkpoints.jsonl` (baseline + changed calls + stop checkpoint if the final state was new). |
| `detected_changed_calls` | Hooked calls the gate measured as changing the filesystem. |
| `unchanged_calls` | Hooked calls measured as changing nothing. |
| `baseline_tool` | Tool of the first hooked call, which the gate never measures (`change: baseline`). |
| `baseline_class` | How that first call was settled: `write` (its command writes task or output files), `scratch` (writes only under `/tmp`, which the gate counts as a change), `read` (writes nothing): read by hand for the 20 `traj-text` trials where it mattered (first attempts: as in round 1's table). See "How the 20 first calls were read". `not_read`: the trial has at least 2 detected changes, so thresholds at 0 or 1 do not depend on it. `unmeasured`: no `calls.jsonl` baseline (watcher timed out or trial never ran a hooked call). |
| `changed_calls_min`, `changed_calls_max` | File-changing calls including the first call: equal when `baseline_class` is `write`/`scratch`/`read`; a range of 1 when `not_read`; empty when `unmeasured`. |
| `changed_paths_total` | Paths changed, summed over detected changed calls (a path changed twice counts twice). |
| `s_to_first_detected_change` | Seconds from the first hooked call to the first detected changed call (0 can't occur: the first call is never "detected"). |
| `first_command` | The first hooked call's command, whitespace-collapsed, first 300 characters. |

## Verifier checks (`repair-checks.traj-text.json`)
| Column | Meaning |
|---|---|
| `check_kind` | `pytest`, `trace` (vba-userform-port), or `cad` (freecad tasks: only the overall `score` has a pass/fail). |
| `n_checks`, `n_failed`, `failing_checks` | Checks with a pass/fail status in this trial; those failed; their names. |
| `source_n_failed`, `source_failing_checks` | The same for the source trial (for first attempts, the trial itself). |
| `set_relation` | Repairs only: `same`, `subset` (fails strictly fewer, all among the source's), `superset`, or `different` vs the source's failing set. `no_checks`: no verifier output. Empty for first attempts. |
| `n_fixed`, `n_broken` | Repairs only: source-failed checks that now pass; source-passed checks that now fail. |

## Early-stop flags
`1` true, `0` false, empty when not applicable. All are decided for every row (no unknowns).

| Column | Meaning |
|---|---|
| `zero_change`, `le1_change` | File-changing calls (first call included) = 0, ≤ 1. Every row. |
| `zero_change_same_set`, `le1_change_same_set` | The same, and the failing set equals the source's. Failed repairs only (reward < 1); empty for first attempts, passes, and errored trials. |

## Counts (failed trials, no exception)
Round 1's columns are copied from its table; the `traj-text` column is this table's.

| | original | `fresh` | `state` | `state-traj` | `traj` | `traj-text` |
|---|---|---|---|---|---|---|
| Failed | 55 | 51 | 54 | 48 | 46 | 60 |
| 0 changes | 0 | 0 | 0 | 0 | 0 | 1 |
| 0 changes, same set | – | 0 | 0 | 0 | 0 | 1 |
| ≤ 1 change | 10 | 9 | 11 | 14 | 8 | 17 |
| ≤ 1 change, same set | – | 9 | 8 | 8 | 5 | 14 |

No passing `traj-text` repair has ≤ 1 change. No `traj-text` trial has an exception, so "Failed" is every repair with reward 0.
The 17 `traj-text` repairs with ≤ 1 change: bun-sourcemap-leak `4qq8YMG`, `CtSZWt2`, `EJm44oc`; cad-model `MjzjJib`, `VDUq4Yv` (0 changes), `jQXbefM`; foodstuff-beta-activity `N9sfw3G`, `hPjQ8Fj`; freecad-spring-clip `9U2GWjd`; layout-config-recreation2 `2uTCWUv`; mvcc-lsm-compaction `6RLSMD4`; protein-autointerp-disulfide `QbEeLud`, `WNPoYKE`, `Zrwjj6u`; roy-polymorph-cn `yV6TVyN`; sglang-qwen-burst `piTFfpk`; sound-change-cascade `pFcH7Ac`. Three of them (foodstuff-beta-activity `N9sfw3G`, roy-polymorph-cn `yV6TVyN`, sglang-qwen-burst `piTFfpk`) fail a superset of their source's checks; the other 14 fail the same set.

## How the 20 first calls were read
On 2026-10-08, each of the 20 first calls was read by two readers working independently, from its full command and output in `agent/trajectory.json`, with round 1's definitions and examples. They agreed on all 20. Labels are in `TRAJ_TEXT_WRITE`, `TRAJ_TEXT_SCRATCH`, and `TRAJ_TEXT_READ` in the script: 6 `write`, 3 `scratch`, 11 `read`.

One case did not occur in round 1: a command that means to write but fails before writing. Five first calls opened a file that does not exist in the task image and stopped with `FileNotFoundError`, before any write ran:

| Trial | Missing file | What the command would have written |
|---|---|---|
| cad-model `jQXbefM` | `m.py` | `m.py` (`open('m.py','w')`) |
| freecad-spring-clip `9U2GWjd` | `answer.py` | `answer.py`, then `rm -rf __pycache__` |
| layout-config-recreation2 `2uTCWUv` | `output/config.json` | `/tmp/out.png` (`render.py`) |
| protein-autointerp-disulfide `WNPoYKE` | `predicted_features.json` | `predicted_features.json` |
| sound-change-cascade `pFcH7Ac` | `rules.json` | `/tmp/o.tsv` (`engine/apply.py`) |

They are classed by effect, as `read`: the class sets whether the first call counts as a file-changing call. Each of the five has one detected changed call; classed by intent, all five would move from ≤ 1 change to 2.

## Known limits
- `baseline_class` is a hand reading of a command, not a measurement.
- A call whose hook timed out (`watcher_timeouts`) has no record in `calls.jsonl`, so it is counted neither as changed nor as unchanged, and the flags do not see it. Three `traj-text` rows flagged ≤ 1 change have one such call, each `pip install cadquery`: cad-model `MjzjJib` (`pip install cadquery; python m.py`), `VDUq4Yv` (`pip install cadquery; cat > m.py ...`, the row with 0 changes), and `jQXbefM` (`pip install cadquery; python m.py`). Round 1's table has two rows like this: `fresh` cad-model `4ZCenTM` and first attempt freecad-spring-clip `my7Lgzb`, each with 2 timed-out calls.
- The gate counts every filesystem change, including incidental ones (`__pycache__`, build outputs); `changed_paths_total` and the trial dirs' `calls.jsonl` show which.
- Process and memory state are not captured (ADR-0004): a call that only starts a server counts as unchanged.
