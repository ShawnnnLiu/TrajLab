---
name: adr
description: "Write or amend an Architecture Decision Record in docs/decisions/ for trajlab. Use this whenever a change touches what a trial records (hook, watcher, backend, postprocess output, store schema, derived columns, query tool, retry loop), whenever the Exp 2 specs or ADR-0014 say \"ADR needed\" or \"decided in a later step\", whenever the user says \"write that up as an ADR\", \"we need an ADR for this\", \"record this decision\", or asks you to decide an open question from the specs. Also use it when a decision changes and the existing ADR needs an amendment rather than a rewrite."
---

# Writing an ADR for trajlab

ADRs are the repo's memory of why things are the way they are.
CLAUDE.md constraint 5 makes them mandatory for anything that changes what a trial records, and the Exp 2 technical spec routes every open question through one.
A missing ADR means the next person re-derives the decision or silently diverges from it, and a trial captured under an undocumented change cannot be cited in the paper.

## Before writing

1. Find the next number. Numbers are assigned on `main`, not on the branch:
   ```
   git fetch origin main --quiet; ls docs/decisions/ | sort | tail -3
   git ls-tree --name-only origin/main docs/decisions/ | sort | tail -3
   ```
   Take the highest across both plus one.
   Numbers can have gaps: ADR-0013 lives on an unmerged branch (`sl/groundtruth-replay`) and `main` jumps from 0012 to 0014.
   Never reuse a number that exists on any branch; if unsure, grep `git branch -a --contains` for the file name.
2. Read the ADR the decision builds on, and the spec section that asked for it.
   Exp 2 open questions are numbered in `docs/experiments/exp2/exp2_technical_spec_trajlab_2026-10-18.md` §8 and in ADR-0014 "Open questions, decided in later steps"; cite the question number in the Context.
3. Check whether this is an amendment.
   If an existing ADR already covers the area and the decision refines it, add an `## Amendment (YYYY-MM-DD)` section to that file instead of a new ADR.
   Fourteen of the thirteen ADRs carry amendments; that is the normal way decisions evolve here.
   A new ADR supersedes an old one only when the old decision is reversed (say so in both files).

## Format

Match the existing files exactly. Title line, status line, then the four sections:

```markdown
# ADR-NNNN: <Decision as a noun phrase>

Status: proposed (YYYY-MM-DD).

## Context

What question came up, where (spec section, meeting, run, ADR number), and what was measured or read.
Cite the facts: a script in scripts/, a corpus_id, a line in docs/harbor-facts.md, Harbor source module.

## Decision

Numbered, bold-led items. Each item is one decision, stated as what the code does, not what it should do.

0. **The scope sentence.** Say what experiment or component this binds.
1. **First decision.** ...

## Consequences

What follows: costs, limits, what the paper must now say, what needs a new corpus_id, what tests cover it.

## Validation (YYYY-MM-DD)

Optional. The acceptance run or measurement that shows the decision holds, with the command and the numbers.
```

Status stays `proposed` until the owner reviews the PR; do not write `accepted` yourself.
Dates are absolute (`2026-10-18`), never "today" or "this week".
One sentence per line in the body, per the owner's Markdown rule.

## What goes in Decision vs Consequences

Decision items are things a reader can check against the code.
"Deltas come from checkpoint top layers" is a decision; "this keeps the store independent of the change detector" is a consequence.
When a decision changes what a trial records, say so in Consequences and name the `corpus_id` bump: "runs after this ADR use a new corpus_id".

## After writing

- If the ADR affects the paper's claims or threats, add or update the entry in `docs/paper-notes.md` in the same PR (see the `post-run` skill for its shape).
- If a glossary term is introduced or changed, update `docs/glossary.md`.
- If the decision is a workaround for something Harbor lacks, add the note to `docs/upstream-notes.md` too (CLAUDE.md constraint 1).
- Mention the ADR number in the commit message and in the PR description.
- Reference it from CLAUDE.md's layout table only if it introduces a module.

## Example

Input: step 3 of Exp 2 settles how `args_json` is normalized for `is_loop` (spec §8 question 2).
Output: `docs/decisions/0015-derived-columns.md`, Context citing spec §3.3 and ADR-0014 open question 1, Decision items for the normalization rule, recompute-vs-append on ingest, and whether derived columns ship in the `record-files` bundle (question 7), Consequences naming the `SCHEMA_VERSION` bump and the version string recorded in the store manifest.
