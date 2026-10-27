#!/bin/sh
# trajlab query tool (ADR-0019): run one SQL query against the record of prior attempts.
#   query "SELECT ..."        or        query < file.sql
# Writes the query to /logs/agent/queries/<id>.sql; the host answers with <id>.result (rows as
# JSON lines, then a line with the row count) or <id>.err. Exit 0 with the rows on stdout, 1 with
# the error on stderr, 2 if the host did not answer in time. POSIX sh only: no bash, jq, python.
d="${CLAUDE_CONFIG_DIR:-/logs/agent/sessions}/../queries"
mkdir -p "$d" 2>/dev/null || { echo "query: cannot create $d" >&2; exit 2; }
chmod a+rwx "$d" 2>/dev/null
if [ $# -gt 0 ]; then sql="$*"; else sql=$(cat); fi
if [ -z "$(printf '%s' "$sql" | tr -d '[:space:]')" ]; then
  echo 'usage: query "SELECT ..."' >&2
  exit 1
fi
id="q_$(date +%s)_$$"
printf '%s\n' "$sql" > "$d/$id.sql.tmp" && mv "$d/$id.sql.tmp" "$d/$id.sql" || {
  echo "query: cannot write $d/$id.sql" >&2
  exit 2
}
wait_s=${TRAJLAB_QUERY_WAIT:-60}
case "$wait_s" in "" | *[!0-9]*) wait_s=60 ;; esac
deadline=$(( $(date +%s) + wait_s ))
while :; do
  if [ -e "$d/$id.result" ]; then cat "$d/$id.result"; exit 0; fi
  if [ -e "$d/$id.err" ]; then cat "$d/$id.err" >&2; exit 1; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "query: no answer from the host within ${wait_s}s" >&2
    exit 2
  fi
  sleep 0.1 2>/dev/null || sleep 1
done
