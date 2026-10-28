# Exp 2 preregistration: `tb40-exp2-dry1` (dry run)

Written 2026-10-28 07:29 UTC by `trajlab exp2 prereg` at `f28735ffd6c5ef9a3247350142e71ca4280e499d`.
It is committed before the first `record-engine` attempt runs: `trajlab exp2 run` refuses that arm until this file is the version at HEAD, and refuses any run whose prompts or derived views the code no longer reproduces (technical spec §5.3, ADR-0022).
The block under Settings is what the loop runs; the prose repeats it.

## Sample

3 failed first attempts of `corpus/jobs/tb40-sonnet-v4`, from 3 tasks; each roots one chain per arm. Store `corpus/stores/tb40-sonnet-v4`.

| Root failure | Task | Instruction sha256 |
|---|---|---|
| `mvcc-lsm-compaction__PFDAk9Y` | `terminal-bench/mvcc-lsm-compaction` | `a168992073c09912` |
| `bun-sourcemap-leak__3f2BwTC` | `terminal-bench/bun-sourcemap-leak` | `48524392be65b45b` |
| `cargo-flight-dispatch__DAgHbSz` | `terminal-bench/cargo-flight-dispatch` | `4c63f550eaee8759` |

Excluded: none.

## Budget, arms, and comparison

- N = 1, reported at N = 1.
- Arms: `control`, `record-files`, `record-engine`; a fresh environment and a fresh Claude Code session for every attempt in every arm; no stop hook.
- Primary comparison: record-engine against record-files: repair rate at N = 3, paired by failure.
- Test: none (dry run).

## Record, prompt, and agent

- Store schema 4, derived `3+c45ab40d3b46`: `step_flags`, `delta_flags`, `path_versions`, `trial_summary`, `step_jobs`, `delta_attribution`.
- Protocol steps sha256 `bca9ebe9add06b324c12f93c51a60ac274f9db0c26dfa5b7a3975c7b2e764c5c`; hooks file sha256 `9a84f108f6e13bb940c6bc35c8bda6f8d1d90b8031259a46eaa873a6f3fd9022`.
- Query tool: cap 200 rows, timeout 10.0 s, memory 512MB, 2 threads.
- Model `anthropic/claude-sonnet-5-5`, reasoning_effort `medium`, version `2.1.278`, agent timeout multiplier 0.5; config `configs/harbor/tb40-sonnet-v4.json`.

| Prompt prefix (arm / prior attempts) | sha256 |
|---|---|
| `control/1` | `22c378644c7d84c0eefb5cce29ed02ca82e101b5fe9c2ffd21cc5bdaf7913c1c` |
| `record-files/1` | `0740f6ce140c8424eb4cde24b7fd7ad52e4775bf5c06d6df86f8216a147cf84c` |
| `record-engine/1` | `8d6ee59ac21e66057ecd82b550f9139cd63918b0a9254f885c01098ed9d0d2ed` |

## Settings

```json
{
  "run": "tb40-exp2-dry1",
  "kind": "dry-run",
  "corpus_job": "corpus/jobs/tb40-sonnet-v4",
  "corpus_store": "corpus/stores/tb40-sonnet-v4",
  "corpus_config": "configs/harbor/tb40-sonnet-v4.json",
  "sample": [
    {
      "trial_name": "mvcc-lsm-compaction__PFDAk9Y",
      "trial_id": "c4cbbdba-e348-4080-a26e-f122d53e14f9",
      "task": "terminal-bench/mvcc-lsm-compaction",
      "instruction_sha256": "a168992073c0991248e620dd4c12cdf98d443df184c62d4abc3b64bfda756c9e"
    },
    {
      "trial_name": "bun-sourcemap-leak__3f2BwTC",
      "trial_id": "dc48b931-6ef1-4565-b70c-38980b756639",
      "task": "terminal-bench/bun-sourcemap-leak",
      "instruction_sha256": "48524392be65b45b3bdea9454b1d9a6a1e65879df06540e50aa5c3bbd9e32195"
    },
    {
      "trial_name": "cargo-flight-dispatch__DAgHbSz",
      "trial_id": "be1b4732-cffb-4316-946b-7991435475fc",
      "task": "terminal-bench/cargo-flight-dispatch",
      "instruction_sha256": "4c63f550eaee87595fb1b0d796f2af6ab4bb822960460a5092ba86f4fc677563"
    }
  ],
  "exclusions": [],
  "budget": 1,
  "report_at": [
    1
  ],
  "arms": [
    "control",
    "record-files",
    "record-engine"
  ],
  "primary": "record-engine against record-files: repair rate at N = 3, paired by failure",
  "test": "none (dry run)",
  "schema_version": 4,
  "derived": "3+c45ab40d3b46",
  "derived_views": [
    "step_flags",
    "delta_flags",
    "path_versions",
    "trial_summary",
    "step_jobs",
    "delta_attribution"
  ],
  "protocol_sha256": "bca9ebe9add06b324c12f93c51a60ac274f9db0c26dfa5b7a3975c7b2e764c5c",
  "prefix_sha256": {
    "control/1": "22c378644c7d84c0eefb5cce29ed02ca82e101b5fe9c2ffd21cc5bdaf7913c1c",
    "record-files/1": "0740f6ce140c8424eb4cde24b7fd7ad52e4775bf5c06d6df86f8216a147cf84c",
    "record-engine/1": "8d6ee59ac21e66057ecd82b550f9139cd63918b0a9254f885c01098ed9d0d2ed"
  },
  "hooks_sha256": "9a84f108f6e13bb940c6bc35c8bda6f8d1d90b8031259a46eaa873a6f3fd9022",
  "stop_hook": null,
  "query": {
    "arm": "record-engine",
    "ingests": true,
    "mounts_bundle": false,
    "serves_queries": true,
    "query_cap": 200,
    "query_timeout_s": 10.0,
    "query_memory_limit": "512MB",
    "query_threads": 2
  },
  "model": "anthropic/claude-sonnet-5-5",
  "agent_kwargs": {
    "reasoning_effort": "medium",
    "version": "2.1.278"
  },
  "agent_timeout_multiplier": 0.5,
  "repo_sha": "f28735ffd6c5ef9a3247350142e71ca4280e499d"
}
```
