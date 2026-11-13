---
name: figures
description: "Make a paper or demo figure from trajlab run data (repair rates, cost per arm, checkpoint timings, store sizes, Exp 2 repair-rate-at-N curves) with the repo's conventions on top of the built-in dataviz skill. Use this whenever the user asks for a figure, plot, chart, or graph from a job dir, a report JSON, or the store; whenever a research or findings note needs a figure; and whenever an existing figure script needs a new arm, run, or metric added. Figures are produced by a dated script, never by hand in a notebook."
---

# Figures for trajlab

Every figure in the paper must be regenerable from a committed script and a cited `corpus_id`.
The built-in `dataviz` skill governs the form of the chart (which mark, how to color, how to label).
This skill governs where the data comes from, how the script is built, and where the output goes.
Read `dataviz` before writing the first line of plotting code; then follow the rules below.

## Data

- Figures read report output, not raw trial dirs: `repair-report.json`, `report.json` from the trial report script, `trajlab exp2 score` output, or a store query.
  If the number does not exist in a report yet, add it to the report script first, so the figure and the text cite the same source.
- Numbers are never retyped. The script reads them; the findings note pastes what the script printed.
- Rows without a reward (infra errors) are left out of rates and said so in the caption; rows with unreadable sessions are left out of token costs, likewise.

## Script

- `scripts/YYYY-MM-DD_<topic>_figures.py` with the inline-metadata header and `uv run --script`, as `2026-10-09_repair_figures.py` does; matplotlib with the `Agg` backend, numpy, scipy when a test is drawn.
- One `--out` directory argument; the script writes every figure as both PNG at 300 dpi and PDF, named by what it shows (`resolution_rate`, `cost`, `repair_rate_at_n`).
- Fixed seeds for bootstrap and permutation, stated in the docstring, so the figure is reproducible.
- Arm ids map to paper labels in one dict at the top of the script (`fresh` is "Baseline", `record-engine` will be the proposed system); ticks may wrap, legends do not.
- Statistical annotations come from the same script: the test named in the ADR (Wilcoxon paired by failure for Exp 1; McNemar or mixed logistic for Exp 2's primary comparison), with Holm adjustment over multiple pairs, and a bootstrap interval over failures for the effect.

## Form, beyond dataviz

- Paper figures are read in grayscale print as often as on screen: pick a palette that survives both, and never encode an arm by color alone.
- Error bars are intervals over failures (the unit of pairing), not over repairs.
- Per-arm sample sizes go in the caption or on the axis, not in the title.
- Keep the figure legible at a single column width: at most five arms per axis, short tick labels, no inset tables.

## Output and citation

- Output directory: `docs/research/figures/YYYY-MM-DD_<topic>/` (Exp 2: `docs/experiments/exp2/figures/...`), committed.
- The findings note that uses the figure carries a "Regenerate with" line holding the exact command, then each figure as `**Figure N.** caption` followed by the image link, as `2026-10-09_tb40-repair-v2-findings.md` does.
- When a figure's numbers back a claim, add the evidence line in `docs/paper-notes.md` (see the `post-run` skill).
