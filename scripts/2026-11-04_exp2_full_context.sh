#!/usr/bin/env bash
# Manual run (needs Docker and a Claude login; costs subscription usage): the comparison ADR-0023's
# amendment of 2026-11-04 asks for, hunks holding the whole file (`v3`) against the same hunks cut
# to 3 lines of context (`v3-ctx3`), in both record arms, on one root, through
# scripts/2026-11-02_exp2_engine_probe.py. One attempt at a time, interleaved (rep, arm, variant)
# so the four cells stay balanced if it stops early. Safe to run again: an attempt that finished
# is skipped; one that started and did not finish is reported and left alone (look at it, move
# its dirs aside, and run again to redo it). Prints the probe's `summary` of its attempts at the
# end.
#
# It starts no watcher, so two launchers (one per task) can share one: start
#   uv run trajlab watch corpus/exp2/probe-jobs --every 1000 --gate none
# first, and keep it running until both end. The jobs and chains dirs are passed as repo paths
# even when corpus/exp2 is a symlink to another worktree's data: `trajlab run` takes only a config
# inside the repo, judged without following symlinks (`harbor_runner.repo_relative`), and the
# watcher resolves the paths it sees.
# Usage: bash scripts/2026-11-04_exp2_full_context.sh mvcc|pp reps
set -u
cd "$(dirname "$0")/.."

task=${1:?usage: $0 mvcc|pp reps}
reps=${2:?usage: $0 mvcc|pp reps}
case "$task" in
  mvcc)
    store=corpus/stores/mvcc-local-v6
    root=corpus/jobs/probe-mvcc-first/mvcc-lsm-compaction__CsiwPnL
    tag=mv ;;
  pp)
    store=corpus/stores/pp-local-v6
    root=corpus/jobs/exp2-example-v2/production-planning__PckZuoA
    tag=pp ;;
  *) echo "unknown task $task (mvcc or pp)" >&2; exit 2 ;;
esac
for path in "$store/record.duckdb" "$root/result.json" corpus/exp2/probe-jobs corpus/exp2/probe; do
  [ -e "$path" ] || { echo "missing $path" >&2; exit 2; }
done
docker info >/dev/null 2>&1 || { echo "Docker is not running" >&2; exit 2; }
if ! pgrep -f "trajlab watch corpus/exp2/probe-jobs" >/dev/null; then
  echo "no watcher holds corpus/exp2/probe-jobs; start one first (see the header)" >&2
  exit 2
fi
jobs=corpus/exp2/probe-jobs
chains=corpus/exp2/probe

finished() {  # $1 = job name: a trial dir of the job has result.json
  ls "$jobs/$1"/*/result.json >/dev/null 2>&1
}

logs="$jobs/_exp2-probe"
mkdir -p "$logs"
cells=("record-engine v3" "record-engine v3-ctx3" "record-files v3" "record-files v3-ctx3")
partial=()
done_jobs=()
for i in $(seq 1 "$reps"); do
  for cell in "${cells[@]}"; do
    read -r arm variant <<<"$cell"
    name="probe-$tag-$variant-${arm#record-}-$i"
    if finished "$name"; then
      echo "$name: done before; skipped"
      done_jobs+=("$jobs/$name")
      continue
    fi
    if [ -e "$logs/$name" ] || [ -e "$jobs/$name" ]; then
      echo "$name: started before and not finished; left alone" >&2
      partial+=("$name")
      continue
    fi
    echo "$name: launching at $(date -u +%H:%M:%SZ)"
    uv run python scripts/2026-11-02_exp2_engine_probe.py launch \
      --corpus-store "$store" --root "$root" --jobs-dir "$jobs" --chains "$chains" \
      --arm "$arm" --variant "$variant" --name "$name" \
      >"$logs/$name.report.json" 2>"$logs/$name.launch.log"
    code=$?
    echo "$name: exit $code, $(grep -m1 '"reward"' "$logs/$name.report.json" || echo 'no reward')"
    finished "$name" && done_jobs+=("$jobs/$name")
  done
done

[ ${#partial[@]} -gt 0 ] && echo "not finished, left alone: ${partial[*]}" >&2
[ ${#done_jobs[@]} -gt 0 ] && uv run python scripts/2026-11-02_exp2_engine_probe.py summary "${done_jobs[@]}"
