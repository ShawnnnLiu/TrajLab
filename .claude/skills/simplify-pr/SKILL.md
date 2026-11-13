---
name: simplify-pr
description: "Run the built-in simplify pass on trajlab changes with the repo's rules about what counts as simpler. Use this whenever the user says \"simplify\", \"clean this up\", \"tighten\", \"is there a simpler way\", or asks for a quality pass on a branch before review; and after any large addition under src/trajlab/store/, repair/, or score/ for Exp 2. Quality only; bugs go through review-pr."
---

# Simplifying trajlab code

The built-in `simplify` skill looks for reuse, simplification, and efficiency cleanups and applies them.
In this repo some things that look like simplifications are regressions, because the package boundaries and the Harbor models are load-bearing.
Run `simplify` on the branch, then check its proposed and applied changes against the rules below, and revert any that cross them.

## Not simpler here

- **Collapsing a `contracts/` model into the module that uses it.** `contracts/` is the only capture code analysis imports; a model moved out of it is a model analysis can no longer see (constraint 2).
- **Importing across `capture/`, `checkpoint/`, `atif/` directly** to avoid a small duplication. They share through `contracts/` only (constraint 3); duplication of a few lines is the price.
- **Redefining a Harbor model** as a lighter dataclass or TypedDict. `Trajectory`, `TrialResult`, `TrialPaths` are reused verbatim (constraint 7), even when only two fields are read.
- **Merging dated scripts** or moving shared helpers between them. Scripts are provenance; they are cited by name and left alone. Shared logic that two scripts need belongs in `src/` with tests, or the second script becomes a CLI command.
- **Replacing an explicit probe with an inference.** Code that asks Docker (`docker ps --filter label=...`) instead of string-building a name is deliberate (`docs/harbor-facts.md`); keep it.
- **Removing a "redundant" check that an ADR made deliberate**, such as the hook's second look for `.ack` after a timeout, the watcher's idempotence on replayed `.req` files, or the store's refusal of a leftover `.wal`. Grep the ADRs for the behavior before deleting it.
- **Dropping the type on a public function** or a pydantic field validator to shorten a signature.

## Simpler here

- One typer command per entry point, with options typed by `Annotated`, over hand-rolled argument handling.
- A pydantic model read with `model_validate_json` over manual `json.loads` plus key access.
- `pathlib` joins over string concatenation; `PurePosixPath` for container paths.
- `logging` with structured messages over f-strings assembled for `print`.
- Fewer layers between the CLI and the function that does the work, as long as the package boundary rule holds.
- Tests that use `conftest.py` helpers (`trial_copy`, `job_dir`, `make_checkpoint`, `make_call`, `write_capture`) over tests that rebuild those by hand.

## After the pass

`make test` and `make lint` must stay green; a simplification that changes an output file (`trajectory.enriched.json`, `checkpoints.jsonl`, store tables) is a recording change and needs an ADR, which usually means it was not a simplification.
