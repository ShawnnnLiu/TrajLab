# Round 1 early-stop table (tb40-repair-v2), one row per trial

The table is `corpus/jobs/tb40-sonnet-v2/early-stop.csv` on the Linux server (`/srv/trajlab/jobs`; not in git). This note is its README, also written there as `early-stop.README.md`.

321 rows: 69 first attempts (`arm = original`) and 63 repairs in each of `fresh`, `state`, `state-traj`, `traj`.
Generated 2026-10-07 by `uv run python scripts/2026-10-07_early_stop_table.py corpus/jobs/tb40-sonnet-v2 --prefix tb40-repair-v2`.
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
| `cost_usd_own`, `tok_output`, `tok_cache_read` | Own cost at Sonnet 5.5 list prices, and two token counts (from `repair-report.json`). |

## File changes (change gate, ADR-0010)
| Column | Meaning |
|---|---|
| `hooked_calls` | Tool calls the PostToolUse hook reported. |
| `watcher_timeouts` | Hook requests the watcher did not answer in time. |
| `checkpoints` | Records in `checkpoints.jsonl` (baseline + changed calls + stop checkpoint if the final state was new). |
| `detected_changed_calls` | Hooked calls the gate measured as changing the filesystem. |
| `unchanged_calls` | Hooked calls measured as changing nothing. |
| `baseline_tool` | Tool of the first hooked call, which the gate never measures (`change: baseline`). |
| `baseline_class` | How that first call was settled: `write` (its command writes task or output files), `scratch` (writes only under `/tmp`, which the gate counts as a change), `read` (writes nothing): all three read by hand for the 66 trials where it mattered. `not_read`: the trial has at least 2 detected changes, so thresholds at 0 or 1 do not depend on it. `unmeasured`: no `calls.jsonl` baseline (watcher timed out or trial never ran a hooked call). |
| `changed_calls_min`, `changed_calls_max` | File-changing calls including the first call: equal when `baseline_class` is `write`/`scratch`/`read`; a range of 1 when `not_read`; empty when `unmeasured`. |
| `changed_paths_total` | Paths changed, summed over detected changed calls (a path changed twice counts twice). |
| `s_to_first_detected_change` | Seconds from the first hooked call to the first detected changed call (0 can't occur: the first call is never "detected"). |
| `first_command` | The first hooked call's command, whitespace-collapsed, first 300 characters. |

## Verifier checks (`repair-checks.json`)
| Column | Meaning |
|---|---|
| `check_kind` | `pytest`, `trace` (vba-userform-port), or `cad` (freecad tasks: only the overall `score` has a pass/fail). |
| `n_checks`, `n_failed`, `failing_checks` | Checks with a pass/fail status in this trial; those failed; their names. |
| `source_n_failed`, `source_failing_checks` | The same for the source trial (for first attempts, the trial itself). |
| `set_relation` | Repairs only: `same`, `subset` (fails strictly fewer, all among the source's), `superset`, or `different` vs the source's failing set. `no_checks`: no verifier output. Empty for first attempts. |
| `n_fixed`, `n_broken` | Repairs only: source-failed checks that now pass; source-passed checks that now fail. |

## Early-stop flags
`1` true, `0` false, empty when not applicable. All are decided for every row (no unknowns in round 1).

| Column | Meaning |
|---|---|
| `zero_change`, `le1_change` | File-changing calls (first call included) = 0, ≤ 1. Every row. |
| `zero_change_same_set`, `le1_change_same_set` | The same, and the failing set equals the source's. Failed repairs only (reward < 1); empty for first attempts, passes, and errored trials. |

## Counts (failed trials, no exception)
| | original | `fresh` | `state` | `state-traj` | `traj` | Repairs |
|---|---|---|---|---|---|---|
| Failed | 55 | 51 | 54 | 48 | 46 | 199 |
| 0 changes | 0 | 0 | 0 | 0 | 0 | 0 |
| 0 changes, same set | – | 0 | 0 | 0 | 0 | 0 |
| ≤ 1 change | 10 | 9 | 11 | 14 | 8 | 42 |
| ≤ 1 change, same set | – | 9 | 8 | 8 | 5 | 30 |

Two passing repairs also have ≤ 1 change (`mvcc-lsm-compaction__qZhpH4M`, `sglang-qwen-burst__WDmGw7g`).
`freecad-platform-drawing__DiSv3hE` (a first attempt) is excluded from "Failed": its watcher timed out on every call.

## Known limits
- `baseline_class` is a hand reading of a command, not a measurement.
- The gate counts every filesystem change, including incidental ones (`__pycache__`, build outputs); `changed_paths_total` and the trial dirs' `calls.jsonl` show which.
- Process and memory state are not captured (ADR-0004): a call that only starts a server counts as unchanged.
