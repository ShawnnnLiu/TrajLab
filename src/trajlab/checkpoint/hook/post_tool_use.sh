# trajlab PostToolUse hook: request a checkpoint and wait for it (docs/checkpoint-protocol.md).
# POSIX sh only: task images may lack bash, jq, and python. Never exits non-zero.
d="${CLAUDE_CONFIG_DIR:-/logs/agent/sessions}/../checkpoints"
mkdir -p "$d" 2>/dev/null || exit 0
chmod a+rwx "$d" 2>/dev/null
input=$(cat)
field() {
  printf '%s\n' "$input" | grep -o "\"$1\" *: *\"[^\"]*\"" | head -n 1 | sed 's/.*: *"\([^"]*\)"$/\1/'
}
id=$(field tool_use_id)
case "$id" in
  "" | *[!A-Za-z0-9_-]*)
    printf '%s no usable tool_use_id: %.200s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$input" >> "$d/hook-errors.log"
    exit 0
    ;;
esac
tool=$(field tool_name)
session=$(field session_id)
agent=$(field agent_id)
if [ -n "$agent" ]; then agent="\"$agent\""; else agent=null; fi
printf '{"tool_use_id":"%s","tool_name":"%s","session_id":"%s","agent_id":%s}\n' \
  "$id" "$tool" "$session" "$agent" > "$d/$id.req.tmp" && mv "$d/$id.req.tmp" "$d/$id.req" || exit 0
wait_s=${TRAJLAB_ACK_WAIT:-240}
case "$wait_s" in "" | *[!0-9]*) wait_s=240 ;; esac
deadline=$(( $(date +%s) + wait_s ))
while [ ! -e "$d/$id.ack" ]; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    : > "$d/$id.timeout"
    if [ -e "$d/$id.ack" ]; then rm -f "$d/$id.timeout"; fi
    exit 0
  fi
  sleep 0.2 2>/dev/null || sleep 1
done
exit 0
