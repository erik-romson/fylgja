#!/usr/bin/env bash
# Print a `claude agents --json` roster for the renderer tests.
#
#   mkroster.sh <id> <status> <cwd> <age_seconds> [<id> <status> <cwd> <age> ...]
#
# startedAt is milliseconds relative to now, for the same reason mkrec.sh dates
# its records relative to now: a committed timestamp would age past the dormancy
# threshold and start failing on its own.
set -e
now_ms=$(( $(date +%s) * 1000 ))
args=()
while [ "$#" -ge 4 ]; do
    args+=(--args "$1" "$2" "$3" "$4")
    shift 4
done
# A status given as the literal word "null" becomes JSON null, which is what
# `claude agents --json` reports for a headless `claude -p` run.
jq -n --argjson now "$now_ms" '
    [ $ARGS.positional
      | _nwise(4)
      | {pid: 1, cwd: .[2], kind: "interactive",
         startedAt: ($now - ((.[3] | tonumber) * 1000)),
         sessionId: .[0], name: "stub",
         status: (if .[1] == "null" then null else .[1] end)} ]
' "${args[@]}"
