#!/bin/sh
# Manual check (needs Docker): the PostToolUse hook under BusyBox sh in Alpine, which has no
# bash, jq, or python. Covers the ack path, the timeout path, and an unusable tool_use_id.
# Usage: sh scripts/2026-09-30_hook_alpine_check.sh
set -eu
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
script="$repo/src/trajlab/checkpoint/hook/post_tool_use.sh"
event='{"session_id":"s1","tool_name":"Bash","tool_input":{"command":"echo \"tool_use_id\": \"toolu_DECOY\""},"tool_use_id":"toolu_01Alpine"}'

run_hook() {  # $1 = TRAJLAB_ACK_WAIT, $2 = stdin; runs exactly as Claude Code does: sh -c <command>
  printf '%s' "$2" | docker run --rm -i -e TRAJLAB_ACK_WAIT="$1" \
    -e CLAUDE_CONFIG_DIR=/logs/agent/sessions -v "$work:/logs/agent" -v "$script:/hook.sh:ro" \
    alpine:3.20 sh -c 'sh -c "$(cat /hook.sh)"; echo "exit=$?"'
}

echo "timeout path:"
run_hook 1 "$event"
test -f "$work/checkpoints/toolu_01Alpine.timeout"
grep -q '"tool_use_id":"toolu_01Alpine"' "$work/checkpoints/toolu_01Alpine.req"

echo "ack path:"
rm -f "$work/checkpoints/toolu_01Alpine."*
(while [ ! -e "$work/checkpoints/toolu_01Alpine.req" ]; do sleep 0.1; done
 echo '{}' > "$work/checkpoints/toolu_01Alpine.ack") &
run_hook 30 "$event"
wait
test ! -e "$work/checkpoints/toolu_01Alpine.timeout"

echo "unusable id:"
run_hook 1 '{"tool_use_id":"../x"}'
grep -q "no usable tool_use_id" "$work/checkpoints/hook-errors.log"

echo "all hook checks passed"
