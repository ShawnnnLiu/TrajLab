"""ADR-0021 acceptance on captured trials: what the write recorder saw and how it maps to calls.

For each trial dir (built into the store given), prints:

- the recorded events by op, how many found their writer already gone, and the peak rates
  (events in one second; per Bash call, events over the call's own window);
- the `eval '<command>'` check: every Bash call should have a write event whose tool shell
  ran exactly that command (Claude Code's shell writes its cwd file at the end of each call),
  listed when it has none, with whether the call was backgrounded or asked to run in the
  background;
- how events were tied to calls (`trajlab.store.writes.Writers`) and why the rest were not;
- from the store: delta rows by attribution, and the agreement between ADR-0020's rule and the
  record on rows the record settles (`recorded`);
- after the last trial, the totals over all of them.

    uv run python scripts/2026-10-27_adr0021_acceptance.py --store <store> <trial dir>...
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from harbor.models.trial.result import TrialResult

from trajlab.store.schema import connect_agent
from trajlab.store.writes import Writers, command_hashes, read_write_events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("trials", type=Path, nargs="+")
    args = parser.parse_args()
    totals: dict[str, Counter[str]] = {}
    with connect_agent(args.store) as con:
        for trial_dir in args.trials:
            report(con, trial_dir, totals)
    print(f"\n=== totals over {len(args.trials)} trials")
    for name, counter in totals.items():
        print(f"  {name}: " + ", ".join(f"{k} {n}" for k, n in counter.most_common()))


def report(con, trial_dir: Path, totals: dict[str, Counter[str]]) -> None:  # type: ignore[no-untyped-def]
    from trajlab.contracts import StepRow

    result = TrialResult.model_validate_json((trial_dir / "result.json").read_text())
    trial_id = result.id
    names = [c[0] for c in con.execute("SELECT * FROM steps LIMIT 0").description]
    steps = [
        StepRow.model_validate(dict(zip(names, row, strict=True)))
        for row in con.execute(
            "SELECT * FROM steps WHERE trial_id = ? ORDER BY step_idx", [trial_id]
        ).fetchall()
    ]
    events = read_write_events(trial_dir)
    reward = result.verifier_result.rewards if result.verifier_result else None
    print(f"\n=== {trial_dir.name} (reward {reward}): {len(steps)} steps, {len(events)} events")
    total = totals.setdefault
    total("trials", Counter())["with events" if events else "without events"] += 1
    if not events:
        return
    ops = Counter(e.op for e in events)
    gone = sum(e.gone for e in events)
    total("ops", Counter()).update(ops)
    total("writer gone", Counter()).update({"gone": gone, "read": len(events) - gone})
    print("  ops: " + ", ".join(f"{op} {n}" for op, n in sorted(ops.items())))
    print(f"  writer gone at the event: {gone} ({gone / len(events):.1%})")
    per_second = Counter(int(e.at.timestamp()) for e in events)
    print(f"  peak: {max(per_second.values())} events in one second")

    bash = [s for s in steps if s.tool == "Bash"]
    seen = {e.shell.command_sha256 for e in events if e.shell is not None}
    missing = []
    rates = []
    for step in bash:
        command = json.loads(step.args_json or "{}").get("command", "")
        if not command_hashes(command) & seen:
            background = json.loads(step.args_json or "{}").get("run_in_background")
            moved = "moved to the background" in (step.output or "")
            missing.append((step.step_idx, background, moved, command))
        if step.t_start and step.t_end:
            n = sum(1 for e in events if step.t_start <= e.at <= step.t_end)
            seconds = max((step.t_end - step.t_start).total_seconds(), 1.0)
            rates.append((n / seconds, n, seconds, step.step_idx, command))
    print(f"  eval check: {len(bash) - len(missing)} of {len(bash)} Bash calls seen by command")
    eval_check = total("Bash calls", Counter())
    eval_check["seen by command"] += len(bash) - len(missing)
    for step_idx, background, moved, command in missing:
        flags = ("run_in_background " if background else "") + ("moved " if moved else "")
        eval_check["no shell: " + (flags.strip() or "foreground")] += 1
        print(f"    no shell for step {step_idx} {flags}| {command[:90]!r}")
    for rate, n, seconds, step_idx, command in sorted(rates, reverse=True)[:3]:
        print(
            f"  busiest call: step {step_idx}, {n} events in {seconds:.0f} s ({rate:.0f}/s) "
            f"| {command[:60]!r}"
        )

    writers = Writers(steps, events)
    mapped: Counter[str] = Counter()
    for event in events:
        writer = writers.writer(event)
        if writer is not None:
            mapped[writer.mapped_by] += 1
        elif event.gone and event.cmd is None:
            mapped["none: gone, never read"] += 1
        elif event.agent and event.shell is None:
            mapped["none: Claude Code outside a write tool"] += 1
        else:
            mapped["none: other"] += 1
    print("  events by mapping: " + ", ".join(f"{k} {n}" for k, n in mapped.most_common()))
    total("events by mapping", Counter()).update(mapped)

    rows = con.execute(
        "SELECT attribution, count(*) FROM delta_attribution WHERE trial_id = ? "
        "GROUP BY 1 ORDER BY 2 DESC",
        [trial_id],
    ).fetchall()
    print("  delta rows: " + ", ".join(f"{a} {n}" for a, n in rows))
    total("delta rows", Counter()).update(dict(rows))
    agreement = con.execute(
        """
        SELECT rule_attribution, rule_causal_step = causal_step AS agree, count(*)
        FROM delta_attribution WHERE trial_id = ? AND attribution = 'recorded'
        GROUP BY ALL ORDER BY 1, 2
        """,
        [trial_id],
    ).fetchall()
    for rule, agree, n in agreement:
        print(f"    recorded, rule said {rule}: {'agrees' if agree else 'DISAGREES'} on {n}")
        total("recorded rows, rule", Counter())[f"{rule} {'agrees' if agree else 'disagrees'}"] += n


if __name__ == "__main__":
    main()
