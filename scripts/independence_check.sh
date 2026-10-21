#!/bin/sh
# Lidskjalv independence suite (WP2.8, A21). Run from anywhere: sh scripts/independence_check.sh
#
# 1. the full Fylgja suite passes in a home with no Lidskjalv at all;
# 2. `fylgja doctor` gives the same results with and without a (fake) Lidskjalv install, and only
#    its informational Lidskjalv line differs;
# 3. the removal steps of docs/runbooks/remove-lidskjalv.md, applied to a rebuilt copy of the
#    "with" home, leave Fylgja's checks unchanged.
#
# Everything happens under target/tests/independence/. Nothing touches the real home or ~/dotfiles.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
BASE="$ROOT/target/tests/independence"
FIXTURE="$ROOT/tests/fixtures/dotfiles"
RUNBOOK="$ROOT/docs/runbooks/remove-lidskjalv.md"

if [ -z "${INDEPENDENCE_KEEP:-}" ]; then
  trap 'rm -rf "$BASE"' EXIT
fi

fail() {
  echo "independence check failed: $*" >&2
  exit 1
}

command -v jq >/dev/null 2>&1 || fail "jq is required (the runbook uses it)"

echo "=== sync: uv sync --frozen --all-groups (real HOME) ==="
# Resolve uv's cache and managed Pythons before HOME moves; tests that shell out to uv need them.
UV_CACHE_DIR=${UV_CACHE_DIR:-$(uv cache dir)}
UV_PYTHON_INSTALL_DIR=${UV_PYTHON_INSTALL_DIR:-$(uv python dir)}
export UV_CACHE_DIR UV_PYTHON_INSTALL_DIR
unset VIRTUAL_ENV
uv sync --frozen --all-groups
PY="$ROOT/.venv/bin/python"
FYLGJA="$ROOT/.venv/bin/fylgja"
[ -x "$PY" ] || fail "$PY is missing"
[ -x "$FYLGJA" ] || fail "$FYLGJA is missing"

rm -rf "$BASE"
mkdir -p "$BASE/bin"

# A stub `claude`: `install hooks` reads the Claude Code version to pick the events, and a CI
# runner has no Claude. A fixed version also keeps the check the same on every machine.
cat >"$BASE/bin/claude" <<'EOF'
#!/bin/sh
echo "2.1.289 (Claude Code)"
EOF
chmod +x "$BASE/bin/claude"

# in_home <home> <command...>: run with HOME and the FYLGJA_*_DIR variables inside <home>, and the
# stub claude first on PATH.
in_home() {
  _home=$1
  shift
  env HOME="$_home" \
    PATH="$BASE/bin:$PATH" \
    FYLGJA_CONFIG_DIR="$_home/.config/fylgja" \
    FYLGJA_DATA_DIR="$_home/.local/share/fylgja" \
    FYLGJA_STATE_DIR="$_home/.local/state/fylgja" \
    "$@"
}

# build_home <name> <with|without>: a home whose ~/dotfiles is a copy of tests/fixtures/dotfiles/,
# linked per dotfiles.conf, with Fylgja's hooks installed. "without" has no trace of Lidskjalv.
build_home() {
  _name=$1
  _kind=$2
  _home="$BASE/$_name"
  mkdir -p "$_home/.local/bin"
  cp -R "$FIXTURE" "$_home/dotfiles"
  _settings="$_home/dotfiles/claude/.claude/settings.json"
  if [ "$_kind" = without ]; then
    # Lidskjalv's hook entries, its dotfiles.conf line and its script.
    PYTHONPATH="$ROOT/tests" "$PY" - "$_settings" <<'PYEOF'
import json
import sys
from pathlib import Path

import dotfiles_support

path = Path(sys.argv[1])
cleaned = dotfiles_support.without_lidskjalv(json.loads(path.read_text(encoding="utf-8")))
path.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
PYEOF
    grep -v 'lidskjalv' "$_home/dotfiles/dotfiles.conf" >"$_home/dotfiles/dotfiles.conf.new"
    mv "$_home/dotfiles/dotfiles.conf.new" "$_home/dotfiles/dotfiles.conf"
    rm -f "$_home/dotfiles/claude/.claude/lidskjalv.sh"
  fi
  # Link every "package:path" entry into the home, as Erik's install script does.
  grep -o '"[A-Za-z0-9_.-]*:[^"]*"' "$_home/dotfiles/dotfiles.conf" | tr -d '"' |
    while IFS=: read -r _package _path; do
      _path=${_path%/}
      mkdir -p "$_home/$(dirname "$_path")"
      ln -s "$_home/dotfiles/$_package/$_path" "$_home/$_path"
    done
  if [ "$_kind" = with ]; then
    # A fake Lidskjalv install: the hook entries, the link and the stub are in the fixture; add its cache.
    mkdir -p "$_home/.cache/lidskjalv/cc_sessions" "$_home/.cache/lidskjalv/builds"
    echo '{}' >"$_home/.cache/lidskjalv/cc_sessions/4101.json"
    echo '{}' >"$_home/.cache/lidskjalv/builds/state.json"
  fi
  # The hooks Fylgja registers are guarded by `[ -x ~/.local/bin/fylgja ]`.
  ln -s "$FYLGJA" "$_home/.local/bin/fylgja"
  in_home "$_home" "$FYLGJA" install hooks --apply >"$BASE/install-$_name.log" ||
    fail "install hooks in $_name: $(cat "$BASE/install-$_name.log")"
  case $_kind in
    without)
      [ ! -e "$_home/.cache/lidskjalv" ] || fail "$_name has a Lidskjalv cache"
      [ ! -e "$_home/.claude/lidskjalv.sh" ] && [ ! -L "$_home/.claude/lidskjalv.sh" ] ||
        fail "$_name has a lidskjalv.sh link"
      ! grep -q 'lidskjalv' "$_settings" "$_home/dotfiles/dotfiles.conf" ||
        fail "$_name still mentions Lidskjalv"
      ;;
    with)
      grep -q 'lidskjalv.sh' "$_settings" || fail "$_name has no Lidskjalv hook entries"
      [ -L "$_home/.claude/lidskjalv.sh" ] || fail "$_name has no lidskjalv.sh link"
      [ -d "$_home/.cache/lidskjalv" ] || fail "$_name has no Lidskjalv cache"
      ;;
  esac
}

# doctor <name>: run `fylgja doctor --json` in the home; the exit status goes to <name>.rc.
doctor() {
  _home="$BASE/$1"
  _rc=0
  in_home "$_home" "$FYLGJA" doctor --json >"$BASE/doctor-$1.json" || _rc=$?
  echo "$_rc" >"$BASE/doctor-$1.rc"
  jq -e '.checks | length > 0' "$BASE/doctor-$1.json" >/dev/null || fail "doctor in $1 printed no checks"
}

# fylgja_checks <name>: Fylgja's own checks, comparable between homes. The informational Lidskjalv
# line is dropped; the home path is masked; the hook-commands count covers every tool's hook
# commands (Lidskjalv's included), so only its status is compared.
fylgja_checks() {
  sed "s|$BASE/$1|<home>|g" "$BASE/doctor-$1.json" |
    jq -S '[.checks[]
            | select(.name != "lidskjalv-hooks")
            | if .name == "hook-commands" then del(.detail) else . end]'
}

assert_same_checks() {
  fylgja_checks "$1" >"$BASE/checks-$1.json"
  fylgja_checks "$2" >"$BASE/checks-$2.json"
  if ! diff "$BASE/checks-$1.json" "$BASE/checks-$2.json"; then
    fail "Fylgja's doctor checks differ between $1 and $2"
  fi
}

assert_no_failures() {
  [ "$(cat "$BASE/doctor-$1.rc")" = 0 ] || fail "doctor exits non-zero in $1"
  jq -e '[.checks[] | select(.status == "fail")] | length == 0' "$BASE/doctor-$1.json" >/dev/null ||
    fail "a doctor check fails in $1"
}

echo "=== build the homes: without Lidskjalv, with a fake Lidskjalv install, and a copy for the runbook ==="
build_home without without
build_home with with
build_home removed with

echo "=== full suite in the Lidskjalv-free home ==="
in_home "$BASE/without" "$PY" -m pytest -m "not live" -q -p no:cacheprovider

echo "=== doctor with and without Lidskjalv ==="
doctor without
doctor with
assert_no_failures without
assert_no_failures with
jq -e '[.checks[] | select(.name == "lidskjalv-hooks")] | length == 0' "$BASE/doctor-without.json" >/dev/null ||
  fail "the Lidskjalv check appears in the Lidskjalv-free home"
jq -e '[.checks[] | select(.name == "lidskjalv-hooks" and .informational == true)] | length == 1' \
  "$BASE/doctor-with.json" >/dev/null || fail "the informational Lidskjalv check is missing in the with home"
assert_same_checks without with
# Only the informational check may differ in the summary counts.
for _name in without with; do
  jq -e '.summary.fail == 0' "$BASE/doctor-$_name.json" >/dev/null || fail "$_name summary has failures"
done
jq -S '.summary' "$BASE/doctor-without.json" >"$BASE/summary-without.json"
jq -S '.summary' "$BASE/doctor-with.json" >"$BASE/summary-with.json"
diff "$BASE/summary-without.json" "$BASE/summary-with.json" ||
  fail "the informational Lidskjalv check changed the summary"

echo "=== runbook dry-run against a copy of the with home ==="
awk '/^```sh dry-run$/ {on = 1; next} /^```$/ {on = 0} on' "$RUNBOOK" >"$BASE/runbook-dry-run.sh"
[ -s "$BASE/runbook-dry-run.sh" ] || fail "no 'sh dry-run' block in $RUNBOOK"
in_home "$BASE/removed" sh -eu "$BASE/runbook-dry-run.sh"
_removed="$BASE/removed"
! grep -q 'lidskjalv.sh' "$_removed/dotfiles/claude/.claude/settings.json" || fail "hook entries remain"
jq -e '.hooks | length > 0' "$_removed/dotfiles/claude/.claude/settings.json" >/dev/null ||
  fail "the runbook removed every hook"
! grep -q 'lidskjalv' "$_removed/dotfiles/dotfiles.conf" || fail "the dotfiles.conf line remains"
[ ! -e "$_removed/.claude/lidskjalv.sh" ] && [ ! -L "$_removed/.claude/lidskjalv.sh" ] || fail "the link remains"
[ ! -e "$_removed/.cache/lidskjalv" ] || fail "the cache remains"
[ -L "$_removed/.claude/settings.json" ] || fail "settings.json is no longer a link"
ls "$_removed/.local/state/fylgja/backups" | grep -q 'settings.json' || fail "no settings backup"
doctor removed
assert_no_failures removed
jq -e '[.checks[] | select(.name == "lidskjalv-hooks")] | length == 0' "$BASE/doctor-removed.json" >/dev/null ||
  fail "the Lidskjalv check remains after the runbook"
assert_same_checks with removed
assert_same_checks without removed

echo "=== independence check passed ==="
