# Contributing

- `uv sync`, then `uv run pre-commit install`. Ruff formats and lints on commit.
- One branch per issue, named `<initials>/<short-topic>`. PRs into `main`, one reviewer, squash merge.
- `make test` must pass. Tests run offline against `tests/fixtures/hello-world-trial/`, never against `corpus/`, Docker, or the network.
- Anything that changes what a trial records (hook, watcher, backend, postprocess) needs an ADR in `docs/decisions/` and a new `corpus_id` for the next run.
- Never commit `corpus/jobs/`, `.env`, session JSONLs outside the fixture, or notebook outputs.
- `harbor` is pinned to an exact version in `pyproject.toml`. Bump it in its own PR and note the version in the next corpus manifest.
- Scripts in `scripts/` are dated and disposable. If one is run twice, promote it to a `cli.py` command.
- Working with Claude Code: it reads `CLAUDE.md` automatically. One build-order step per PR. Review the diff; disagreements go into an ADR, not chat.
