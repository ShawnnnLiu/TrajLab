# ADR-0013: Use Waypoint to save running programs for repairs

Status: proposed (2026-10-08). Code on branch `am/waypoint`; checks 1 to 4 below not yet run.
Replaces ADR-0004 for one new experiment only. Everything else stays on Docker.
How to run it: `docs/waypoint-runbook.md`.

## TL;DR

**What changes:** first attempts run inside Waypoint instead of Docker. When the agent stops, Waypoint saves the whole environment, **files and running programs**. Repairs can then start from a copy of that save with the failed agent's programs (a server, a long job) still running. Docker can only give them the files.

**Key decisions**
1. One save per attempt, made after the agent stops and **before the tests run**, so the test files are never in it.
2. Five repair arms, all on Waypoint: `fresh`, `traj`, `state-files`, `state-live`, `state-live-traj`. The main comparison is `state-live` vs `state-files`: same files, with or without the running programs.
3. Repairs start with the failed attempt's logs deleted, so the `state-*` arms don't secretly see the failed conversation.
4. Each trial gets its own network and CPU/memory limits, so copies of one save don't fight over ports.
5. Saves stay on the server, root only, and are scanned for the API key.
6. Per-action saves are not part of this experiment. They are possible (tested, see below), but repairs only use the final save.

**What we know so far**
- The code is written and passes the offline tests. Nothing has run on a real task yet.
- Saving after every Claude Code action works: a small test on 2026-10-08 froze and resumed Claude Code after each of 5 tool calls, about 0.6 s and 125 MB per save, and the agent finished correctly.
- In round 1, only about 2 of the 21 repaired failures started programs in the background (vba-userform-port, sound-change-cascade). For the other 19, a Waypoint save gives a repair the same thing as a Docker checkpoint.

**Open questions (for Shawn)**
1. Is Waypoint here to improve the repair experiment, or to show "we can save and resume a live agent" as a systems result? The answer decides whether the cost below is worth it.
2. With about 2 of 21 failures affected, `state-live` and `state-files` will look the same on most tasks. Do we keep these tasks, or pick tasks where agents leave servers or jobs running?
3. Do we want per-action saves (files and running programs after every step)? They work, but cost about 5 GB per trial, roughly 350 GB for 69 trials.
4. Where does it run? The AWS server is 99% full and shuts down on 2026-10-09. The CloudLab node needs Waypoint installed. Is there budget for about 69 new first attempts plus about 300 repairs?
5. Is ADR-0004 ("no Waypoint") being reversed for this experiment, and should this branch build on `sl/traj-text`, which isn't merged yet?

## The problem

The repair experiment (ADR-0012) asks whether a new attempt does better when it gets what a failed attempt left behind.

| Arm | The repair agent gets | Before this ADR |
|---|---|---|
| `fresh` | nothing, starts over | works |
| `traj` | the failed attempt's conversation | works |
| `state` | the failed attempt's files | works (Docker) |
| `state-live` | the failed attempt's files **and its running programs** | missing |

Docker saves files only. If the failed agent spent an hour getting a server running, a `state` repair gets the files with the server switched off.

## What Waypoint does

Waypoint saves a running Linux environment: its files and every program running in it, including what those programs hold in memory.
A save can be reopened later, several times at once, and each copy carries on exactly where the save was made.
Waypoint never saves on its own. Something has to tell it "save now".

## The whole flow

```
TERMINAL-BENCH task
  task text ───────────┐
  environment image ─┐ │
  hidden tests ──┐   │ │
                 │   ▼ ▼
                 │  FIRST ATTEMPT, inside a Waypoint session
                 │    Claude Code works ─► stops (or times out) ─► SAVE "final"
                 │                                                  (files + running programs)
                 ▼                                                       │
              TESTS RUN after the save, so test files are never in it    │
                 │                                                       │
                 ▼                                                       │
              score: pass / fail                                         │
                 │ fail                                                  │
                 ▼                                                       │
   trajlab repair picks one failure per task, starts 5 arms x 3 repairs  │
                                                                         │
   fresh             clean task image                                    │
   traj              clean task image + the failed conversation          │
   state-files       copy of the save, leftover programs stopped ◄───────┤
   state-live        copy of the save, programs still running   ◄───────┤
   state-live-traj   copy of the save, programs running + conversation ◄┘
                 │
                 ▼
   each repair is tested and scored the same way; arms are compared by pass rate
```

Every arm gets the same prompt: the line "A previous attempt at this task did not pass the task's tests.", then the original task text. No arm is given a diff or the test results. The arms differ only in the computer they start on and whether they see the failed conversation.

## Where things are stored

```
/srv/trajlab/
├── jobs/                                    = corpus/jobs
│   ├── tb40-sonnet-wp1/<task>__<id>/        first attempt (Harbor's usual trial folder)
│   │   ├── result.json                      score
│   │   ├── agent/                           conversation (trajectory.json, sessions/*.jsonl)
│   │   ├── verifier/                        test output
│   │   ├── trajlab-preinstall.json          which image it started from
│   │   └── trajlab-waypoint.json            ◄ which Waypoint session, and what the save holds:
│   │                                          programs running, sizes, whether the key leaked
│   ├── _repair-inputs/tb40-repair-wp1-<trial>-<arm>/
│   │   ├── config.json                      the repair job Harbor runs
│   │   ├── repair-source.json               which failure; for state-* arms, which save
│   │   └── <session>.jsonl                  failed conversation (traj, state-live-traj)
│   └── tb40-repair-wp1-<trial>-<arm>/<task>__<id>/
│       └── trajlab-waypoint.json            for state-* arms: which save it opened
│
└── waypoint/                                root only, never copied off the server
    ├── rootfs/<image id>/                   each pre-installed image, unpacked once
    ├── info/<session>.json                  Waypoint's index of sessions
    └── sessions/<session>/                  one per first attempt
        ├── original/                        starting files (a copy of rootfs/<image id>)
        ├── checkpoints/final/
        │   ├── upper/                       files the agent changed or added
        │   └── criu/                        the running programs, frozen (memory)
        └── forks/<repair>/upper/            each repair's own changes; the save is never modified
```

A reopened copy sees `original/` + `final/upper/` + its own changes, stacked in that order.

## Decisions

1. **First attempts run inside Waypoint.** `trajlab.capture.waypoint:WaypointEnvironment` plugs into Harbor the same way our Docker environments do. It starts from the same pre-installed image as today (ADR-0008), so the Claude Code version is unchanged. A task's separate test environment, if it has one, still runs in Docker.
2. **One save per attempt, right after the agent stops and before the tests.** Harbor always fetches the agent's logs between the agent finishing and the tests starting, including after a timeout, so the save is made at that moment. If Claude Code is still running (a timeout), it is killed first. Everything it started keeps running.
3. **The save is kept until no repair needs it.** After the tests, the attempt's programs are stopped, but the save stays on disk. `trajlab waypoint-cleanup` removes a session only when its task has been drawn and every repair that opens its save has finished.
4. **Five repair arms, all on Waypoint:** `fresh`, `traj`, `state-files`, `state-live`, `state-live-traj`. Running every arm on Waypoint keeps the setup the same across arms. `trajlab repair` picks these arms automatically when the first attempts ran on Waypoint.
5. **A repair starts with the failed attempt's logs deleted.** The save contains `/logs/agent`, which holds the failed conversation. It is emptied before a repair starts. Docker checkpoints never held `/logs` either.
6. **No per-action saves in this experiment.** Waypoint trials have no hooks and need no watcher. Per-action history stays with the Docker corpus. Per-action Waypoint saves are possible (see "Options we did not pick") and would be a separate decision.
7. **Each trial gets its own network and its own CPU and memory limits.** Waypoint doesn't provide these, so every Waypoint command that starts or reopens programs runs inside the trial's own network namespace (with internet through NAT) and a systemd scope with the task's limits.
8. **Saves are checked for the API key.** Programs the agent started carry the key in memory, so it can end up in the save. Every save is scanned, and the result is recorded in `trajlab-waypoint.json`. Saves never leave the server.

## Options we did not pick

| Option | Why not |
|---|---|
| Save after the whole run (the pasted `run_trial.py`) | The tests have already run, so the test files are in the save, and a repair could read them. |
| Save after every command Waypoint runs | Harbor starts Claude Code as one long command, so this gives one save per trial, not one per action. It would work for an agent whose loop runs outside the environment (terminus-2). |
| Save after every Claude Code tool call | Works, but not needed for repairs. Tested on 2026-10-08 (`scripts/2026-10-08_waypoint_per_action_probe.py`): 5 of 5 saves, about 0.6 s and 125 MB each, agent finished correctly. Needs three changes: start Claude Code in the background (a running Waypoint command blocks saves), a hook that waits while the host saves, and a CRIU setting naming the folders Claude Code watches (`/work`, `/root/.claude`). Costs about 5 GB per 40-action trial. Not yet tried on a real task. |
| Reuse the existing Docker first attempts | They have no running programs saved. |

## Checks before the real run

| # | Check | How |
|---|---|---|
| 1 | Hello-world passes on Waypoint. The save exists, holds the agent's file, and holds no test files. Same after a forced timeout. | `trajlab run configs/harbor/hello-world-waypoint-v1.json`, then `scripts/2026-10-08_waypoint_checks.py save <trial>` |
| 2 | Three copies of one save, opened at once, all answer on the same server port and reach the internet. | `scripts/2026-10-08_waypoint_checks.py parallel` |
| 3 | Each of the 23 tasks starts, runs, and saves. | a 1-attempt run of `tb40-sonnet-wp1.json`, then `trajlab waypoint-report` |
| 4 | Disk: each session's `original/` plus its save, times 69 attempts, fits on `/srv` with 50 GB to spare. | sizes from checks 1 and 3 |

## Known limits

- **Connections to outside servers don't survive reopening.** For example, a download in progress at the time of the save. Programs that only listen (servers) are fine.
- **Each copy has its own network address.** A server bound to the environment's own address, rather than `0.0.0.0` or `127.0.0.1`, may fail to reopen. Such repairs end in an environment error and are reported, not hidden.
- **Root only.** Commands run as root. Tasks with a non-root agent user go through `runuser`, which check 3 must confirm.
- **One container only.** Tasks that need several services are refused.
- **Few failures have running programs.** In round 1, about 2 of 21. This is from a search of the agents' commands, not a measurement. The `programs` column of `trajlab waypoint-report` measures it for the new run.

## What this costs

- First attempts must be run again (about 69), because the Docker runs have no running programs saved.
- It needs a server with root access, CRIU 4.0 or newer, and free disk. On 2026-10-08 the AWS server had Waypoint 0.7.0 and CRIU 4.2.1 but was 99% full (9 GB free) and shuts down on 2026-10-09. The CloudLab node from `profile.py` needs Waypoint installed.
- Disk per first attempt: a full copy of the task image plus the save. ext4 can't share the image copy between sessions; XFS or btrfs can.
- New corpus names: `tb40-sonnet-wp1` for first attempts and `tb40-repair-wp1-<trial>-<arm>` for repairs. Round 1 (`tb40-repair-v2`) is not rerun or compared directly.
- CLAUDE.md's "do not write a `WaypointEnvironment`" gets an exception for this experiment. The glossary gains "Waypoint session", "save", and "fork".
