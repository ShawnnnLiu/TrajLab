# ADR-0021: Record which process wrote each file, at capture time

Status: accepted (2026-10-27), as built in the second amendment below; proposed the same day.

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

## Amendment (2026-10-27): the first prototype check passes

Run on the Linux capture server (Docker CE 29.9.2, containerd image store, overlayfs snapshotter, kernel 7.0.0-1014-aws), with `fatrace` (an `fanotify` client that marks every mounted filesystem and prints the writer's pid) as root, a throwaway `alpine` container, and a filter on the container's overlay upper directory, read from its init process's `/proc/<pid>/mountinfo` (`upperdir=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/<n>/fs`):

```
sudo timeout 15 fatrace -t -f WC+ | grep -F "$upperdir"
docker exec <cid> sh -c 'echo hi > /tmp/probe.txt; (sh -c "sleep 1; echo bg > /tmp/bg.txt; sleep 20" &); sleep 3'
```

```
04:26:21.722019 sh(2046553):    +   .../snapshots/3115/fs/tmp
04:26:21.722019 sh(2046553):    CWO .../snapshots/3115/fs/tmp/probe.txt
04:26:22.723723 sh(2046560):    +   .../snapshots/3115/fs/tmp
04:26:22.723723 sleep(2046560): CWO .../snapshots/3115/fs/tmp/bg.txt
```

- A write made inside the container through overlayfs reaches the host's `fanotify` mark with the container process's host pid and the upper directory's path; the container path is the event path with the upper directory removed.
- The backgrounded child (`sh -c "sleep 1; echo bg ..." &`, pid 2046560, container pid 14 by `NSpid`) is reported as the writer of `bg.txt` after its parent shell had exited and it had been reparented to the container's init: the case ADR-0020 calls `background_untracked` is recorded with its process.
- The mount itself is not visible in the host's root mount namespace (a mark on `/run/containerd/.../rootfs` saw nothing), so the recorder marks the host filesystem that holds the snapshots (`FAN_MARK_FILESYSTEM`) and filters by the upper directory, which `mountinfo` of the container's init names.
- `fatrace` prints the process's current name and host pid; the recorder reads `NSpid`, `cmdline`, session id, and start time itself, at the event, before the process is gone (several short-lived pids were gone by the time this probe looked them up).

Still to check (item 5): the event rate and cost on a build-heavy call, the `eval '<command>'` form on every call of a trial, and the 19 contested rows of `pretrain-shard-corruption__XK8w3KT`.

## Amendment (2026-10-27): accepted, as built, and the acceptance checks

### Decision, as built

Items 0, 4, and 6 stand. Items 1 to 3 are what landed on branch `sl/adr-0021-write-recorder`; item 5's checks are below.

1. **The recorder** is `trajlab record-writes <jobs-dir>` (`trajlab.checkpoint.writes`, over a ctypes fanotify binding, `trajlab.checkpoint.fanotify`; `fatrace` stayed the manual cross-check).
   It is started by hand as root in its own tmux session beside the watcher, not by the watcher: `sudo PYTHONDONTWRITEBYTECODE=1 .venv/bin/trajlab record-writes corpus/jobs`, one per jobs dir by `flock` on `.trajlab-recorder.lock`; `trajlab run` warns when none holds it, and the corpus manifest gains `writes_recorded`.
   One group in FID mode with directory and name records (`FAN_REPORT_DFID_NAME`), so creates, deletes, and renames into place carry their names and no event holds a file descriptor; one `FAN_MARK_FILESYSTEM` mark on the filesystem of the containerd snapshots (the first amendment) for `FAN_CREATE`, `FAN_DELETE`, `FAN_MOVED_TO`, `FAN_CLOSE_WRITE`, and `FAN_MODIFY`, directories included, with an unlimited queue.
   Host processes' events are dropped by pid namespace before any path is resolved; a container event's directory handle is resolved with `open_by_handle_at` and kept when it lies under a running trial's upper directory, read from its init's `mountinfo`; `FAN_MODIFY` is kept once per process and path per second.
   Each kept event is one `WriteEvent` line (`trajlab.contracts.writes`) in `agent/trajlab/writes.jsonl`: the container path, the op, the host time, and the writer as read from `/proc` at once (host and container pid, command line, session id, start time, its parents up to the container's root, whether Claude Code is among them, and the outermost Bash tool shell among them).
   Each Bash call's shell first sources its shell snapshot from `/logs/agent/sessions/shell-snapshots/`, which is on the host's filesystem; an inode mark on that directory (`FAN_OPEN`) gives one `open` line per call with the shell read while it runs, so a call whose shell exits before any of its writes is read (a `find`, a `nohup ... &`) still names its shell.
2. **A process is mapped to its call in the store, not by the recorder** (`trajlab.store.writes`), since the tie-break needs the call's window, known only after the call.
   Claude Code 2.1.278's form, read from its binary (`buildExecCommand`): the parts `source <snapshot> 2>/dev/null || true`, `shopt -u extglob ...`, an `unalias` guard, `eval <command, shell-quoted>[ < /dev/null]`, and `pwd -P >| /tmp/claude-<id>-cwd`, joined by ` && ` and run as `bash -c [-l] <that>` with `detached: true`, so every call's shell leads a session of its own; the quoting is `'...'` with `'"'"'` for a quote, and a redirect to `nul` is rewritten to `/dev/null` unless the command has `<`, `$`, or a backquote.
   The recorder parses the eval'd word back and records its sha256; the store matches it against each Bash step's `command` (and its `nul` rewrite).
   The rule, first match wins: `command` (a tool shell among the process and its parents; two steps with one command are told apart by the shell's start time); `session` (no shell above it any more, but its session id is a tool shell's pid seen at another event: `nohup` and `&` keep the session); `start_time` (not Claude Code: the one Bash step whose window holds the start of the process's oldest ancestor below the container's init; a `setsid` child); `tool_window` (Claude Code's own process, which writes a Write or Edit call's file to a temporary name and renames it into place, during exactly one write-tool step); otherwise no call.
3. **Store schema 4 and derived views 3.** `writes` holds, per delta row and writer step, the events on the row's path in its checkpoint interval (after the previous checkpoint's commit, up to this one's; the container is paused while `docker commit` runs), with `mapped_by`, `events`, `ops`, and the first and last times; a row whose writer belongs to no call has a null `writer_step`.
   `delta_attribution` gains `recorded` and `multi_writer` before ADR-0020's cases, and three columns: `writer_steps`, and the rule's own answer, `rule_attribution` and `rule_causal_step`, kept on every row so the two can be compared.
   A `multi_writer` row's causal step is its landing step, with the writers listed.

The open questions are answered: the verifier's writes are recorded (the recorder lets a trial go only when `result.json` appears or its container is gone) and join no delta row, since none lies after the stop checkpoint, so they are excluded at the store; `writes.jsonl` is in the bind mount and readable by the agent at `/logs/agent/trajlab/`, which the store never diffs.

What it still misses: attribute-only changes (`chmod`, a `touch` of a file already in the upper directory; `FAN_ATTRIB` is not subscribed), which fall to ADR-0020's rule unless the path was also written; a write whose directory is gone before its handle is resolved; and a writer that exited before the recorder read it and was never read before, which has no session or start time to map by.

### Acceptance checks

Run on the capture server on 2026-10-27 with the recorder of commit `02da609`, by `scripts/2026-10-27_adr0021_acceptance.py` and `scripts/2026-10-27_adr0021_recorder_cost.py`; logs in `/srv/trajlab/logs/` (`accept-*`, `adr0021-recorder-cost-2026-10-27.log`, `record-2026-10-27.log`).

1. **Hello-world end to end** (`hello-world-writes-v1`, trial `hello-world__niQAdPG`, reward 1, 2 m 37 s): 3,076 event lines (create 1,364, close_write 776, modify 711, moved_to 186, delete 39), 26 of them with the writer already gone.
   Claude Code's Write call wrote `/app/hello.txt.tmp.57.<hex>` and renamed it to `/app/hello.txt`, all from the `claude` process; the trial's one delta row is `recorded` to the Write step by `tool_window`, and ADR-0020's rule (`foreground`) agrees.
   3,031 of the events are the verifier's (`uv`, `python`, `apt-get`) after the stop checkpoint and join no delta row.
2. **Event rate and cost on a build-heavy command** (the `interleaved-vigenere` derived image; copies of `/usr/lib` and `/usr/local/lib`, byte-compiled, 20,000 small files written, all removed: 27,894 files; three runs each, alternating): median wall time 6.1 s without the recorder and 6.2 s with it (+2.5%, at the timing's 0.1 s resolution); 65,056 events read (10,486/s), 89,714 lines written, no queue overflow; the recorder's CPU 3.0 s, about half of one core while the command ran.
   Host-wide, while Harbor's pre-install builder installed Claude Code into a derived image, the recorder read 2,100 to 6,100 events/s (held and then dropped, since the builder is no trial) at 0.26 to 0.65 of a core.
   The writer waits on nothing: events are queued by the kernel and read after the write.
3. **The `eval '<command>'` form and the sessions**, on the first 12 Bash calls of the pretrain trial below while it ran: 11 calls' shells were read with exactly the call's command (sha256 equal); the 12th, `cd /; find / -xdev ...`, wrote only its cwd file and had exited when that event was read, so its shell was never seen.
   That gap is why the recorder also marks the shell-snapshot directory (item 1 above, added after this check, commit after `02da609`): each shell is read when it sources its snapshot, before its command runs.
   Every one of 52 events by a tool shell had its session id equal to its pid, and every one of 380 events by a process under a tool shell had the shell's pid as its session id: `detached: true` holds, and the session rule has what it needs.
4. **One `pretrain-shard-corruption` trial** (`pretrain-writes-v1`, trial `pretrain-shard-corruption__oGVk8dH`, same agent settings as `tb40-sonnet-v3`; run with `--allow-dirty`, since the uncommitted changes were docs and scripts; 17 minutes, reward 0, chain complete): recorded by `02da609` until the recorder was restarted on the shell-snapshot mark at 05:23:12 UTC, a gap of a few seconds, then by the new code.
   802 event lines (create 199, modify 215, close_write 166, moved_to 135, delete 81, open 6); 122 (15%) found their writer gone, nearly all tool shells closing their cwd file and their task output under `/tmp/claude-0` as they exit, which are excluded paths; the busiest call wrote 345 events in 3 s.
   Of 34 Bash calls, 32 were matched by command; the two unmatched (step 8, `cd /; find / ...`, and step 18, `sleep 100; cat .../tasks/*.output`) ran before the restart, and all 5 calls after it have their `open` line.
   Events by mapping: `command` 628, `session` 8, `start_time` 4; no call for 132 by Claude Code outside a write tool (its own task files), 26 by writers gone before they were ever read, and 4 by `runc` (the harness's `docker exec`).
   The agent backgrounded two calls (steps 15 and 17, past the 120 s timeout) and polled them with `sleep; cat .../tasks/*.output`, but its jobs wrote only Claude Code's excluded task output, so all 96 delta rows are `recorded`, and ADR-0020's rule agrees on all 96 (95 it called `foreground`; the one it called `contested`, the record gives to the landing call).
   The case this ADR is for, a job writing a file the agent polls, did not occur in this trial; `tb40-sonnet-v4` is the test of it.
5. **The 19 contested rows of `pretrain-shard-corruption__XK8w3KT`** cannot be checked: that trial ran without the recorder. The new trial's contested rows are reported in item 4's results and across `tb40-sonnet-v4` instead.

`tb40-sonnet-v4` was started before item 4 finished, on the owner's call (2026-10-27): the recorder only observes, and a mapping fault found later is fixed by rebuilding the store from the trials' `writes.jsonl`.
