"""Exp 2's retry loop (technical spec §5; ADR-0016, ADR-0022): `trajlab exp2 prereg | run`.

This package orchestrates the other packages and sits where `cli.py` does: it may import
`contracts`, `capture`, `atif`, `store`, and Harbor, and nothing in `src/` but `cli.py` imports
it (ADR-0022). An attempt is an ordinary `trajlab run` of one trial, so it is captured exactly as
a corpus trial is (watcher, write recorder, checkpoints), and its agent is
`trajlab.capture.repair:RepairClaudeCode` with the retry prompt as its note.

- `prompt`: the retry prompt and its hash; `leaks`: the check that it names no check and quotes
  no verifier text.
- `arms`: the three arms' settings and an attempt's Harbor job config, the bundle mount included.
- `chain`: a retry chain's directory, its logs, and its store (seeded from the corpus store,
  then each failed retry ingested).
- `loop`: the state machine over chains, resumable from the logs.
- `prereg`: the preregistration file and the check that it is committed.
"""
