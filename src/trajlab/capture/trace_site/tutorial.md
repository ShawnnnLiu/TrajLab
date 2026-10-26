# Using TrajLab trace

TrajLab trace is a website for reading Claude Code trials one tool call at a time, and for labeling them for the failure-pattern detector (ADR-0015 §8). For every call, it shows what the agent said, ran, and saw next to what actually changed on the machine. Your labels save on the server as you work, and you can download them as CSV or JSON.

It takes about ten minutes to read. Press `?` at any time for the keyboard shortcuts, and `g` for the labeling guide.

## 1. Open the site

The site runs on the server, and you reach it from your laptop through an SSH tunnel.

On the server, start it once. It keeps running in the background:

```
cd ~/TrajLab && git pull
pkill -f "trajlab trace"; tmux kill-session -t trace 2>/dev/null; sleep 1
tmux new -d -s trace 'cd ~/TrajLab && uv run trajlab trace corpus/jobs; bash'
```

On your laptop, open the tunnel, using the server's current IP from the AWS console, and leave that terminal open:

```
ssh -N -o ServerAliveInterval=15 -L 8765:localhost:8765 -i ~/.ssh/trajlab.pem ubuntu@SERVER_IP
```

Then go to http://localhost:8765. If the page stops loading, the tunnel dropped: run the same `ssh` line again. If you have moved somewhere new, first set the SSH rule's source to **My IP** in the AWS console.

## 2. Set your name

Type your name in **Labeler**, at the top right. Every label you make is saved under that name, so spell it the same way every time: `amina` and `Amina` count as two different labelers. This browser remembers the name.

## 3. Find a trial

The home page lists the **experiments**, one per Harbor job. Each one shows how many trials ran and passed, and how far you have got.

An experiment has three tabs:

- **Trials**: every trial, with its result, minutes, calls, and your progress. Filter by task or outcome, or tick two trials and press **Compare**.
- **Labeling queue**: the order to label in (section 9). **Continue** opens the next trial you have not finished.
- **Agreement**: how two labelers' labels compare (section 11).

## 4. The trial page

The header shows the task, the result, the agent's time, the hooked calls, the cost, and how many steps you have judged.

```sample
pills
```

- **Transcript** opens the whole run as plain text, from the task instruction to the end, with nothing cut. **JSON** is Harbor's `trajectory.json`, unchanged.
- **Compare with…** puts this trial next to another one.
- **Reload** reads the trial from disk again, which is useful while a trial is still running.
- If the trial is in your labeling queue, a blue bar says which part of the queue it belongs to, and whether to label every step or only the whole trial. **Next in queue** moves on.

Below the header are three tabs: **Trace** for reading and labeling call by call, **Replay** for watching the run as if in an IDE, and **Files** for the history of each file.

## 5. Trace: read one call at a time

The strip at the top is a map of every hooked call, in order. Its color says what the call did to the filesystem, and the line along its top shows how you labeled it.

```sample
strip
```

The list on the left is the whole run. Tool calls show their number (`#12`), the tool, and the command or path. The agent's messages appear in grey italics, marked "said". Calls the hook does not see, such as `Read` and `Grep`, are faded: the detector never sees them either, so there is nothing to label there. The marks on the right of each row:

```sample
chips
```

Click a row, or press `j` and `k`, to open a call on the right:

- **Agent: said, ran, saw** shows what the agent wrote since the previous hooked call (its thinking is folded), what it ran (a command, or an edit shown as a diff), and the output it read, with the exit code.
- **Environment: what actually changed** shows whether the filesystem changed, and each changed file with its diff. It also shows how long the checkpoint paused the agent, the model time, tool time, and tokens for that step, and any processes or ports that started or stopped.

In a diff, the words that changed inside a line are marked:

```sample
diff
```

A file marked **may be against an older version** comes from a watcher fault fixed on 2026-10-22, so expect it only in older trials, such as the `off` run. An earlier call changed the file without reading it, and this diff starts from the version before that call, so it can show that call's changes as this one's. To see what the file really held, use **File** in Replay.

The point is to compare the two columns. "I fixed `parse.py`" on the left with no change to `parse.py` on the right, or "all tests pass" after a test run that exited with 1, is what the patterns describe.

The status words you will see:

- **changed**: the call changed files. **baseline**: the first measured call, so there is nothing earlier to compare with.
- **no change**: the filesystem is the same as at the last checkpoint.
- **timeout** or **no record**: the hook gave up waiting, so this call's state was not captured.
- **not hooked**: a tool the hook does not see.

## 6. Label a call

The checklist at the bottom stays in view while you scroll. Try it: the example below changes nothing.

```sample
checklist
```

- Tick each pattern that happened at this call: click it, or press `1` to `8`. Hover over a pattern to see its definition.
- If none happened, press **None of these** (`0`). A call with nothing ticked and without "None" is **not judged**, which is different from "no pattern". The export keeps the two apart.
- **Unsure** (`u`) marks a judgment to look at again.
- Write anything worth keeping in the note: `n` jumps to it, and `Esc` leaves it.
- **Mark first wrong step** (`w`) marks the one call where the trial first went wrong. A trial has at most one, so marking another call moves the mark.

To move along: `j` and `k` go to the next and previous step, `]` goes to the next step you have not judged, and `↓` and `↑` go through every row, messages included.

Messages have no checklist. Their labels go on the next hooked call or on the stop, which is where the detector reads them.

## 7. Label the whole trial

The first row, **Overview and whole-trial label**, shows the result, where the time went, the tokens and cost, the verifier's tests, and the task instruction. Its checklist asks which patterns occurred anywhere in the trial. When you are done with a trial, tick **I finished labeling this trial**. That is what the queue and the home page count.

## 8. Replay: watch the run like an IDE

Replay shows the machine at any call. Move with the slider, the arrow keys, or **Play** (space).

- **Explorer** lists every file the agent changed. The letter says what had happened to the file by the selected call:

```sample
explorer
```

- **Editor**: **Diff** shows what a call changed in the file, and **File** shows the whole file as it was after the selected call. With **follow** on, the editor opens the files each call changes.
- **Terminal** shows every command and its output up to this call, with edits and writes as one-line markers. Click a `#12` to jump to that call.
- **Processes and ports** shows what was running after the call, and anything that started or stopped.

Here the checklist is folded to one line, but the number keys still label the call on screen.

**When File cannot show a file.** The site rebuilds a file from the recorded diffs when it can. When it cannot, because the file existed before the trial or because the run recorded only changed paths, it reads the file from the call's checkpoint snapshot. Snapshots exist only on the server that ran the trial, and only until someone removes them: the `off` run still has its snapshots, and the pilot's were deleted. When neither works, the editor says what is missing.

## 9. The labeling queue, and labeling blind

ADR-0015 §8 fixes the order of work. The queue draws it with fixed seeds, so it is the same for everyone, every time:

1. **Labeling pilot** (5 trials): label every step, then revise the guide and the thresholds. These trials are not scored.
2. **Step-level sample** (12 trials): label every step. Six of them are marked **second labeler**: a teammate labels those again, for the agreement check.
3. **Trial-level** (the rest): the whole-trial checklist only.

Do not open trials beyond the pilot until the guide is frozen. Looking at them first lets the guide drift toward those particular trials.

**Hints**, the checkbox in the top bar, add yellow boxes with automatic cross-checks: a command repeated, a file back to an earlier version, a path a message names that no call changed, a success claim after a failed test. They are close to the detector's own rules, so labels made with hints on would agree with the detector for the wrong reason. Keep hints off while you label trials that are scored. On the pilot they are fine.

## 10. The labeling guide

Press `g`, or **Guide**, to open the guide beside the page. It is the file `docs/labeling-guide.md` in the repo. A heading that starts with a pattern's name, such as `## Loop` or `## No-effect write: definition`, turns its first paragraph into that pattern's definition in the checklist's hover text, in place of the detector's rule. After you edit the guide, reload the page.

## 11. A second labeler, and agreement

A second labeler opens the same site and types their own name. The **Agreement** tab then shows, for each pattern:

- how often the two said yes or no on the same trials and steps, with Cohen's κ;
- the first wrong steps they both marked;
- every disagreement, with a link to the call.

ADR-0015 §8 scores a pattern only if κ is at least 0.6 and it has at least 10 positive labels.

## 12. Saving and exporting

Everything saves by itself within a second, to `corpus/labels/<experiment>/<your name>/<trial>.json` on the server. The top bar says **Saved** with the time. If the connection drops, the browser keeps your unsaved labels, sends them when it can, and warns you if you try to close the page before that.

To download, use the **CSV** and **JSON** links: on the home page for every experiment, and on an experiment's page for that experiment. Each gives either everyone's labels or only yours.

Each row is one judged step or one trial. It holds:

- the experiment, trial, task, reward, and labeler;
- the `tool_call_id`, the call number, the tool, and a summary of the call;
- a 0/1 column for each pattern, the unsure mark, and your note;
- the first wrong step.

To compare with the detector, join its flags to these rows on trial and `tool_call_id`.

## 13. Compare two trials

**Compare** puts two trials side by side: their results, effort, failed commands, the files only one of them changed, the tools each used, and both runs in full. It is most useful for a pass and a fail of the same task.

## 14. Keyboard shortcuts

| Where | Key | Does |
|---|---|---|
| Everywhere | `?` | shows the shortcut list |
| Everywhere | `g` | opens the labeling guide |
| Trace | `j` / `k` | next / previous step |
| Trace | `]` | next step you have not judged |
| Trace and Replay | `1`–`8`, `0`, `u`, `w`, `n` | patterns, none, unsure, first wrong step, note |
| Replay | `←` / `→`, Home / End | previous / next call, first / last call |
| Replay | space | play or pause |

## 15. When something goes wrong

- **The page does not load.** The tunnel closed. Run the `ssh -N … -L 8765…` line again. If it hangs, set **My IP** in the AWS console first.
- **"Address already in use" when starting the site.** An old copy of the site is running. Run the three restart lines from section 1.
- **"Its image is not on this machine."** That file's snapshot is gone, or it is on another machine (section 8).
- **Your labels seem to be missing.** Check the spelling of your name in **Labeler**.
- **A trial that is still running looks out of date.** Press **Reload**.
