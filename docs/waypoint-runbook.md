# Running the Waypoint repair experiment (ADR-0013)

Every command runs from the repo root on the capture server. `trajlab` is `uv run trajlab`.

## 0. One-time server setup

Waypoint needs root, CRIU 4.0 or newer, and Docker on the same machine.

```bash
sudo -n true                      # passwordless sudo must work
waypoint version                  # v0.7.0 or newer
criu --version                    # 4.0 or newer
cat /etc/waypoint/config.json     # must name bash_init_src, e.g. /usr/local/libexec/waypoint/bash_init
sudo mkdir -p /srv/trajlab/waypoint && sudo chmod 700 /srv/trajlab/waypoint
df -h /srv                        # see "Disk" below
```

If Waypoint is missing, install it from `vendor/waypoint` (its `README.md`, "Scripted Setup").

Credentials go in `.env`, as for every run (`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`, plus `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, ADR-0005).

## 1. Checks (ADR-0013, "Checks before the real run")

```bash
# Check 2: three copies of one save, each in its own network, all answer on port 8000.
uv run python scripts/2026-10-08_waypoint_checks.py parallel
#   expect: "copy0: port 8000 -> '200'; internet -> '200'; PASS" for copy0..copy2, then "check 2: PASS"

# Check 1: hello-world on Waypoint.
trajlab run configs/harbor/hello-world-waypoint-v1.json --storage /srv/trajlab/jobs
trajlab waypoint-report corpus/jobs/hello-world-waypoint-v1
uv run python scripts/2026-10-08_waypoint_checks.py save corpus/jobs/hello-world-waypoint-v1/<trial>
#   expect: reward 1.0, a "final" save, and "check 1: PASS" (no /tests, no verifier output in the save)
```

Repeat check 1 with a forced timeout: copy the config with `"agent_timeout_multiplier": 0.01` and a new `job_name`, run it with `--allow-dirty`, and confirm the report shows `timeout_or_error` and a save.

## 2. First attempts

No watcher is needed: Waypoint trials have no hooks.

```bash
trajlab run configs/harbor/tb40-sonnet-wp1.json --storage /srv/trajlab/jobs
```

That's 23 tasks × 3 attempts, 6 at a time. Each attempt runs in its own Waypoint session and makes one save when the agent stops.

## 3. Repairs

Start this alongside the first attempts. It waits for them, then picks one failure per task once all 3 attempts of that task have ended.

```bash
trajlab repair corpus/jobs/tb40-sonnet-wp1 --prefix tb40-repair-wp1 --per-task 1 \
    --storage /srv/trajlab/jobs
```

Because the source job ran on Waypoint, the arms are `fresh`, `state-files`, `state-live`, `state-live-traj`, and `traj`, 3 repairs each. Add `--dry-run` first to see the plan without starting anything.

Progress is in `corpus/jobs/_repair-inputs/tb40-repair-wp1.status.json`. Each job's log is in `corpus/jobs/<job>.log`.

## 4. Reading the results

**First attempts and their saves:**

```bash
trajlab waypoint-report corpus/jobs/tb40-sonnet-wp1
```

| Column | Meaning |
|---|---|
| `reward` | 1.0 = passed the tests |
| `save` | `final` if a save was made |
| `agent` | `ended` (Claude Code finished) or `timeout_or_error` (it was killed before the save) |
| `programs` | how many programs were still running at the save, and their names: what `state-live` gets that `state-files` doesn't |
| `files_mb`, `memory_mb` | the save's size: changed files, frozen programs |
| `key` | `FOUND` if an API key or token is in the save |

A save with `0 (-)` programs is the same for `state-live` and `state-files`. Those failures can't show a difference between the two arms.

**Repairs, one row per repair trial:**

```bash
uv run python scripts/2026-10-02_repair_report.py corpus/jobs/tb40-sonnet-wp1 \
    --prefix tb40-repair-wp1 --arms fresh --arms state-files --arms state-live \
    --arms state-live-traj --arms traj
```

This prints a table and writes `corpus/jobs/tb40-sonnet-wp1/repair-report.json` (score, time, tokens, and cost per repair) and `repair-checks.json` (each test per repair, next to the same test in the failed attempt). The pass rate per arm is the share of rows with `reward == 1.0`.

`trajlab waypoint-report corpus/jobs/tb40-repair-wp1-<trial>-state-files` shows which programs a `state-files` repair stopped.

**What to compare:**
- `state-live` vs `state-files`: does having the programs still running help?
- `state-files` vs `fresh`: do the leftover files help?
- `state-live-traj` vs `traj`: does the live state help when the conversation is also there?

## 5. Cleanup and disk

Each first attempt keeps its session (a full copy of the task image plus its save) until its repairs finish.

```bash
trajlab waypoint-cleanup corpus/jobs/tb40-sonnet-wp1 --prefix tb40-repair-wp1 --dry-run
trajlab waypoint-cleanup corpus/jobs/tb40-sonnet-wp1 --prefix tb40-repair-wp1
```

It prints `keep` with a reason, or `removed`, per first attempt. It never removes a save a repair still needs.

**Disk:** `trajlab repair` stops launching below 50 GB free (`--hold-below-gb`), but first attempts don't check. Before step 2, `/srv` needs room for 69 sessions: `du -sh` of one hello-world session from check 1, scaled by each task's image size. ext4 makes `original/` a full copy per session; XFS or btrfs share it.

## 6. When something goes wrong

```bash
sudo env WAYPOINT_SESSIONS_DIR=/srv/trajlab/waypoint/sessions \
    WAYPOINT_SESSION_INFO_DIR=/srv/trajlab/waypoint/info waypoint list --json   # all sessions
ip netns list | grep tlwp-                                                       # trial networks
sudo less /srv/trajlab/waypoint/sessions/<session>/checkpoints/final/criu/dump.log   # a failed save
sudo less /srv/trajlab/waypoint/sessions/<session>/forks/<fork>/restore.log          # a failed reopen
```

A crashed run can leave a network behind (a name like `tlwp-123` with no running trial). Remove it with `sudo ip netns del tlwp-123; sudo ip link del tlwp123h`, and its NAT rules: `sudo iptables -S | grep tlwp123h` and `sudo iptables -t nat -S | grep 10.213.` list them; delete each with `-D` in place of `-A`/`-I`.
