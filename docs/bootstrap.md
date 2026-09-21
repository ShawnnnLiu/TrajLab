# Bootstrap: from empty directory to first green commit

Do these by hand, in order, before handing the repo to Claude Code. Estimated time: one hour, plus Docker image pulls.

## 0. Prerequisites on the machine

- Docker Desktop (or Docker Engine) running; `docker info` works without sudo.
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- `gh` installed and logged in (`gh auth status`).
- Claude Code installed locally (`claude --version`); an `ANTHROPIC_API_KEY` with budget for corpus runs.

## 1. Create the repo

```bash
mkdir trajlab && cd trajlab
uv init --python 3.12 --package --name trajlab
uv add harbor pydantic typer watchdog
uv add --group dev ruff pytest pre-commit
uv run harbor --version            # note it; pin it exactly in pyproject.toml
```

Copy in, from this bundle: `CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `.gitignore`, `.env.example`, `Makefile`, `.pre-commit-config.yaml`, `docs/`, `configs/claude-code/settings.hooks.json`. Create the empty packages:

```bash
mkdir -p src/trajlab/{contracts,capture,checkpoint/hook,checkpoint/backends,atif,util} \
         configs/harbor configs/tasks scripts notebooks tests/fixtures corpus/manifests corpus/jobs docs/decisions
touch src/trajlab/{contracts,capture,checkpoint,checkpoint/backends,atif,util}/__init__.py
```

Edit `pyproject.toml`: pin `harbor==<version>`, add `[project.scripts] trajlab = "trajlab.cli:app"`, `[tool.ruff] line-length = 100`.

```bash
uv sync
uv run pre-commit install
git init -b main
git add -A && git commit -m "Scaffold capture repo"
gh repo create trajlab --private --source=. --push
```

On GitHub: Settings → Collaborators → add the three teammates. Settings → Branches → add a rule for `main`: require a pull request, one approval, no force pushes.

## 2. Reference copy of Harbor, outside the repo

```bash
cd .. && git clone --depth 1 https://github.com/harbor-framework/harbor.git harbor-ref && cd trajlab
```

Claude Code reads the installed package in `.venv/`, but the reference clone has the RFC (`rfcs/0001-trajectory-format.md`), docs (`docs-mintlify/`), and examples (`examples/tasks/`). Keep it a sibling directory, never inside the repo.

## 3. Prove Harbor works on this machine (stock, no trajlab code)

```bash
cp .env.example .env                    # add ANTHROPIC_API_KEY
set -a && source .env && set +a
uv run harbor run -t hello-world/hello-world -a claude-code -m anthropic/<model> -e docker
uv run harbor view jobs                 # open http://127.0.0.1:8080, click the trial
```

If this fails, nothing else matters; fix it first. Common causes: Docker not running, image pull blocked, API key missing, Node install in the container failing on a slow network.

## 4. Make the fixture

Copy the hello-world trial dir to `tests/fixtures/hello-world-trial/`. Delete `agent/sessions/debug/`, `agent/sessions/shell-snapshots/`, `agent/sessions/statsig/`, anything over 200 KB, and grep the remainder for the API key (`grep -r sk-ant tests/fixtures` must return nothing). Commit it. Every unit test runs against this directory and nothing else.

## 5. Confirm dataset names before writing a job config

```bash
uv run harbor datasets list
```

Write `configs/harbor/tb_subset_v0.json` with the dataset name exactly as listed, `claude-code`, the model, `n_attempts`, `n_concurrent`, `environment.type = "docker"`, and a `tasks` filter for the subset in `configs/tasks/tb_subset_v0.txt`. Job config schema: `harbor.models.job.config.JobConfig`; Harbor's docs page `core-concepts/jobs/configs` has examples.

## 6. Hand off to Claude Code

```bash
claude
> Read CLAUDE.md and docs/harbor-facts.md, then implement build-order step 1 (scaffold) and stop.
```

Work one build-order step per session or per PR. Review the diff before merging; the ADRs in `docs/decisions/` are where design disagreements get settled, not in chat.

## 7. First corpus run (Sep 25 target)

Stock Harbor, no hooks, no watcher:

```bash
uv run trajlab run configs/harbor/tb_subset_v0.json     # once step 5 of the build order exists
# or, before that exists:
uv run harbor run -c configs/harbor/tb_subset_v0.json
uv run trajlab manifest corpus/jobs/<job>
```

Commit the manifest under `corpus/manifests/`. Upload or copy the job dir to wherever `corpus/README.md` says the data lives (shared drive, or `harbor hub upload` if the team wants the viewer online). Do not commit the job dir.
