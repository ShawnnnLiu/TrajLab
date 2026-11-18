"""Count the attempts of each Exp 2 chain whose verifier outcome repeats an earlier one.

For the run notes of the chain probes
(`docs/experiments/exp2/2026-11-15_roots2-replication-chains.md` counted this inline; this script
is that rule, kept). Offline: reads the plan, the JSON that
`scripts/2026-11-10_exp2_passk_chains.py report` printed, and the trial dirs.

An attempt repeats when its verifier outcome equals that of its chain's root or of an earlier
attempt of the same chain. The outcome is, for a task whose verifier writes
`verifier/reward_details.json` (freecad), the four reason strings of that file; otherwise the set
of failing tests, parameters included, from the `FAILED` lines of `verifier/test-stdout.txt`. A
passed attempt is never a repeat. `same_answer` is a second, stricter count for
foodstuff-beta-activity only: the attempt's `artifacts/app/results.txt` equals, byte for byte, the
root's or an earlier attempt's.

Prints JSON: per chain the 0/1 flags by attempt, and per task and arm the sums.

    uv run python scripts/2026-11-16_exp2_chain_repeats.py corpus/exp2/roots3/plan.json \
        corpus/exp2/roots3/report.json --jobs-dir corpus/exp2/roots3-jobs
"""

import argparse
import json
import re
from pathlib import Path

FAILED = re.compile(r"^FAILED \S*?([^/\s]+::.+?)(?: - .*)?$", re.MULTILINE)
ANSWER = Path("artifacts/app/results.txt")


def outcome(trial: Path) -> str | None:
    details = trial / "verifier" / "reward_details.json"
    if details.is_file():
        found = json.loads(details.read_text())
        return json.dumps(
            [
                part[key]
                for part in (found.get("base") or {}, found.get("target") or {})
                for key in sorted(part)
                if key.endswith("_reason")
            ]
        )
    stdout = trial / "verifier" / "test-stdout.txt"
    if not stdout.is_file():
        return None
    return json.dumps(sorted(set(FAILED.findall(stdout.read_text(errors="replace")))))


def answer(trial: Path) -> str | None:
    path = trial / ANSWER
    return path.read_text(errors="replace") if path.is_file() else None


def only_trial(job: Path) -> Path:
    (trial,) = sorted(job.glob("*__*"))
    return trial


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    args = parser.parse_args()
    root_job = {
        trial: Path(lane["job"])
        for lane in json.loads(args.plan.read_text())["lanes"]
        for trial in lane["trials"]
    }
    chains, totals = [], {}
    for chain in json.loads(args.report.read_text())["chains"]:
        root = root_job[chain["root"]] / chain["root"]
        seen, answers = {outcome(root)}, {answer(root)}
        flags, same = [], []
        for attempt in chain["attempts"]:
            trial = only_trial(args.jobs_dir / attempt["job"])
            found, text = outcome(trial), answer(trial)
            passed = bool(attempt["reward"])
            flags.append(int(not passed and found is not None and found in seen))
            same.append(int(not passed and text is not None and text in answers))
            seen.add(found)
            answers.add(text)
        row = {"task": chain["task"], "root": chain["root"], "arm": chain["arm"], "repeats": flags}
        total = totals.setdefault(
            f"{chain['task']}/{chain['arm']}", {"attempts": 0, "repeats": 0, "same_answer": 0}
        )
        if chain["task"] == "foodstuff-beta-activity":
            row["same_answer"] = same
            total["same_answer"] += sum(same)
        total["attempts"] += len(flags)
        total["repeats"] += sum(flags)
        chains.append(row)
    print(json.dumps({"totals": totals, "chains": chains}, indent=2))


if __name__ == "__main__":
    main()
