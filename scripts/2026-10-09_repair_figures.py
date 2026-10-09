# /// script
# requires-python = ">=3.12"
# dependencies = ["matplotlib>=3.9", "numpy>=2", "scipy>=1.14"]
# ///
"""Figures of repair round 1 (ADR-0012), all five arms: resolution rate, cost, and the tests.

Reads `repair-report.json` (round 1's four arms) and `repair-report.traj-text.json` (the fifth arm)
that `scripts/2026-10-02_repair_report.py` wrote next to the source job, and writes three figures,
each as PNG (300 dpi) and PDF, to `--out`:

    resolution_rate     repairs that passed, pooled over each arm's repairs with a reward
    cost                (a) mean agent seconds per repair, (b) mean own tokens per repair
    resolution_pvalues  (a) each arm against `fresh`, (b) all ten pairs, plus the omnibus test

Tokens are the repair's own (input, cache writes, cache reads, output), as in the report; rows with
`sessions_unreadable` are left out of (b). Infra errors (no reward) are left out of the rates.

Tests, on per-failure rates (each failure's mean reward over its repairs), paired by failure:
- each arm against `fresh`: exact Wilcoxon signed-rank (ADR-0012 decision 8's primary test), with a
  95% bootstrap interval over failures for the mean difference;
- all ten pairs: the same test, Holm-adjusted over the ten;
- omnibus: the Friedman statistic, with a permutation p from shuffling arms within each failure.
Bootstrap and permutation use a fixed seed, so the figures are reproducible.

    uv run --script scripts/2026-10-09_repair_figures.py corpus/jobs/tb40-sonnet-v2 \
        --out docs/research/figures/2026-10-09_repair-round1
"""

import argparse
import itertools
import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LogNorm  # noqa: E402
from scipy.stats import rankdata  # noqa: E402

ARMS = ("fresh", "traj-text", "state", "traj", "state-traj")
LABEL = {
    "fresh": "Baseline",
    "traj-text": "Trajectory as text",
    "state": "No trajectory + failed env.",
    "traj": "Trajectory + fresh env.",
    "state-traj": "Trajectory + failed env.",
}
TICK = {
    "fresh": "Baseline",
    "traj-text": "Trajectory\nas text",
    "state": "No trajectory +\nfailed env.",
    "traj": "Trajectory +\nfresh env.",
    "state-traj": "Trajectory +\nfailed env.",
}
TOKENS = ("tok_input", "tok_cache_write_5m", "tok_cache_write_1h", "tok_cache_read", "tok_output")
BLUE, ORANGE = "#1f77b4", "#ff7f0e"
BOOTSTRAP, PERMUTATIONS, SEED = 20_000, 200_000, 0

Rows = dict[str, dict[str, list[dict]]]  # arm -> source trial -> repair rows


def load(source: Path) -> Rows:
    rows = json.loads((source / "repair-report.json").read_text())
    rows += [
        r
        for r in json.loads((source / "repair-report.traj-text.json").read_text())
        if r["arm"] == "traj-text"
    ]
    by: Rows = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["arm"] in ARMS:
            by[r["arm"]][r["source"]].append(r)
    return by


def pooled_mean(by: Rows, arm: str, value) -> float:
    vals = [v for reps in by[arm].values() for r in reps if (v := value(r)) is not None]
    return sum(vals) / len(vals)


def wilcoxon_exact(d: np.ndarray) -> float:
    """Two-sided exact signed-rank p over all sign flips; zero differences are dropped."""
    d = d[d != 0]
    if not len(d):
        return 1.0
    ranks = rankdata(np.abs(d))
    observed = abs(ranks[d > 0].sum() - ranks[d < 0].sum())
    signs = np.array(list(itertools.product((1, -1), repeat=len(d))))
    return float(np.mean(np.abs(signs @ ranks) >= observed - 1e-9))


def holm(ps: np.ndarray) -> np.ndarray:
    adjusted, running = np.empty(len(ps)), 0.0
    for i, j in enumerate(np.argsort(ps)):
        running = max(running, (len(ps) - i) * ps[j])
        adjusted[j] = min(1.0, running)
    return adjusted


def friedman(m: np.ndarray) -> float:
    n, k = m.shape
    ranks = np.apply_along_axis(rankdata, 1, m)
    return 12 / (n * k * (k + 1)) * (ranks.sum(0) ** 2).sum() - 3 * n * (k + 1)


def style() -> None:
    plt.rcParams.update(
        {
            "font.family": "serif",
            "font.serif": ["STIXGeneral", "DejaVu Serif"],
            "mathtext.fontset": "stix",
            "font.size": 11,
            "axes.labelsize": 13,
            "axes.labelweight": "bold",
            "axes.titlesize": 14,
            "axes.titleweight": "bold",
            "xtick.labelsize": 11,
            "ytick.labelsize": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.linewidth": 0.8,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": "#dddddd",
            "grid.linewidth": 0.6,
            "axes.axisbelow": True,
            "savefig.dpi": 300,
        }
    )


def save(fig: plt.Figure, out: Path, name: str) -> None:
    for ext in ("png", "pdf"):
        fig.savefig(out / f"{name}.{ext}", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def bars(ax: plt.Axes, values: list[float], color: str, fmt: str, pad: float) -> None:
    x = np.arange(len(ARMS))
    ax.bar(x, values, width=0.6, color=color, edgecolor="black", linewidth=0.6)
    for xi, v in zip(x, values, strict=True):
        ax.text(xi, v + pad, fmt.format(v), ha="center", va="bottom", fontsize=10)
    ax.set_xticks(x, [TICK[a] for a in ARMS], fontweight="bold")
    ax.tick_params(axis="x", length=0)
    ax.set_xlabel("Repair arm")


def resolution_rate(by: Rows, out: Path) -> None:
    rates = [100 * pooled_mean(by, a, lambda r: r["reward"]) for a in ARMS]
    fig, ax = plt.subplots(figsize=(7, 4))
    bars(ax, rates, BLUE, "{:.1f}", 0.6)
    ax.set_ylim(0, 32)
    ax.set_ylabel("Resolution rate (%)")
    ax.set_title("Repair Resolution Rate by Arm")
    save(fig, out, "resolution_rate")


def cost(by: Rows, out: Path) -> None:
    seconds = [pooled_mean(by, a, lambda r: r["agent_s"]) for a in ARMS]
    tokens = [
        pooled_mean(
            by,
            a,
            lambda r: None if r["sessions_unreadable"] else sum(r[k] or 0 for k in TOKENS) / 1e6,
        )
        for a in ARMS
    ]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.2))
    bars(a1, seconds, BLUE, "{:.0f}", max(seconds) * 0.015)
    a1.set_ylim(0, max(seconds) * 1.15)
    a1.set_ylabel("Mean agent time per repair (s)")
    a1.set_title("(a) Execution Time")
    bars(a2, tokens, ORANGE, "{:.2f}", max(tokens) * 0.015)
    a2.set_ylim(0, max(tokens) * 1.15)
    a2.set_ylabel("Mean tokens per repair (millions)")
    a2.set_title("(b) Token Usage")
    fig.suptitle("Repair Cost by Arm", fontsize=15, fontweight="bold")
    fig.tight_layout(w_pad=3)
    save(fig, out, "cost")


def resolution_pvalues(by: Rows, out: Path) -> None:
    rng = np.random.default_rng(SEED)
    fails = sorted(by["fresh"])
    rate = {
        a: np.array(
            [np.mean([r["reward"] for r in by[a][f] if r["reward"] is not None]) for f in fails]
        )
        for a in ARMS
    }
    m = np.column_stack([rate[a] for a in ARMS])
    observed = friedman(m)
    perm = np.array(
        [friedman(np.array([rng.permutation(r) for r in m])) for _ in range(PERMUTATIONS)]
    )
    p_omnibus = (np.sum(perm >= observed - 1e-9) + 1) / (PERMUTATIONS + 1)
    boot = rng.integers(0, len(fails), (BOOTSTRAP, len(fails)))
    pairs = list(itertools.combinations(range(len(ARMS)), 2))
    raw = np.array([wilcoxon_exact(rate[ARMS[i]] - rate[ARMS[j]]) for i, j in pairs])
    adjusted = holm(raw)

    plt.rcParams.update({"axes.grid": False, "ytick.labelsize": 11})
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.2), gridspec_kw={"width_ratios": [1, 1.05]})

    # (a) Difference from baseline, 95% bootstrap interval, exact Wilcoxon p.
    arms = ARMS[1:]
    for y, a in zip(range(len(arms) - 1, -1, -1), arms, strict=True):
        d = 100 * (rate[a] - rate["fresh"])
        lo, hi = np.percentile(d[boot].mean(1), [2.5, 97.5])
        a1.errorbar(d.mean(), y, xerr=[[d.mean() - lo], [hi - d.mean()]], fmt="none",
                    ecolor="black", elinewidth=1.2, capsize=6, capthick=1.2)  # fmt: skip
        a1.plot(d.mean(), y, "s", ms=9, color=ORANGE if d.mean() < 0 else BLUE,
                mec="black", mew=0.8, zorder=3)  # fmt: skip
        a1.text(47, y, f"p = {wilcoxon_exact(d):.2f}", va="center", ha="left", fontsize=11)
    a1.axvline(0, color="#777777", lw=1, ls="--")
    a1.set_yticks(range(len(arms) - 1, -1, -1), [LABEL[a] for a in arms], fontweight="bold")
    a1.set_xlim(-25, 45)
    a1.set_ylim(-0.6, len(arms) - 0.4)
    a1.set_xlabel("Difference from baseline (percentage points)")
    a1.set_title("(a) Each Arm vs. Baseline")
    a1.spines["left"].set_visible(False)
    a1.tick_params(axis="y", length=0)
    a1.grid(axis="x", color="#e3e3e3", lw=0.6)

    # (b) Pairwise: Holm-adjusted p, raw p in parentheses.
    grid = np.full((len(ARMS), len(ARMS)), np.nan)
    for (i, j), r, h in zip(pairs, raw, adjusted, strict=True):
        grid[j, i] = h
        a2.text(i, j, f"{h:.2f}\n({r:.3f})", ha="center", va="center", fontsize=10.5,
                color="white" if h < 0.05 else "black")  # fmt: skip
    cmap = plt.get_cmap("Blues_r").copy()
    cmap.set_bad("white")
    im = a2.imshow(grid, cmap=cmap, norm=LogNorm(vmin=0.01, vmax=1))
    a2.set_xticks(range(4), [TICK[a] for a in ARMS[:4]], fontweight="bold", fontsize=10)
    a2.set_yticks(range(1, 5), [TICK[a] for a in ARMS[1:]], fontweight="bold", fontsize=10)
    a2.set_xlim(-0.5, 3.5)
    a2.set_ylim(4.5, 0.5)
    for s in a2.spines.values():
        s.set_visible(False)
    a2.tick_params(length=0)
    a2.set_title("(b) Pairwise Comparisons")
    bar = fig.colorbar(im, ax=a2, fraction=0.04, pad=0.02, ticks=[0.01, 0.05, 0.1, 0.5, 1])
    bar.ax.set_yticklabels(["0.01", "0.05", "0.1", "0.5", "1"])
    bar.set_label("Holm-adjusted p", fontweight="bold")

    fig.suptitle("Statistical Comparison of Resolution Rates", fontsize=16, fontweight="bold")
    fig.text(
        0.5,
        -0.04,
        f"Omnibus test across all five arms (Friedman, within-failure permutation): "
        f"p = {p_omnibus:.3f}.  (a) Mean per-failure difference with 95% bootstrap CI over "
        f"{len(fails)} failures; exact Wilcoxon signed-rank p.\n(b) Exact Wilcoxon signed-rank "
        "on per-failure rates; cells show Holm-adjusted p over 10 comparisons, raw p in "
        "parentheses.",
        ha="center",
        fontsize=10.5,
    )
    fig.tight_layout(w_pad=4)
    save(fig, out, "resolution_pvalues")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="source job dir, e.g. corpus/jobs/tb40-sonnet-v2")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    by = load(args.source)
    style()
    resolution_rate(by, args.out)
    cost(by, args.out)
    resolution_pvalues(by, args.out)


if __name__ == "__main__":
    main()
