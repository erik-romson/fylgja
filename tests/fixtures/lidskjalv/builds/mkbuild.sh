#!/usr/bin/env bash
# Write one build cache entry for the builds tests.
#
#   mkbuild.sh <builds_dir> <root> <branch> <state> <checked_age> <next_in> \
#              [head_sha] [local_head_sha] [etag] [workflows_json]
#
# <checked_age>   seconds ago that the entry was last checked
# <next_in>       seconds from now until the next check is allowed
#                 (may be negative to produce an already-due entry)
# <workflows_json>  optional JSON array of workflow objects; defaults to []
#
# Entries carry timestamps relative to now, so they cannot be static fixtures:
# a committed next_check_at would be in the past the moment it was committed.
#
# The key is computed the same way _builds_key computes it, so a drift between
# the two surfaces as a test failure rather than a silent miss.
set -e

builds_dir="$1"
root="$2"
branch="$3"
state="${4:-none}"
checked_age="${5:-0}"
next_in="${6:-900}"
head_sha="${7:-}"
local_head_sha="${8:-$head_sha}"
etag="${9:-}"
workflows_json="${10:-[]}"

# Compute key: shasum of root<FS>branch, first 16 hex chars
_FS=$'\034'
key=$(printf '%s' "${root}${_FS}${branch}" | shasum | cut -c1-16)

now=$(date +%s)
checked_at=$(( now - checked_age ))
next_check_at=$(( now + next_in ))

mkdir -p "$builds_dir/by-key"

"${JQ_BIN:-jq}" -n \
    --arg  root           "$root" \
    --arg  owner_repo     "" \
    --arg  branch         "$branch" \
    --arg  state          "$state" \
    --arg  head_sha       "$head_sha" \
    --arg  local_head_sha "$local_head_sha" \
    --arg  etag           "$etag" \
    --argjson workflows   "$workflows_json" \
    --argjson checked_at     "$checked_at" \
    --argjson next_check_at  "$next_check_at" \
    '{root:           $root,
      owner_repo:     $owner_repo,
      branch:         $branch,
      state:          $state,
      head_sha:       $head_sha,
      local_head_sha: $local_head_sha,
      url:            "",
      workflows:      $workflows,
      etag:           $etag,
      rate_remaining: null,
      checked_at:     $checked_at,
      next_check_at:  $next_check_at}' \
    > "$builds_dir/by-key/${key}.json"
