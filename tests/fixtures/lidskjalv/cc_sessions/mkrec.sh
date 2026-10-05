#!/usr/bin/env bash
# Write one session record for the renderer tests.
#
#   mkrec.sh <dir> <id> <state> <window_id|null> <cwd> <age_seconds> [since_age] [cwd_now]
#
# Records carry a timestamp relative to now, so they cannot be static fixtures:
# a fixed 'updated' would age past the stale threshold and break the suite the
# moment it was committed.
#
# since_age is how long ago the session entered <state>, defaulting to age. The
# two differ for exactly one reason: a running session heartbeats, so its
# 'updated' moves while its 'since' does not, and the default layout sorts on
# 'since' so that a heartbeat cannot reshuffle the row.
# cwd_now, when given, is where the session's shell is right now -- the pinned
# cwd is where it started. Omitted entirely when not asked for, so the default
# record keeps the shape of one written before the field existed.
set -e
dir="$1"; id="$2"; state="$3"; window="$4"; cwd="$5"; age="${6:-0}"; since="${7:-$age}"
cwd_now="${8:-}"
mkdir -p "$dir"
now=$(date +%s)
jq -n --arg id "$id" --arg st "$state" --arg cwd "$cwd" --arg cnow "$cwd_now" \
      --argjson w "$window" --argjson u "$(( now - age ))" \
      --argjson s "$(( now - since ))" \
      '{session_id:$id, state:$st, cwd:$cwd, window_id:$w,
        iterm_session:"", updated:$u, since:$s, seq:1}
       + (if $cnow != "" then {cwd_now:$cnow} else {} end)' > "$dir/$id.json"
