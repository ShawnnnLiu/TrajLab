#!/usr/bin/env bash
# Manual run (needs Docker and a Claude login; costs subscription usage): the ADR-0024 item 6
# comparison of whole files, `v2` against `v2-nofile`, in both record arms, on one task, through
# scripts/2026-11-02_exp2_engine_probe.py. One attempt at a time, interleaved (rep, arm, variant)
# so the four cells stay balanced if it stops early. Safe to run again: an attempt that finished
# is skipped; one that started and did not finish is reported and left alone (look at it, move
# its dirs aside, and run again to redo it). Starts the probe watcher if none holds the jobs dir
# and stops it at the end. Prints the probe's `summary` of every attempt of the task at the end.
# Mode `control` (added 2026-11-04) runs the control arm instead, with the probe's `baseline`
# variant (control's note, unchanged since tb40-exp2), named probe-<tag>-control-<rep>; it
# separates the shared record-arm text's effect from the record's.
# Usage: bash scripts/2026-11-02_exp2_whole_files.sh mvcc|pp [reps, default 5] [whole|control]
set -u
cd "$(dirname "$0")/.."

task=${1:?usage: $0 mvcc|pp [reps] [whole|control]}
reps=${2:-5}
mode=${3:-whole}
jobs=corpus/exp2/probe-jobs
case "$task" in
  mvcc)
    store=corpus/stores/mvcc-local-v5
    root=corpus/jobs/probe-mvcc-first/mvcc-lsm-compaction__CsiwPnL
    tag=mv ;;
  pp)
    store=corpus/stores/pp-local-v5
    root=corpus/jobs/exp2-example-v2/production-planning__PckZuoA
    tag=pp ;;
  *) echo "unknown task $task (mvcc or pp)" >&2; exit 2 ;;
esac
case "$mode" in
  whole)
    cells=("record-engine v2" "record-engine v2-nofile" "record-files v2" "record-files v2-nofile") ;;
  control)
    cells=("control baseline") ;;
  *) echo "unknown mode $mode (whole or control)" >&2; exit 2 ;;
esac
for path in "$store/record.duckdb" "$root/result.json"; do
  [ -e "$path" ] || { echo "missing $path" >&2; exit 2; }
done
docker info >/dev/null 2>&1 || { echo "Docker is not running" >&2; exit 2; }

started_watcher=""
if ! pgrep -f "trajlab watch $jobs" >/dev/null; then
  mkdir -p "$jobs"
  uv run trajlab watch "$jobs" --every 1000 --gate none >"$jobs/watch-$task.log" 2>&1 &
  started_watcher=$!
  echo "started the watcher (pid $started_watcher, log $jobs/watch-$task.log)"
  sleep 5
fi
stop_watcher() { [ -n "$started_watcher" ] && kill "$started_watcher" 2>/dev/null; }
trap stop_watcher EXIT

finished() {  # $1 = job name: a trial dir of the job has result.json
  ls "$jobs/$1"/*/result.json >/dev/null 2>&1
}

logs="$jobs/_exp2-probe"
mkdir -p "$logs"
partial=()
done_jobs=()
for i in $(seq 1 "$reps"); do
  for cell in "${cells[@]}"; do
    read -r arm variant <<<"$cell"
    if [ "$arm" = control ]; then
      name="probe-$tag-control-$i"
    else
      name="probe-$tag-$variant-${arm#record-}-$i"
    fi
    if finished "$name"; then
      echo "$name: done before; skipped"
      done_jobs+=("$jobs/$name")
      continue
    fi
    if [ -e "$jobs/_exp2-probe/$name" ] || [ -e "$jobs/$name" ]; then
      echo "$name: started before and not finished; left alone" >&2
      partial+=("$name")
      continue
    fi
    echo "$name: launching at $(date -u +%H:%M:%SZ)"
    uv run python scripts/2026-11-02_exp2_engine_probe.py launch \
      --corpus-store "$store" --root "$root" --jobs-dir "$jobs" \
      --arm "$arm" --variant "$variant" --name "$name" \
      >"$logs/$name.report.json" 2>"$logs/$name.launch.log"
    code=$?
    echo "$name: exit $code, $(grep -m1 '"reward"' "$logs/$name.report.json" || echo 'no reward')"
    finished "$name" && done_jobs+=("$jobs/$name")
  done
done

[ ${#partial[@]} -gt 0 ] && echo "not finished, left alone: ${partial[*]}" >&2
[ ${#done_jobs[@]} -gt 0 ] && uv run python scripts/2026-11-02_exp2_engine_probe.py summary "${done_jobs[@]}"
