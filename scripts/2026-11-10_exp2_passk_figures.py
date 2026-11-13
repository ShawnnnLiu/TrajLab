# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib>=3.9", "numpy>=2"]
# ///
"""Figure of the pass@k probe of 2026-11-10: where each attempt's verifier score landed, per task
and arm (run note `docs/experiments/exp2/2026-11-10_passk-probe.md`).

Reads the JSON that `scripts/2026-11-10_exp2_passk_chains.py report` printed (`--report`) and
writes one figure, as PNG (300 dpi) and PDF, to `--out`:

    verifier_score_distribution   one panel per task, one row per arm, one mark per counted
                                  attempt at its verifier score; the shaded band is the scores
                                  that pass the task; under a row, a line over the middle half
                                  of the arm's attempts (first to third quartile) with a tick at
                                  the median, drawn where the score is continuous and the arm has
                                  at least `MIN_FOR_QUARTILES` attempts

The score is the verifier's own number for the task (`TASKS`): hidden tests passed of 15
(mvcc-lsm-compaction, `checks_passed`), the geometric-mean speedup over NetworkX
(vf2-speedup-networkx, `graded`), and `combined` (freecad-platform-drawing, `graded`). Attempts
the harness failed are not counted attempts and are not drawn. An arm's attempts are pooled over
its chains, and the attempts of one chain are not independent: in record-engine each attempt
could read the ones before it.

Marks that would overlap are moved apart by a fixed rule (`spread`): attempts with the same score
form a block centred on it, and the others move up and down within the row first, then sideways.
Nothing is random.

Prints JSON to stdout: per task and arm, the attempts drawn, the minimum, quartiles, and maximum
of the score, and the attempts that passed.

    uv run --script scripts/2026-11-10_exp2_passk_figures.py \
        --report corpus/exp2/passk/report.json \
        --out docs/experiments/exp2/figures/2026-11-10_passk
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

ARMS = ("control", "record-engine")
LABEL = {"control": "Control (no record)", "record-engine": "Record engine (growing record)"}
TICK = {"control": "Control", "record-engine": "Record engine"}
# Categorical slots 1 and 2 of the dataviz reference palette, checked on white with its
# validator (CVD and normal-vision separation, contrast); the marker shape repeats the arm.
COLOR = {"control": "#2a78d6", "record-engine": "#eb6834"}
MARKER = {"control": "o", "record-engine": "D"}
MARKER_AREA = {"control": 30.0, "record-engine": 22.0}
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, BAND, SURFACE = "#e1e0d9", "#c3c2b7", "#f0efec", "#ffffff"
STEP_PT = 6.6  # centre-to-centre distance of two marks that touch
LEVELS = 2  # marks stack this many steps above and below the row's centre before going sideways
MIN_FOR_QUARTILES = 8
ROW = {"control": 1.0, "record-engine": 0.0}


@dataclass(frozen=True)
class Task:
    name: str
    field: str
    axis: str
    xlim: tuple[float, float]
    ticks: tuple[float, ...]
    tick_labels: tuple[str, ...]
    passes_from: float
    quartiles: bool


TASKS = (
    Task(
        "mvcc-lsm-compaction",
        "checks_passed",
        "Hidden tests passed, of 15",
        (10.4, 15.6),
        (11, 12, 13, 14, 15),
        ("11", "12", "13", "14", "15"),
        14.5,
        False,  # four values: the blocks are the distribution
    ),
    Task(
        "vf2-speedup-networkx",
        "graded",
        "Geometric-mean speedup over NetworkX",
        (0, 13_600),
        (0, 2_500, 5_000, 7_500, 10_000, 12_500),
        ("0", "2.5K", "5K", "7.5K", "10K", "12.5K"),
        5_000,
        True,
    ),
    Task(
        "freecad-platform-drawing",
        "graded",
        "Combined score",
        (0.15, 1.05),
        (0.2, 0.4, 0.6, 0.8, 1.0),
        ("0.2", "0.4", "0.6", "0.8", "1.0"),
        0.9,
        True,
    ),
)


def scores(report: dict[str, Any], task: Task, arm: str) -> tuple[list[float], int]:
    """The arm's counted attempts' scores on the task, and how many of them passed."""
    attempts = [
        attempt
        for chain in report["chains"]
        if chain["task"] == task.name and chain["arm"] == arm
        for attempt in chain["attempts"]
    ]
    values = [float(a[task.field]) for a in attempts if a.get(task.field) is not None]
    return values, sum(a["outcome"] == "passed" for a in attempts)


def spread(xs_pt: list[float]) -> list[tuple[float, float]]:
    """An offset in points for each mark, so that no two overlap. Marks at one score form a
    block centred on it, in columns of at most `2 * LEVELS + 1`; every other mark takes the
    nearest free place on a grid of `STEP_PT`, up and down to `LEVELS` first and then sideways."""
    rows = 2 * LEVELS + 1
    taken: list[tuple[float, float]] = []
    out: dict[int, tuple[float, float]] = {}

    def free(x: float, y: float) -> bool:
        return all((x - tx) ** 2 + (y - ty) ** 2 >= (STEP_PT * 0.98) ** 2 for tx, ty in taken)

    groups: dict[float, list[int]] = {}
    for i, x in enumerate(xs_pt):
        groups.setdefault(round(x, 3), []).append(i)
    sideways = [side * n * STEP_PT for n in range(9) for side in (1, -1)][1:]
    for x, members in sorted(groups.items(), key=lambda group: (-len(group[1]), group[0])):
        if len(members) == 1:
            continue
        columns = -(-len(members) // rows)
        per_column = -(-len(members) // columns)
        cells = []
        for k in range(len(members)):
            column, row = divmod(k, per_column)
            height = min(per_column, len(members) - column * per_column)
            cells.append(
                ((column - (columns - 1) / 2) * STEP_PT, (row - (height - 1) / 2) * STEP_PT)
            )
        shift = next(s for s in sideways if all(free(x + s + dx, dy) for dx, dy in cells))
        for i, (dx, dy) in zip(members, cells, strict=True):
            taken.append((x + shift + dx, dy))
            out[i] = (shift + dx, dy)
    places = sorted(
        ((dx, dy) for dx in range(-8, 9) for dy in range(-LEVELS, LEVELS + 1)),
        key=lambda p: (abs(p[0]), abs(p[1]), p[1] < 0, p[0] < 0),
    )
    for i in sorted((i for i in range(len(xs_pt)) if i not in out), key=lambda i: xs_pt[i]):
        dx, dy = next(p for p in places if free(xs_pt[i] + p[0] * STEP_PT, p[1] * STEP_PT))
        taken.append((xs_pt[i] + dx * STEP_PT, dy * STEP_PT))
        out[i] = (dx * STEP_PT, dy * STEP_PT)
    return [out[i] for i in range(len(xs_pt))]


def draw(report: dict[str, Any], out: Path) -> dict[str, Any]:
    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
            "font.size": 8.5,
            "pdf.fonttype": 42,
        }
    )
    width, height = 7.0, 6.9
    fig, axes = plt.subplots(len(TASKS), 1, figsize=(width, height), facecolor=SURFACE)
    fig.subplots_adjust(left=0.19, right=0.975, top=0.905, bottom=0.075, hspace=0.72)
    stats: dict[str, Any] = {}
    for ax, task in zip(axes, TASKS, strict=True):
        box = ax.get_position()
        width_pt, height_pt = box.width * width * 72, box.height * height * 72
        x0, x1 = task.xlim
        y0, y1 = -0.62, 1.5
        per_x, per_y = (x1 - x0) / width_pt, (y1 - y0) / height_pt
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_facecolor(SURFACE)
        ax.axvspan(task.passes_from, x1, color=BAND, lw=0, zorder=0)
        ax.text(
            x1 - 4 * per_x,
            y1 - 9 * per_y,
            "passes",
            ha="right",
            va="center",
            color=MUTED,
            fontsize=7.5,
        )
        labels = []
        for arm in ARMS:
            values, passed = scores(report, task, arm)
            offsets = spread([(v - x0) / per_x for v in values])
            ax.scatter(
                [v + dx * per_x for v, (dx, _) in zip(values, offsets, strict=True)],
                [ROW[arm] + dy * per_y for _, dy in offsets],
                s=MARKER_AREA[arm],
                marker=MARKER[arm],
                color=COLOR[arm],
                edgecolors=SURFACE,
                linewidths=0.7,
                zorder=3,
                clip_on=False,
            )
            q1, median, q3 = (float(q) for q in np.percentile(values, [25, 50, 75]))
            if task.quartiles and len(values) >= MIN_FOR_QUARTILES:
                under = ROW[arm] - (LEVELS + 1.1) * STEP_PT * per_y
                ax.plot([q1, q3], [under, under], color=COLOR[arm], lw=1.5, solid_capstyle="butt")
                tick = 3.2 * per_y
                ax.plot([median, median], [under - tick, under + tick], color=COLOR[arm], lw=1.5)
            labels.append(f"{TICK[arm]}\n{len(values)} attempts")
            stats.setdefault(task.name, {})[arm] = {
                "attempts": len(values),
                "passed": passed,
                "min": min(values),
                "q1": q1,
                "median": median,
                "q3": q3,
                "max": max(values),
            }
        ax.set_yticks([ROW[arm] for arm in ARMS], labels)
        ax.set_xticks(task.ticks, task.tick_labels)
        ax.tick_params(axis="y", length=0, labelcolor=INK_2, pad=8)
        ax.tick_params(axis="x", length=0, labelcolor=MUTED, pad=4)
        ax.grid(axis="x", color=GRID, lw=0.6, zorder=1)
        ax.set_axisbelow(True)
        for side in ("left", "right", "top"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(AXIS)
        ax.spines["bottom"].set_linewidth(0.8)
        ax.set_title(task.name, loc="left", color=INK, fontsize=9.5, fontweight="bold", pad=7)
        ax.set_xlabel(task.axis, color=INK_2, fontsize=8.5, labelpad=3)
    keys = [
        Line2D([], [], marker=MARKER[arm], ls="", color=COLOR[arm], ms=5.5, label=LABEL[arm])
        for arm in ARMS
    ]
    keys.append(
        Line2D([], [], color=MUTED, lw=1.5, marker="|", ms=7, label="middle half, tick at median")
    )
    fig.legend(
        handles=keys,
        loc="upper left",
        bbox_to_anchor=(0.012, 0.995),
        ncols=3,
        frameon=False,
        fontsize=8.5,
        labelcolor=INK_2,
        handletextpad=0.5,
        columnspacing=1.8,
    )
    out.mkdir(parents=True, exist_ok=True)
    fig.savefig(out / "verifier_score_distribution.png", dpi=300, facecolor=SURFACE)
    fig.savefig(out / "verifier_score_distribution.pdf", facecolor=SURFACE)
    plt.close(fig)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(draw(json.loads(args.report.read_text()), args.out), indent=1))


if __name__ == "__main__":
    main()
