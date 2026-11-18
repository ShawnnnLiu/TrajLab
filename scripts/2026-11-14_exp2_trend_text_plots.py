"""Plain-text score-by-retry plots of the score-trend chains (2026-11-13 run), for a terminal.

Reads the chains report written by `scripts/2026-11-10_exp2_passk_chains.py report`; no Docker.
One panel per task and arm: retry number across, graded score up, a letter per chain.

    uv run python scripts/2026-11-14_exp2_trend_text_plots.py corpus/exp2/trend/report.json
"""

import json
import math
import sys

report = json.load(open(sys.argv[1]))
BUDGET = report["budget"]
ROOT = {
    "production-planning": 19,
    "vf2-speedup-networkx": 2213,
    "gsea-proteomics": 11,
    "mvcc-lsm-compaction": 11,
}
GATE = 5000
STEP = math.sqrt(2)


def score(attempt):
    return attempt["graded"] if attempt["graded"] is not None else attempt["checks_passed"]


def rows_for(task, chains):
    """(label, low, high) per row, top row first; a score s is in a row when low <= s < high."""
    values = [score(a) for c in chains for a in c["attempts"] if score(a) is not None]
    values.append(ROOT[task])
    if task.startswith("vf2"):
        low = GATE
        while low > min(values):
            low /= STEP
        edges = [low]
        while edges[-1] <= max(values):
            edges.append(edges[-1] * STEP)
        rows = [(f"{a:>7,.0f}x", a, b) for a, b in zip(edges, edges[1:], strict=False)]
        return rows[::-1], GATE
    total = chains[0]["attempts"][0]["checks_total"]
    rows = [(f"{v:>5} of {total}"[-8:], v, v + 1) for v in range(int(min(values)), total + 1)]
    return rows[::-1], total


def panel(task, arm, chains, rows, pass_low):
    cells = {}
    for letter, chain in zip("abc", chains, strict=True):
        for a in chain["attempts"]:
            s = score(a)
            if s is None:
                continue
            for i, (_, low, high) in enumerate(rows):
                if low <= s < high:
                    cells.setdefault((i, a["attempt"]), []).append(letter)
    for i, (_, low, high) in enumerate(rows):
        if low <= ROOT[task] < high:
            cells[(i, 0)] = ["r"]
    passes = [c["first_pass"] for c in chains]
    shown = ", ".join("none" if p is None else str(p) for p in passes)
    print(f"  {arm}   first pass at retry: {shown}")
    for i, (label, low, _) in enumerate(rows):
        line = ""
        for k in range(BUDGET + 1):
            marks = cells.get((i, k), [])
            mark = "" if not marks else marks[0] if len(marks) == 1 else str(len(marks))
            line += (mark or ("-" if low >= pass_low else "·")).rjust(3, " ")
        note = "  pass" if math.isclose(low, pass_low) else ""
        print(f"  {label} |{line}{note}")
    print("  " + " " * len(rows[0][0]) + " +" + "".join(f"{k:>3}" for k in range(BUDGET + 1)))


tasks = ["production-planning", "mvcc-lsm-compaction", "gsea-proteomics", "vf2-speedup-networkx"]
for task in tasks:
    chains = [c for c in report["chains"] if c["task"] == task]
    rows, pass_low = rows_for(task, chains)
    print(f"\n=== {task} ===")
    for arm in ("control", "record-engine"):
        mine = sorted((c for c in chains if c["arm"] == arm), key=lambda c: c["run"])
        panel(task, arm, mine, rows, pass_low)
        print()
print("x: retry number (0 = the root).  a, b, c: the arm's three chains.  2, 3: that many chains")
print("at the same score.  r: the root's score.  Rows of dashes are passing scores.")
