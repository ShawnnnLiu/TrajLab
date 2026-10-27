"""Replay ADR-0020's attribution rule as first worded over real trials (2026-10-27 validation).

Run before the rule was implemented, on a schema-2 store plus the layer mtimes dumped by
2026-10-27_adr0020_layer_mtimes.py; its numbers are in ADR-0020's amendment of 2026-10-27.
Superseded by the `delta_attribution` view on a schema-3 store, and by
2026-10-27_adr0020_attribution_report.py, which reads that view. Kept as the record of what
was measured and how.

    uv run python scripts/2026-10-27_adr0020_replay.py <schema-2 store> <mtimes dir> <jobs dir>
        [--verbose] <trial name>...
"""

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

BG_PAT = re.compile(
    r"(?:moved to the background \(ID: |running in background with ID: )([a-z0-9]+)"
)
NOTICE = re.compile(r"<task-id>([a-z0-9]+)</task-id>.*?<status>(\w+)</status>", re.S)
# `&` as a job separator (not `&&`, not `2>&1`, not `|&`), or nohup/setsid/disown.
HAND = re.compile(r"(?<![&>|\d])&(?![&>])|\bnohup\b|\bsetsid\b|\bdisown\b")
# A heredoc body: from `<<'TAG'` / `<<TAG` to the line holding TAG alone.
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?^\1$", re.S | re.M)
# A program passed inline: `python -c "..."`, `python3 -c '...'`, `bash -c '...'`.
INLINE_CODE = re.compile(r"(\s-c)\s+(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", re.S)
CACHE = re.compile(r"/__pycache__/|\.pyc$|/\.pytest_cache/")
TOL = timedelta(seconds=1)  # tar mtimes are whole seconds


def ts(x: float) -> datetime:
    return datetime.fromtimestamp(x, tz=UTC)


def in_window(m: datetime, a: datetime, b: datetime) -> bool:
    """[m, m+1s) intersects [a, b]."""
    return m + TOL > a and m <= b


def load_notices(trial_dir: Path) -> dict[str, tuple[datetime, str]]:
    out: dict[str, tuple[datetime, str]] = {}
    for path in (trial_dir / "agent/sessions/projects").glob("*/*.jsonl"):
        for line in path.open():
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            if j.get("type") != "queue-operation" or j.get("operation") != "enqueue":
                continue
            m = NOTICE.search(j.get("content") or "")
            if m:
                when = datetime.fromisoformat(j["timestamp"].replace("Z", "+00:00"))
                out.setdefault(m.group(1), (when, m.group(2)))
    return out


def load_mtimes(path: Path) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = defaultdict(dict)
    for line in path.open():
        j = json.loads(line)
        out[j["seq"]][j["path"]] = j
    return out


def evaluate(con, trial_name: str, mtimes_dir: Path, jobs_dir: Path, verbose: bool) -> dict:
    trial_dir = jobs_dir / trial_name
    (trial_id,) = con.execute(
        "SELECT trial_id FROM trials WHERE trial_name = ?", [trial_name]
    ).fetchone()
    steps = con.execute(
        "SELECT step_idx, source, tool, tool_call_id, args_json, output, t_start, t_end, "
        "checkpoint_seq "
        "FROM steps WHERE trial_id = ? ORDER BY step_idx",
        [trial_id],
    ).fetchall()
    deltas = con.execute(
        "SELECT step_idx, path, op FROM deltas WHERE trial_id = ? ORDER BY step_idx, path",
        [trial_id],
    ).fetchall()
    by_idx = {s[0]: s for s in steps}
    # steps.checkpoint_seq is the state a step saw; the owner of a seq is the first step with it.
    step_of_seq: dict[int, int] = {}
    for s in steps:
        if s[8] is not None and s[8] not in step_of_seq:
            step_of_seq[s[8]] = s[0]
    seqs = sorted(step_of_seq)
    prev_seq = {seq: (seqs[i - 1] if i else None) for i, seq in enumerate(seqs)}
    seq_of_step = {v: k for k, v in step_of_seq.items()}
    first_start = min(s[6] for s in steps if s[6] is not None)
    stop_steps = [s for s in steps if s[2] in ("Stop", "StopFailure")]
    trial_end = stop_steps[-1][6] if stop_steps else max(s[7] or s[6] for s in steps if s[6])
    notices = load_notices(trial_dir)
    mt = load_mtimes(mtimes_dir / f"{trial_name}.jsonl")

    # Backgrounded calls and their job windows.
    bg = []
    for s in steps:
        if s[2] != "Bash" or not s[5]:
            continue
        m = BG_PAT.search(s[5])
        if not m:
            continue
        task = m.group(1)
        done, status = notices.get(task, (None, None))
        window_end = done or trial_end
        # ADR's own wording: the t_start of the step where the notice arrived.
        done_step = next((x[0] for x in steps if x[6] and done and x[6] >= done), None)
        cmd = json.loads(s[4] or "{}").get("command", "")
        bg.append(
            {
                "step": s[0],
                "task": task,
                "start": s[6],
                "end": window_end,
                "done": done,
                "status": status,
                "done_step": done_step,
                "cmd": cmd,
            }
        )
    hand = []
    for s in steps:
        if s[2] != "Bash":
            continue
        cmd = json.loads(s[4] or "{}").get("command", "")
        shell_only = INLINE_CODE.sub(r"\1 <code>", HEREDOC.sub("<<heredoc>", cmd))
        if HAND.search(shell_only):
            hand.append({"step": s[0], "start": s[6], "cmd": cmd, "shell": shell_only})
    calls = [s for s in steps if s[2] not in (None, "Stop", "StopFailure") and s[6] and s[7]]

    rows = []
    for k, path, op in deltas:
        seq = seq_of_step.get(k)
        entry = mt.get(seq, {}).get(path) if seq is not None else None
        before = (
            mt.get(prev_seq.get(seq), {}).get(path)
            if seq is not None and prev_seq.get(seq)
            else None
        )
        if op == "delete":
            m = ts(before["mtime"]) if before else None
        else:
            m = ts(entry["mtime"]) if entry else None
        is_cache = bool(CACHE.search(path))
        rec = {"k": k, "path": path, "op": op, "m": m, "is_cache": is_cache, "tool": by_idx[k][2]}
        if m is None:
            rec.update(attr="no_mtime", j=None)
            rows.append(rec)
            continue
        sk = by_idx[k]
        if sk[6] and sk[7] and in_window(m, sk[6], sk[7]):
            # Diagnostic: a job launched earlier names this file as a redirect target, and the
            # landing call neither redirects nor names it: the mtime alone cannot tell them apart.
            base = path.rsplit("/", 1)[-1]
            own_cmd = json.loads(sk[4] or "{}").get("command", "") if sk[2] == "Bash" else ""
            launchers = [
                h["step"]
                for h in hand
                if h["step"] < k
                and re.search(r">\s*\S*" + re.escape(base.split(".")[0][:4]), h["shell"])
            ]
            launchers += [
                b["step"]
                for b in bg
                if b["step"] < k
                and b["step"] not in launchers
                and re.search(r">\s*\S*" + re.escape(base.split(".")[0][:4]), b["cmd"])
            ]
            suspect = (
                bool(launchers) and ">" not in own_cmd and base not in own_cmd and sk[2] != "Write"
            )
            rec.update(attr="foreground", j=k, suspect=suspect, launchers=launchers)
            rows.append(rec)
            continue
        covering = [b for b in bg if b["step"] < k and in_window(m, b["start"], b["end"])]
        if len(covering) == 1:
            rec.update(attr="background", j=covering[0]["step"], task=covering[0]["task"])
            rows.append(rec)
            continue
        if len(covering) > 1:
            base = path.rsplit("/", 1)[-1]
            named = [b for b in covering if base in b["cmd"]]
            if len(named) == 1:
                rec.update(attr="background_by_path", j=named[0]["step"], task=named[0]["task"])
            else:
                rec.update(attr="ambiguous", j=None, candidates=[b["step"] for b in covering])
            rows.append(rec)
            continue
        inside_call = [c[0] for c in calls if in_window(m, c[6], c[7])]
        if not inside_call:
            open_hand = [h for h in hand if h["step"] < k]
            if len(open_hand) == 1:
                rec.update(attr="background_untracked", j=open_hand[0]["step"])
                rows.append(rec)
                continue
        if m < first_start:
            rec.update(attr="untrusted_mtime", j=None)
            rows.append(rec)
            continue
        # Diagnostic: the latest hand-launched call before the write, and whether its command
        # names the file (basename or its parent dir): what a tie-break rule could use.
        before = [h for h in hand if h["step"] < k and h["start"] <= m + TOL]
        latest = before[-1]["step"] if before else None
        base = path.rsplit("/", 1)[-1]
        parent = path.rsplit("/", 1)[0]
        naming = [
            h["step"]
            for h in before
            if base in h["cmd"] or (parent != "/tmp" and parent in h["cmd"])
        ]
        rec.update(
            attr="unknown",
            j=None,
            inside_call=inside_call,
            open_hand=[h["step"] for h in hand if h["step"] < k],
            latest_launcher=latest,
            naming_launchers=naming,
        )
        rows.append(rec)

    late = [r for r in rows if r.get("j") is not None and r["j"] < r["k"]]
    summary = {
        "trial": trial_name,
        "steps": len(steps),
        "checkpoints": len(seqs),
        "deltas": len(rows),
        "backgrounded": len(bg),
        "hand_launched": len(hand),
        "attr": Counter((r["attr"], r["is_cache"]) for r in rows),
        "late": len(late),
        "late_non_cache": sum(1 for r in late if not r["is_cache"]),
        "lags": sorted(r["k"] - r["j"] for r in late),
    }
    print(
        f"\n=== {trial_name}: {summary['steps']} steps, {summary['checkpoints']} checkpoints, "
        f"{summary['deltas']} deltas, {len(bg)} backgrounded, {len(hand)} hand-launched ==="
    )
    print("job windows (step, task, status, start -> end, done_step):")
    for b in bg:
        print(
            f"  step {b['step']:4d} {b['task']} {b['status'] or 'NO NOTICE':9s} "
            f"{b['start'].strftime('%H:%M:%S')} -> {b['end'].strftime('%H:%M:%S')} "
            f"({(b['end'] - b['start']).total_seconds():5.0f} s) done_step={b['done_step']} | "
            f"{b['cmd'][:70].replace(chr(10), ' ')}"
        )
    if hand:
        print("hand-launched calls:")
        for h in hand:
            print(f"  step {h['step']:4d} | {h['shell'][:140].replace(chr(10), ' ')}")
    print("attribution (non-cache / cache):")
    for attr in (
        "foreground",
        "background",
        "background_by_path",
        "ambiguous",
        "background_untracked",
        "untrusted_mtime",
        "unknown",
        "no_mtime",
    ):
        a, c = summary["attr"][(attr, False)], summary["attr"][(attr, True)]
        if a or c:
            print(f"  {attr:22s} {a:5d} / {c:5d}")
    suspects = [r for r in rows if r.get("suspect")]
    summary["suspect_foreground"] = len(suspects)
    print(
        f"late deltas: {len(late)} ({summary['late_non_cache']} non-cache); "
        f"lags (calls): {summary['lags'][:60]}"
    )
    print(
        "suspect foreground (file is a redirect target of an earlier job, landing call does "
        f"not write it): {len(suspects)}"
    )
    for r in suspects[:12]:
        print(
            f"  SUSPECT {r['path']} landed step {r['k']} ({r['tool']}) "
            f"write {r['m'].strftime('%H:%M:%S')} launchers {r['launchers']}"
        )
    for r in late:
        if r["is_cache"] and not verbose:
            continue
        print(
            f"  LATE {r['attr']:20s} {r['path']} landed step {r['k']} ({r['tool']}) "
            f"caused by step {r['j']} lag {r['k'] - r['j']} write {r['m'].strftime('%H:%M:%S')}"
        )
    bad = [r for r in rows if r["attr"] in ("ambiguous", "unknown", "untrusted_mtime")]
    unknowns = [r for r in rows if r["attr"] == "unknown"]
    if unknowns:
        with_latest = sum(1 for r in unknowns if r["latest_launcher"] is not None)
        with_naming = sum(1 for r in unknowns if len(r["naming_launchers"]) == 1)
        inside = sum(1 for r in unknowns if r["inside_call"])
        print(
            f"unknown rows: {len(unknowns)}; inside some earlier call's window: {inside}; "
            f"with a latest hand-launch before the write: {with_latest}; "
            f"with exactly one launcher naming the file or its dir: {with_naming}"
        )
        pairs = Counter((r["latest_launcher"], r["k"]) for r in unknowns)
        print(
            "  (latest launcher -> landing step): rows "
            + ", ".join(
                f"{a}->{b}: {n}"
                for (a, b), n in sorted(pairs.items(), key=lambda kv: kv[0][1] or 0)[:25]
            )
        )
    shown = 0
    for r in bad:
        if r["is_cache"] and not verbose:
            continue
        if shown >= 40:
            print(f"  ... {len(bad) - shown} more")
            break
        shown += 1
        sk = by_idx[r["k"]]
        extra = {k: v for k, v in r.items() if k in ("candidates", "inside_call", "open_hand")}
        print(
            f"  {r['attr']:16s} {r['path']} landed step {r['k']} ({r['tool']} "
            f"{sk[6].strftime('%H:%M:%S') if sk[6] else '?'}-"
            f"{sk[7].strftime('%H:%M:%S') if sk[7] else '?'}) "
            f"write {r['m'].strftime('%H:%M:%S')} {extra}"
        )
    return summary


def main() -> None:
    args = sys.argv[1:]
    verbose = "--verbose" in args
    args = [a for a in args if a != "--verbose"]
    store_dir, mtimes_dir, jobs_dir, *names = args
    con = duckdb.connect(str(Path(store_dir) / "record.duckdb"), read_only=True)
    totals = Counter()
    for name in names:
        s = evaluate(con, name, Path(mtimes_dir), Path(jobs_dir), verbose)
        for key, n in s["attr"].items():
            totals[key] += n
        totals[("late", False)] += s["late"]
    print("\n=== totals (attr, is_cache) ===")
    for key, n in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"  {key}: {n}")


if __name__ == "__main__":
    main()
