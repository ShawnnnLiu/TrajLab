---
name: job-loop
description: "Set up or run a recurring check on a long trajlab run (an overnight Harbor job, a repair run, an Exp 2 retry campaign) using the built-in loop skill with run-watch as its body. Use this whenever the user says \"babysit this run\", \"keep an eye on the job\", \"check every N minutes\", \"watch it overnight\", or starts a job that will outlast the session; and when a run-watch check finds a stall and the user wants it re-checked automatically."
---

# Looping a run check

Runs here take hours, and the harness cannot notify you when a container stalls or the disk fills.
The built-in `loop` skill fires a prompt on an interval; the `run-watch` skill is what the prompt should run.
The combination is one line:

```
/loop 10m /run-watch corpus/jobs/<job>
```

## Interval

Ten minutes by default.
The liveness signal is the mtime of `agent/claude-code.txt`, and the stall threshold is ten minutes of silence, so a shorter interval detects nothing sooner and costs more.
Use 30 minutes for a `trajlab repair` run, which already guards disk and backs off usage limits itself; use 5 minutes only during a dry run when the user is watching.
With no interval given, let `loop` self-pace between 10 and 30 minutes.

## What each tick reports

Only what changed since the last tick: trials finished (with reward), new stalls, timeouts under `agent/checkpoints/`, disk crossing a threshold, usage-limit backoffs.
A tick with nothing new is a single line, and the loop marks it as such so quiet hours collapse.

## When to escalate instead of continuing

Stop the loop and report at length, with the evidence `run-watch` collects, when:

- a trial's stream has been silent for more than the agent timeout's remaining budget, or a CLOSE_WAIT socket is confirmed (do not kill it; the user decides);
- free disk is below 10 GB on a Mac or below the repair launcher's `--hold-below-gb` on the server;
- the watcher process is gone while hooked trials are running (every call will time out at 240 s);
- the job log shows the same usage-limit error on three consecutive ticks;
- every trial has a `result.json` (the run is over; hand off to the `post-run` skill).

## Ending

When the run finishes, end the loop and say so; do not leave it polling a finished job.
Summarize the run in the shape `run-watch` uses, then point at `post-run` for the write-up.
