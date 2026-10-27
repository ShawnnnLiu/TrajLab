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

## Amendment (2026-10-27): `tb40-sonnet-v4`: the record against ADR-0020's rule

### The run

`tb40-sonnet-v4` is the Exp 2 first-attempt corpus captured again with the recorder: `configs/harbor/tb40-sonnet-v4.json` is `tb40-sonnet-v3`'s config with the job name changed (23 tasks, 3 attempts, 6 concurrent, `--storage /srv/trajlab/jobs`).
The first launch (05:24 UTC) was stopped when the recorder's reader thread died on a process whose `/proc` session id was -1 (fixed in `fd8af06`); its job dir is kept in `/srv/trajlab/aside/tb40-sonnet-v4-recorder-crash-2026-10-27`, and the corpus was launched again at 05:56 UTC from `fd8af06`.

The run stopped at 09:12 UTC with 67 of 69 trials finished.
The host's 968 GB root filesystem filled at 08:56 with checkpoint layers: the containerd snapshotter keeps each image twice (blobs and unpacked snapshots, about 1.85 times the records' `bytes`), and `docker commit` stores a call's whole upper directory again at every checkpoint, so `pretrain-shard-corruption__tgAfUZG`, with a pip cache under `/root/.cache/pip`, committed 238 GB in 42 checkpoints.
Commits failed from 08:56 and the recorder died on `ENOSPC` at 09:06.
The 67 had finished by 08:29 and are untouched; `layout-config-recreation__GFoCenW` and `pretrain-shard-corruption__tgAfUZG` are unfinished, to be rerun with `harbor jobs resume` (which reruns a trial with no result) once there is disk, with the watcher and the recorder running.
Harbor has not exited 0, so `corpus/manifests/tb40-sonnet-v4.json` is not written yet.

The 67 finished trials: 13 passed, no exception, every chain complete; 1,656 hook requests and 1,656 acks, 957 checkpoints.
The recorder ran from the relaunch to 09:06 as one process with no queue overflow; it wrote 2,827,219 event lines for the 67, and dropped 211,704 events whose directory handle no longer resolved (a directory removed before its events were read).
`scripts/2026-10-27_adr0021_acceptance.py` over the 67 (log `/srv/trajlab/logs/acceptance-tb40-sonnet-v4-2026-10-27b.txt`): create 848,428, close_write 743,660, modify 685,901, delete 450,657, moved_to 97,174, and open 1,399 lines; 155,643 events (5.5%) found their writer gone.
2,162,784 of the events (76%) are `layout-config-recreation__kb38wvM`'s, nearly all from the headless Chromium its jobs ran, which is why `start_time` ties 1,377,116 events (all but 236 of them in that trial) against `command`'s 1,371,359 and `session`'s 68,886; no call for 6,214 by Claude Code outside a write tool, 2,089 by writers gone and never read, and 1,547 others.
The `eval` check: 1,564 of 1,587 Bash calls have an event by a shell running exactly their command; none of the 23 without one was backgrounded.
20 of those 23 are in the 13 trials that have no `open` line at all, though their shells sourced a snapshot (their command lines name it) and the recorder logged no failed mark; in the other 54 trials the mark gave 1,399 lines for 1,402 calls, and the 3 calls it missed are the other 3 never seen. Why the mark saw nothing in those 13 is not yet known, and is to be found before the next capture with the recorder.
The store was built with `trajlab store build` on tmpfs (`/dev/shm/trajlab/stores/tb40-sonnet-v4.b`, linked from `corpus/stores/tb40-sonnet-v4`) for want of disk, from commit `781a743` (the carrying directory, below); it does not survive a reboot and is rebuilt from the trial dirs.

### Rows by attribution

From `scripts/2026-10-27_adr0020_attribution_report.py corpus/stores/tb40-sonnet-v4 --markdown` (log `/srv/trajlab/logs/attribution-tb40-sonnet-v4-2026-10-27b.md`); cache rows are `__pycache__`, `.pyc`, and `.pytest_cache` paths.

| Attribution | Rows | Of them cache |
|---|---|---|
| `recorded` | 196,675 | 53,679 |
| `multi_writer` | 4 | 0 |
| `foreground` | 79 | 1 |
| `background`, `background_by_path`, `background_untracked` | 0 | 0 |
| `contested` | 6 | 0 |
| `untrusted_mtime` | 3 | 0 |
| `unknown` | 176 | 9 |
| total | 196,943 | 53,689 |

- The record settles 196,679 rows (99.87%). The 4 `multi_writer` rows are Chromium's `/tmp/.org.chromium.Chromium.*` files in `layout-config-recreation__kb38wvM`, each created by a browser an earlier call started and closed by a process the landing call started.
- 6 rows stay `contested`, all written by a short-lived process (`cat >> opt.py <<'E'` and the like) that had exited before the recorder read its `/proc` entry, so no call can be named; ADR-0020's 533 other contested rows are settled.
- Mapping of the 196,813 write rows (per delta row and writer): `command` 192,697, `session` 2,135, `start_time` 1,849, `tool_window` 2 (the corpus has 2 Write calls and no Edit; Sonnet wrote files with Bash), no call 130.
- 14 trials ran a job past a call's return (9 of them failures). By the record, 3,663 rows in 9 of them landed 1 to 9 calls after the call whose process wrote them (lag 1: 1,200; 2: 1,293; 3: 804; 4: 263; 5: 62; 6 to 9: 41).

### The rule against the record

ADR-0020's answer (`rule_attribution`, `rule_causal_step`) on the 196,675 `recorded` rows: the same call on 193,089 (98.2%), or 139,411 of 142,996 (97.5%) without cache rows.
The 3,586 disagreements are in 9 trials; 3,370 are in `layout-config-recreation__kb38wvM` (failed) and 177 in `pretrain-shard-corruption__R4QX4a7` (failed).

| Rule said | Recorded rows | Rule agrees | Cache rows (agree) |
|---|---|---|---|
| `foreground` | 191,007 | 189,257 (99.1%) | 53,676 (53,675) |
| `untrusted_mtime` | 2,936 | 2,936 (100%) | 0 |
| `unknown` | 2,066 | 434 (21.0%) | 3 (3) |
| `contested` | 533 | 373 (70.0%) | 0 |
| `background_untracked` | 127 | 83 (65.4%) | 0 |
| `background` | 6 | 6 (100%) | 0 |

- **`foreground`'s misses are the evidence condition's** (ADR-0020's amendment, `bfc6043`): all 1,750 lie inside the landing window with an open launcher that names nothing, and the record gives every one to the launcher.
  Over all 42,099 such rows the condition is right on 40,349 (95.8%).
  1,710 of the misses are `kb38wvM`, whose `nohup python3 fit.py ... &` jobs, polled by later calls, wrote output files under generated names that no command spells; 36 are `R4QX4a7`, 3 `sound-change-cascade__Z4Xc8Tg`, 1 `vba-userform-port__PFng8aw`.
- **`unknown` is deletes, and in `kb38wvM` an earlier call's.** 2,063 of the 2,066 are deletes; outside `kb38wvM` the record gives all 384 to the landing call, as the rule's fallback does. In `kb38wvM` 1,632 deletes (1,558 of them under Chromium's profile and Playwright's artifact directories in `/tmp`) were made by processes an earlier call started and that outlived it, the browsers of its `fit.py` jobs (mapped by `session` 1,220, `start_time` 317, `command` 95).
- **`contested`** settled by the record: the landing call 373, the launcher 120, another call 40.
- **`background_untracked`** is right on 83 of 127. Of the other 44, 32 are `R4QX4a7`, where the rule took the latest of several `nohup` launchers (step 79 or 81) and the writer was an earlier one (76, 77, or 79): the multi-job misfire ADR-0020's amendment describes; 12 are `kb38wvM`, where the rule took an earlier launcher and the writer was step 174's job.
- **Late rows.** The rule calls 133 rows late, 121 of them late by the record too and 89 to the same call; the record finds 3,663.
  Most of the rest are the two misses above: a job's write inside the landing window with no name to tie it (1,750) and a job's delete, which has no time (1,632).

### The carrying directory (`781a743`)

The first build of this store (from `6a87a09`) left 8,851 rows (4.5%) with no event of their own in their checkpoint interval, from two causes:

- a directory built under another name and renamed into place: `uv pip install` in `gsea-proteomics__mqM8Mpm` unpacks each wheel into its cache under a temporary name (3,829 rows), and `pip install --target` in three `vf2-speedup-networkx` trials builds `networkx` in a temporary directory (1,250 rows each). The recorder sees one `moved_to` on the directory and nothing under it.
- a recursive delete whose children's events went with their directory (the unresolved handles above): Chromium removing its profiles in `kb38wvM`.

The store now gives such a row the events of its nearest directory above it that was renamed into place (an added or modified path) or removed (a deleted path) in the same interval, and keeps that directory in `writes.via_dir` (glossary: carrying directory). This is still schema 4, which was never released.
On the rebuild 8,597 rows are recorded that way: the 7,579 adds, which ADR-0020's rule had as `foreground` and the record gives to the same call; and 1,018 deletes: 1,004 in `kb38wvM`, all under Chromium's profile and Playwright's artifact directories, which the rule had as `unknown` and the record gives to the earlier call whose browser removed them; 4 elsewhere, the landing call's, as the rule has them; and 10 by a process tied to no call, which stay `unknown`.
Before the fix the agreement was 185,506 of 188,088 recorded rows (98.6%); it falls to 98.2% because the 1,004 newly recorded rows the rule has wrong are all `kb38wvM` deletes.

### What the record still misses on this corpus

264 rows (0.13%) keep ADR-0020's answer:

- **149 with no event in their checkpoint interval**, 148 of them in `kb38wvM`.
  136 have their own or their carrying directory's event in the 5 s before the previous checkpoint's commit returned, 103 within 50 ms of it: the interval opens at the previous record's `captured_at`, when `docker commit` returned, but the layer was taken earlier, while the container was paused, and a write after the unpause lands in the next layer with a time before its interval (Chromium's profile removed as `fit.py` exits, 6 ms before the commit returned).
  Opening the interval of a path with no events of its own at the previous checkpoint's `requested_at` would cover these; not done: 149 rows, under 0.1% of the corpus.
- **115 whose events have no call**: 79 the rule calls `foreground`, 27 `unknown`, 6 `contested`, 3 `untrusted_mtime`.
  The first 112 were written only by processes gone before the recorder read them (a `cat` writing a heredoc, a `mkdir`), the last case "What it still misses" names; the 3 are Claude Code's own lock file, `/tmp/.local/state/claude/locks/2.1.278.lock`, in the first step of each `risk-scorer-replay` attempt, written by Claude Code outside any write tool.
