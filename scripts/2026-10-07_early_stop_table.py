"""One row per round-1 trial: tool calls, file-changing calls, failing checks, early-stop flags.

Reads what `scripts/2026-10-02_repair_report.py` wrote next to the source job (`repair-report.json`,
`repair-checks.json`) and each trial's `agent/checkpoints/calls.jsonl` and `agent/trajectory.json`.
Writes `<source>/early-stop.csv` (one row per trial; list fields joined with `;`).

File-changing calls come from the change gate (ADR-0010): a hooked call whose `change` is `changed`.
The gate never measures a trial's first hooked call (`change: baseline`), so the count is a range,
`changed_calls_min` to `changed_calls_max`. For the 66 trials whose range touched 0 or 1, the first
call's command was read by hand (2026-10-07) and classified in `BASELINE_CLASS`:
`write` (writes task or output files), `scratch` (writes only under `/tmp`, which the gate counts),
`read` (writes nothing); the range then collapses to one number. Every other trial has at least two
measured changed calls, so no threshold at 0 or 1 depends on its first call.

    uv run python scripts/2026-10-07_early_stop_table.py corpus/jobs/tb40-sonnet-v2 \
        --prefix tb40-repair-v2
"""

import argparse
import collections
import csv
import json
from datetime import datetime
from pathlib import Path

WRITE = """
bun-sourcemap-leak__2amsuyB bun-sourcemap-leak__iiUrVqM foodstuff-beta-activity__884GEfo
foodstuff-beta-activity__M2NToWX foodstuff-beta-activity__ovu5iL4 foodstuff-beta-activity__3U44rdX
foodstuff-beta-activity__VmYDbbe foodstuff-beta-activity__hs7AUoy freecad-impeller__4jdH7yZ
freecad-impeller__9GMumss freecad-impeller__WCpkxFs freecad-spring-clip__k7cuERS
freecad-spring-clip__wY8GiJU gsea-proteomics__B4LijyA mvcc-lsm-compaction__qZhpH4M
mvcc-lsm-compaction__sQ7ynD6 mvcc-lsm-compaction__tqJnE4F protein-autointerp-disulfide__TViUF7R
protein-autointerp-disulfide__i5UkRCA protein-autointerp-disulfide__oWKKNPU
protein-autointerp-disulfide__24FTT6W protein-autointerp-disulfide__BrRVAEX
protein-autointerp-disulfide__k7BoyN7 roy-polymorph-cn__W7d9zht
"""
SCRATCH = """
cad-model__GbXT7N8 roy-polymorph-cn__erk5gdU roy-polymorph-cn__hVVf9CP freecad-spring-clip__my7Lgzb
"""
READ = """
bun-sourcemap-leak__7vKyX9J bun-sourcemap-leak__ZFsouL9 bun-sourcemap-leak__bWBkDEi
bun-sourcemap-leak__fz2H4q7 bun-sourcemap-leak__ttgr7iS bun-sourcemap-leak__zZ57hYg
bun-sourcemap-leak__pTLDMN2 cad-model__4ZCenTM cad-model__JMaapgq cad-model__JNNmHZR
cad-model__nxBxaHq cargo-flight-dispatch__MTVyeeF cargo-flight-dispatch__PZC3pSZ
cargo-flight-dispatch__juPDRZd cargo-flight-dispatch__xBxUv3n foodstuff-beta-activity__VazFAs2
freecad-platform-drawing__PjwHQQe mvcc-lsm-compaction__EkrWD8Q mvcc-lsm-compaction__HKSuzfa
mvcc-lsm-compaction__MAF92rQ mvcc-lsm-compaction__Rx5TYSK mvcc-lsm-compaction__W7TfP4y
mvcc-lsm-compaction__iPLCvrn mvcc-lsm-compaction__kV2Djpm mvcc-lsm-compaction__zbYpCbD
pretrain-shard-corruption__msTkgQc protein-autointerp-disulfide__5rq8fHH
protein-autointerp-disulfide__FTyekmW protein-autointerp-disulfide__L2Seiat
protein-autointerp-disulfide__NrB4ARS protein-autointerp-disulfide__U2R2WSK
protein-autointerp-disulfide__YGGRdnf protein-autointerp-disulfide__Yo2C2V2
roy-polymorph-cn__uYfcEB2 sglang-qwen-burst__WDmGw7g sglang-qwen-burst__pmVnG4z
sglang-qwen-burst__uJ8Yr98
"""
BASELINE_CLASS = (
    {t: "write" for t in WRITE.split()}
    | {t: "scratch" for t in SCRATCH.split()}
    | {t: "read" for t in READ.split()}
)

COLUMNS = [
    "task",
    "arm",
    "trial",
    "source",
    "task_first_attempt_passes",
    "reward",
    "passed",
    "exception",
    "agent_s",
    "trial_s",
    "api_calls",
    "tool_calls",
    "hooked_calls",
    "watcher_timeouts",
    "checkpoints",
    "detected_changed_calls",
    "unchanged_calls",
    "baseline_tool",
    "baseline_class",
    "changed_calls_min",
    "changed_calls_max",
    "changed_paths_total",
    "s_to_first_detected_change",
    "first_command",
    "check_kind",
    "n_checks",
    "n_failed",
    "failing_checks",
    "source_n_failed",
    "source_failing_checks",
    "set_relation",
    "n_fixed",
    "n_broken",
    "zero_change",
    "le1_change",
    "zero_change_same_set",
    "le1_change_same_set",
    "cost_usd_own",
    "tok_output",
    "tok_cache_read",
]


def trial_dir(jobs: Path, source_job: Path, prefix: str, row: dict) -> Path:
    if row["arm"] == "original":
        return source_job / row["trial"]
    return jobs / f"{prefix}-{row['source']}-{row['arm']}" / row["trial"]


def first_command(tdir: Path, tool_call_id: str | None) -> str:
    if tool_call_id is None or not (tdir / "agent/trajectory.json").exists():
        return ""
    for step in json.loads((tdir / "agent/trajectory.json").read_text())["steps"]:
        for tc in step.get("tool_calls") or []:
            if tc["tool_call_id"] == tool_call_id:
                args = tc.get("arguments") or {}
                return " ".join(str(args.get("command", args)).split())[:300]
    return ""


def flag(lo: int | None, hi: int | None, n: int) -> str:
    """'1' when the count is surely <= n, '0' when surely > n, '' when unknown."""
    if lo is None:
        return ""
    if hi <= n:
        return "1"
    if lo > n:
        return "0"
    return ""


def relation(fails: set[str], src: set[str]) -> str:
    if fails == src:
        return "same"
    if fails < src:
        return "subset"
    if fails > src:
        return "superset"
    return "different"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("source_job", type=Path)
    ap.add_argument("--prefix", required=True)
    args = ap.parse_args()
    source_job: Path = args.source_job
    jobs = source_job.parent

    rows = json.loads((source_job / "repair-report.json").read_text())
    checks = json.loads((source_job / "repair-checks.json").read_text())

    kind: dict[tuple, str] = {}
    n_checks: collections.Counter = collections.Counter()
    fails: dict[tuple, set] = collections.defaultdict(set)
    src_fails: dict[tuple, set] = collections.defaultdict(set)
    passes: dict[tuple, set] = collections.defaultdict(set)
    src_passes: dict[tuple, set] = collections.defaultdict(set)
    for c in checks:
        if c["status"] is None:  # CAD metric rows carry only a value
            continue
        k = (c["arm"], c["trial"])
        kind[k] = c["kind"]
        n_checks[k] += 1
        (fails if c["status"] == "failed" else passes)[k].add(c["check"])
        if c["source_status"] == "failed":
            src_fails[k].add(c["check"])
        elif c["source_status"] == "passed":
            src_passes[k].add(c["check"])

    task_passes = collections.Counter(
        r["task"] for r in rows if r["arm"] == "original" and r["reward"] == 1.0
    )

    out = []
    for r in rows:
        tdir = trial_dir(jobs, source_job, args.prefix, r)
        cdir = tdir / "agent/checkpoints"
        calls = (
            [json.loads(line) for line in (cdir / "calls.jsonl").read_text().splitlines()]
            if (cdir / "calls.jsonl").exists()
            else []
        )
        hooked = [c for c in calls if c["trigger"] == "tool_call"]
        changed = [c for c in calls if c["change"] == "changed"]
        detected = sum(1 for c in hooked if c["change"] == "changed")
        base = next((c for c in hooked if c["change"] == "baseline"), None)
        bclass = BASELINE_CLASS.get(r["trial"], "not_read") if base else "unmeasured"
        if base is None:
            lo = hi = None
        elif bclass in ("write", "scratch"):
            lo = hi = detected + 1
        elif bclass == "read":
            lo = hi = detected
        else:
            lo, hi = detected, detected + 1
        first_change_s = ""
        if hooked and changed:
            t0 = datetime.fromisoformat(hooked[0]["requested_at"])
            t1 = datetime.fromisoformat(changed[0]["requested_at"])
            first_change_s = round((t1 - t0).total_seconds(), 1)

        k = (r["arm"], r["trial"])
        has_checks = n_checks[k] > 0
        f, sf = fails[k], (fails[k] if r["arm"] == "original" else src_fails[k])
        is_repair = r["arm"] != "original"
        same = has_checks and is_repair and f == sf
        failed = r["reward"] is not None and r["reward"] < 1.0
        z, l1 = flag(lo, hi, 0), flag(lo, hi, 1)
        out.append(
            {
                **{
                    c: r.get(c)
                    for c in (
                        "task",
                        "arm",
                        "trial",
                        "source",
                        "reward",
                        "exception",
                        "agent_s",
                        "trial_s",
                        "api_calls",
                        "tool_calls",
                        "cost_usd_own",
                        "tok_output",
                        "tok_cache_read",
                    )
                },
                "task_first_attempt_passes": task_passes[r["task"]],
                "passed": int(r["reward"] == 1.0),
                "hooked_calls": len(hooked),
                "watcher_timeouts": len(list(cdir.glob("*.timeout"))) if cdir.exists() else 0,
                "checkpoints": len((cdir / "checkpoints.jsonl").read_text().splitlines())
                if (cdir / "checkpoints.jsonl").exists()
                else 0,
                "detected_changed_calls": detected,
                "unchanged_calls": sum(1 for c in hooked if c["change"] == "unchanged"),
                "baseline_tool": base["tool_name"] if base else "",
                "baseline_class": bclass,
                "changed_calls_min": lo,
                "changed_calls_max": hi,
                "changed_paths_total": sum(c["changed_paths_total"] or 0 for c in changed),
                "s_to_first_detected_change": first_change_s,
                "first_command": first_command(tdir, base["tool_call_id"] if base else None),
                "check_kind": kind.get(k, ""),
                "n_checks": n_checks[k],
                "n_failed": len(f),
                "failing_checks": ";".join(sorted(f)),
                "source_n_failed": len(sf) if has_checks else "",
                "source_failing_checks": ";".join(sorted(sf)) if has_checks else "",
                "set_relation": (relation(f, sf) if is_repair else "")
                if has_checks
                else "no_checks",
                "n_fixed": len(sf - f) if has_checks and is_repair else "",
                "n_broken": len(f & src_passes[k]) if has_checks and is_repair else "",
                "zero_change": z,
                "le1_change": l1,
                "zero_change_same_set": (str(int(z == "1" and same)) if z else "")
                if is_repair and failed
                else "",
                "le1_change_same_set": (str(int(l1 == "1" and same)) if l1 else "")
                if is_repair and failed
                else "",
            }
        )

    dest = source_job / "early-stop.csv"
    with dest.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(out)
    print(f"wrote {len(out)} rows to {dest}")


if __name__ == "__main__":
    main()
