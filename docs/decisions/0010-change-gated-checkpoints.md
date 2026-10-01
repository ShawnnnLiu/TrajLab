# ADR-0010: Checkpoint only calls that changed the filesystem

Status: proposed (2026-09-30).
Amends ADR-0001; supersedes ADR-0007 as the default policy.

## Context

ADR-0001 decides by tool name: `Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` are checkpointed; `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, `Task` are not.
In the TB 2.1 trials captured so far, Claude Code made 32 Bash calls and no `Read` or `Grep` calls: it inspects files through Bash (`ls -la; cat check.py`), and single Bash calls mix reads and writes (`rm -f x; ...; cat > /tmp/deep.scm <<EOF`).
So the tool name says little about whether a call changed anything, and parsing Bash to guess would be a heuristic a reviewer could not check.
After ADR-0008, a checkpoint is cheap in storage but still costs 3 to 15 s of `docker commit`, so checkpointing calls that changed nothing wastes time and blurs the join.

Measured on 2026-09-30 on derived images with about 46,000 files: a full `find / -xdev -printf ...` listing of the container takes 0.17 to 0.37 s and is 4.3 MB.

## Decision

1. **Read-only tools stay unhooked**, as in ADR-0001; they cannot write by construction.
2. **Every hooked call is measured.**
   The watcher lists the container's root filesystem with GNU `find` (type, mode, owner, size, change time, link target per path) and compares it with the listing taken at the trial's last checkpoint.
   If nothing differs outside the exclusion list, the call is **unchanged**: no snapshot, and its record names the last checkpoint, whose state it provably equals.
   Otherwise the watcher takes a checkpoint and the listing becomes the new reference.
3. **Change time, not modification time.** A file's change time cannot be set by ordinary tools, so `touch -d` or `cp -p` cannot hide a write; deletions show up as missing paths.
   Directories are compared by type, mode, and owner only, because overlayfs copy-up and child changes move their change time; their children carry the real changes.
4. **"Minor" means "changed nothing"**, never "changed little": no size threshold, because a one-byte edit can be the consequential action.
5. **Exclusions** are the paths the harness, not the agent, writes, found by listing the top layer of real checkpoints: `/logs/**` (Harbor's mounts), `/tmp/claude-<uid>/**` (Claude Code's scratch and background-task output), `/tmp/claude-code-settings/**` (Harbor's settings upload), and per user `~/.claude/**`, `~/.claude.json`, `~/.cache/claude/**`, `~/.local/state/claude/**` (Claude Code's own state).
   A directory created only to hold excluded paths is excluded too.
   Exclusions affect only the decision; a checkpoint still captures the whole filesystem.
6. **Conservative fallbacks.** The first measured call of a trial (or the first after a watcher restart) has no reference and is checkpointed as the **baseline**; an image without GNU `find` is checkpointed on every call with the change marked **unknown**.
7. **Every answered call gets a `CallRecord`** in `agent/checkpoints/calls.jsonl` and in its `.ack`: outcome (`checkpoint`, `unchanged`, or `deferred` under every-N), the change verdict, the paths this call itself changed relative to the previous call (capped, with the total), detection time, and the checkpoint whose state the call's result is in.
8. **The policy is the gate**: `trajlab watch --gate change` (this ADR), `--gate audit` (measure every call but checkpoint all of them, to validate the detector), or `--gate none` (ADR-0007's every-N).
   `change` and `audit` require `--every 1`.
   The gate is written to each trial's `policy.json` and recorded in the corpus manifest as `checkpoint_gate`.

## Consequences

- The join is exact: every hooked call names the checkpoint that holds the state after it, and for unchanged calls that is a measured equality, not an assumption.
  Read-only steps still join to the most recent earlier checkpoint.
- The share of calls that changed the filesystem becomes a reported result, per tool.
- Per-call cost is the listing (about 0.3 s) plus a commit only when something changed.
- The detector is validated against an independent ground truth: in an audit run, consecutive checkpoint images are compared by the contents of their top layers (`docker image save`), which share no code with `find`.
- Threats to state in the paper: process and memory state are not captured (ADR-0004), so a call that only starts a server counts as unchanged; files written by background processes between calls are attributed to the next measured call; a root agent that rewinds the system clock could evade change-time comparison.
- A new capture policy: corpora that use it need a new `corpus_id`.
