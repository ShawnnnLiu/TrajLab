---
name: run-watch
description: "Check the health of a running Harbor job, repair run, or Exp 2 retry chain in trajlab: agent liveness, checkpoint hook acks, disk headroom for docker-commit images, usage-limit stalls, and stuck containers. Use this whenever the user asks \"is the run still going\", \"how is the job doing\", \"did anything stall\", \"check on the overnight run\", or hands you a job name under corpus/jobs; whenever a /loop is set up to babysit a run (this skill is the body of that loop); and before killing or restarting any trial. Do not guess liveness from the session JSONL."
---

# Watching a trajlab run

Long runs here cost money and wall-clock, and the failure modes are quiet: a Claude Code stream that stalls holds the whole agent timeout without writing a line, a missing watcher turns every hooked call into a 240 s timeout, and docker-commit images fill the disk until Docker itself fails.
Each has happened on this repo (2026-09-29 stall, 2026-10-02 disk, `docs/research/`).
This skill is a read-only check; it reports, and only proposes an intervention.

## Inputs

- A job dir, `corpus/jobs/<job>`, or the jobs dir for everything running.
- Optionally the watcher log at `corpus/jobs/watcher-<name>.log` and per-trial `agent/checkpoints/watcher.log`.

## Checks, in order

### 1. What is running

```
docker ps --format '{{.Names}}\t{{.Status}}' | grep -- '__env-main-1'
ls -d corpus/jobs/<job>/*__*/ | while read t; do [ -f "$t/result.json" ] || echo "unfinished: $t"; done
```
Container names are `<task>__<id>__env-main-1`, lowercased (`docs/harbor-facts.md`, Docker environment).
A trial dir without `result.json` is still running or died; `result.json` only appears at finalize.

### 2. Agent liveness (per unfinished trial)

The signal is the mtime of `agent/claude-code.txt`, which gets a stream-json event every few seconds during thinking.
The session JSONL is not a signal: a line lands only when an assistant message completes, and single turns have run past four minutes.

```
for t in corpus/jobs/<job>/*__*/; do [ -f "$t/result.json" ] && continue
  f="$t/agent/claude-code.txt"; [ -f "$f" ] || { echo "no stream yet: $t"; continue; }
  echo "$(( $(date +%s) - $(stat -f %m "$f") ))s since last event  $t"; done
```
(On Linux use `stat -c %Y`.)
More than ten minutes without an event is a stall candidate.
Confirm with the socket state inside the container before calling it one: CLOSE_WAIT is state `08` in `/proc/net/tcp`.
```
docker exec <container> sh -c 'grep -c " 08 " /proc/net/tcp'
```
A non-zero count plus a stale mtime is the 2026-09-29 pattern.
Before killing anything, capture the evidence that separates a client that stopped reading from a proxy that dropped the stream: `ss -tanpi` or the receive-queue column of `/proc/net/tcp` inside the container, and the Docker Desktop log on a Mac.
Then report; the user decides whether to kill.

### 3. Checkpoint protocol health

Under `agent/checkpoints/` every `.req` should have an `.ack`; a `.timeout` means the hook waited 240 s and gave up, and the call is uncovered.
```
for d in corpus/jobs/<job>/*__*/agent/checkpoints; do
  r=$(ls "$d"/*.req 2>/dev/null | wc -l); a=$(ls "$d"/*.ack 2>/dev/null | wc -l); t=$(ls "$d"/*.timeout 2>/dev/null | wc -l)
  echo "req=$r ack=$a timeout=$t  $d"; done
```
Timeouts with no watcher process mean the hook settings were passed without `trajlab watch` running; `trajlab run` refuses that, but a hand-started `harbor run` does not.
Check the watcher is alive and holds the lock: `pgrep -fl "trajlab watch"` and `ls corpus/jobs/.trajlab-watcher.lock`.
A single unacked `.req` older than a minute while the watcher is alive is a stuck commit; look in that trial's `watcher.log`.

### 4. Disk

Each `docker commit` holds the container's whole writable layer, so a long trial multiplies its image size by its checkpoint count (5 GB at 47 checkpoints was observed).
```
df -h corpus/jobs | tail -1
docker system df
docker images trajlab-checkpoint --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | wc -l
```
`trajlab repair` holds launches below `--hold-below-gb` (default 50) and `--prune` removes finished trials' intermediate images; a plain `harbor run` has no guard.
Below 20 GB free on a Mac, say so prominently; below 10 GB, propose pausing launches.
Never delete images yourself: checkpoint images are the data, and the only images safe to remove are ones a finished trial's manifest no longer needs, which is a decision for the user and the `--prune` path.

### 5. Usage limits and infra errors

```
grep -l "usage limit\|rate_limit\|overloaded" corpus/jobs/<job>/*__*/agent/claude-code.txt 2>/dev/null
ls corpus/jobs/<job>/*__*/exception.txt 2>/dev/null
tail -20 corpus/jobs/<job>.log
```
`trajlab repair` backs off `--usage-backoff` seconds after a usage-limit error; report the count and when the backoff ends.
An `exception.txt` is an infra failure, not a task failure; it gets no reward and is excluded from rates.

### 6. Progress

```
for t in corpus/jobs/<job>/*__*/result.json; do
  python3 -c "import json,sys; r=json.load(open('$t')); v=r.get('verifier_result') or {}; print((v.get('rewards') or {}).get('reward'), '$t')"; done | sort | uniq -c
```
Report finished vs unfinished, pass vs fail, and the elapsed time of the longest unfinished trial against the job's agent timeout.

## Output

A short table: trial, state (running / stalled? / finished), seconds since last event, req/ack/timeout, reward.
Then the disk line and any proposed intervention, phrased as a proposal.
When running under `/loop`, report only what changed since the last check; a quiet run is one line.

## When used as the body of a loop

`/loop 10m /run-watch corpus/jobs/<job>` is the intended use for overnight runs.
Ten minutes matches the liveness threshold; shorter intervals add nothing because the stream events are what carry the signal.
