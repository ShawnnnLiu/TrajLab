# ADR-0021: Record which process wrote each file, at capture time

Status: proposed (2026-10-27).

## Context

ADR-0020 attributes a delta row to the call that caused it from two things the store holds after the fact: the file's modification time and the calls' windows.
Its validation on `tb40-sonnet-v3` (ADR-0020, amendment of 2026-10-27) found the limit of that evidence.
A process an earlier call left running (`nohup python climb2.py > log_$s.txt &`) keeps appending to a file while the agent polls it with `sleep 115; tail log_1[1-8].txt`; at the commit the file's time lies inside the polling call's own window.
The time says when, not who.
The amended rule names the job when its command redirects into the file and the polling call names nothing, and says `contested` otherwise; it cannot say `background` with certainty, and it cannot see a job that writes a file its command never names.

The store is exact for the 54 of 69 trials in which no process outlived its call.
For the other 15, and for every retry attempt of Exp 2 on the same tasks, the causal step of a contested row is a guess with a stated rule, scored twice (strict and windowed) as ADR-0020 item 4 provides.

What would make it exact is the identity of the process that performed each write, which the kernel knows and nothing in the capture records.
Process snapshots (`--extras`, ADR-0015) list what runs at each hooked call, not what each process wrote.

## Decision

0. **Scope.** A capture-side record of writes per process, from which the store joins each delta row to the call that started the writing process.
   A capture change: corpora that carry it get a new `corpus_id`, and Exp 2's retry attempts must carry it before the preregistration if the strict localization score is to be exact.
   Proposed; the prototype in item 5 decides whether it is adopted.

1. **A write recorder on the host, beside the watcher.** For each trial container, a host process subscribes with `fanotify` to the container's root filesystem mount (the overlay the containerd snapshotter mounts on the host) for `FAN_MODIFY`, `FAN_CLOSE_WRITE`, `FAN_CREATE`, `FAN_DELETE`, and `FAN_MOVED_TO`, and logs each event as one line of `agent/trajlab/writes.jsonl` in the trial dir: the container path (the event's file descriptor resolved under the mount, with the mount prefix removed), the operation, the host time, and the writer's host pid.
   `fanotify` needs `CAP_SYS_ADMIN`, so the recorder runs as a root helper the watcher starts, with no other privilege; Harbor is not touched.
   Linux only: the Mac's Docker VM has no such mount.

2. **A process is mapped to its call by its command line.** At each event the recorder reads the writer's `/proc/<pid>/status` (`NSpid`, for the container pid), `cmdline`, `stat` (session id, start time), and walks `ppid` to the root of its tree inside the container.
   Claude Code runs a Bash call as `/bin/bash -c source <shell snapshot> ... && eval '<command>'` with the call's command text verbatim in the command line (seen in `docker top` on this corpus), so the tree's root is the call whose `args_json` command equals that text, disambiguated by the shell's start time lying in the call's window.
   A child that `nohup`s and is reparented to pid 1 keeps the shell's session id; one that calls `setsid` keeps only its start time, which still falls in exactly one call's window unless the agent ran two tool calls at once.

3. **The store joins writes to deltas.** Schema version 4 adds a `writes` table (per delta row: the writer's call as `writer_step`, the number of writes, the last write's time) and `delta_attribution` gains a first case, `recorded`, that wins whenever a write to the row's path by a process of exactly one call lies in (previous checkpoint, this checkpoint].
   A path written by processes of two calls in one interval is `multi_writer`, with both steps listed.
   ADR-0020's cases remain for corpora without the record and for rows with no event (a write through a path the recorder could not resolve, a rename).

4. **What stays unattributable.** Writes by processes no call started (a daemon in the task image, cron); a file written by two calls' processes in one checkpoint interval, which gets a list, not one step; and whatever happens after the stop checkpoint.

5. **Prototype before adoption**, as a dated script run by hand on hello-world and on one `pretrain-shard-corruption` task, recorded here by amendment:
   - that an `fanotify` mark on the overlay mount reports the container process's pid for a write made inside the container (overlayfs forwards the write in the caller's context; to be confirmed, with `FAN_MARK_FILESYSTEM` on the upper filesystem as the fallback);
   - the event rate on a build-heavy call and the recorder's cost on the trial's wall time;
   - that the `eval '<command>'` form holds for every call of a Claude Code 2.1.278 trial, including calls with `run_in_background`;
   - how many of the 19 contested rows of `pretrain-shard-corruption__XK8w3KT` steps 97 to 102 the record sends to step 97.
   The alternative, open-for-write file descriptors and `/proc/<pid>/io` counters added to the `--extras` process snapshot, catches a process that holds a log open and misses one that opens, writes, and closes each time; it is the fallback if `fanotify` fails the first check.

6. **No LLM judge, no capture-side agent change.** `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` would not help: 14 of the 15 affected trials launched jobs by hand.

## Consequences

- A new `corpus_id` for every corpus captured with the recorder, and the first-attempt corpus of Exp 2 captured again on the server (one evening) if the retry loop is to be scored against exact attributions.
- The watcher gains a privileged helper and a per-trial log; `docs/checkpoint-protocol.md` and ADR-0006 are amended with the recorder's start and stop, tied to the container's life.
- `contracts/` gains the write event model; `store/` gains the `writes` ingest and the two attribution cases; `SCHEMA_VERSION` 4.
- The paper's "Background writes" threat becomes two numbers: the share of rows attributed by record, and the agreement of ADR-0020's rule with the record on the corpus that has both.
- Open: whether a write by the verifier's own processes (after the stop) needs excluding at the recorder or only at the store; whether `agent/trajlab/writes.jsonl` is visible to the agent through the bind mount and must be excluded like the hook's files (`/logs` is already an excluded path).
