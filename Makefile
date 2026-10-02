.PHONY: sync test lint fmt corpus watch postprocess

sync:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

fmt:
	uv run ruff format src tests scripts
	uv run ruff check --fix src tests scripts

# manual, needs Docker and ANTHROPIC_API_KEY
corpus:
	uv run trajlab run configs/harbor/$(CONFIG).json

BACKEND ?= docker_commit
# Required: which calls to checkpoint (ADR-0010, ADR-0007), e.g. make watch GATE=change EVERY=1.
GATE ?=
EVERY ?=

watch:
	uv run trajlab watch corpus/jobs --backend $(BACKEND) --every $(EVERY) --gate $(GATE)

postprocess:
	uv run trajlab postprocess corpus/jobs/$(JOB)
