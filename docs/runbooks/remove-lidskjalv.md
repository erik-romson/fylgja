# Runbook: remove Lidskjalv and keep Fylgja working

Fylgja and Lidskjalv are independent permanently (decision 0021, A21). Fylgja registers its own async
hooks, reads none of Lidskjalv's files and runs none of its scripts, so removing Lidskjalv leaves Fylgja
untouched. This runbook lists what removal involves and how to show that Fylgja is unaffected.

**These commands are for a human.** Nothing in the repository runs them against the real home. The block
tagged `sh dry-run` in section 1 is also run by `scripts/independence_check.sh`, against a copy of the
dotfiles tree in a throwaway home under `target/tests/independence/` (never against `~/dotfiles`).

Lidskjalv is a shared installation: its hook script, `~/.cache/lidskjalv` and the SketchyBar plugins are not
Fylgja's. Remove them only when you really retire Lidskjalv. This runbook never edits Fylgja's own entries
(commands containing `fylgja hook`).

## 0. Before you start

Record the baseline so that section 3 has something to compare with:

```sh
fylgja doctor --json > /tmp/fylgja-doctor-before.json
fylgja now --json | jq 'del(.generated_at)' > /tmp/fylgja-now-before.json
```

Ensure `~/dotfiles` has no uncommitted changes you want to keep apart (`git -C ~/dotfiles status --short`).
You commit the result yourself; nothing here commits.

## 1. What removal involves

### 1.1 Hook entries, the `dotfiles.conf` line, its link and the cache (scripted)

1. Lidskjalv's hook entries in `~/dotfiles/claude/.claude/settings.json`: every hook whose command contains
   `lidskjalv.sh`. Groups and events left empty are removed; Fylgja's entries and the other tools' hooks stay.
2. The `"claude:.claude/lidskjalv.sh"` line in `~/dotfiles/dotfiles.conf`.
3. The link `~/.claude/lidskjalv.sh` and the script it points at in `~/dotfiles/claude/.claude/`. A regular file
   where a link is expected is left alone and stops the run.
4. `~/.cache/lidskjalv`.

The settings file is backed up into `~/.local/state/fylgja/backups/` first (never into the dotfiles tree).

```sh dry-run
set -eu
DOTFILES="$HOME/dotfiles"
SETTINGS="$DOTFILES/claude/.claude/settings.json"
CONF="$DOTFILES/dotfiles.conf"
BACKUPS="$HOME/.local/state/fylgja/backups"
mkdir -p "$BACKUPS"
stamp=$(date -u +%Y%m%dT%H%M%SZ)

# 1. hook entries: keep every hook whose command does not run lidskjalv.sh
cp "$SETTINGS" "$BACKUPS/$stamp-settings.json"
tmp=$(mktemp)
jq 'if has("hooks") then
      .hooks |= (
        with_entries(
          .value |= (
            map(.hooks |= map(select(((.command // "") | contains("lidskjalv.sh")) | not)))
            | map(select((.hooks | length) > 0))
          )
        )
        | with_entries(select((.value | length) > 0))
      )
    else . end' "$SETTINGS" > "$tmp"
cat "$tmp" > "$SETTINGS"   # rewrite in place, so the link from ~/.claude stays valid
rm -f "$tmp"

# 2. the dotfiles.conf line
tmp=$(mktemp)
grep -v '"claude:.claude/lidskjalv.sh"' "$CONF" > "$tmp" || true
cat "$tmp" > "$CONF"
rm -f "$tmp"

# 3. the link, then the script it pointed at
LINK="$HOME/.claude/lidskjalv.sh"
if [ -L "$LINK" ]; then
  rm "$LINK"
elif [ -e "$LINK" ]; then
  echo "refusing: $LINK is a regular file, not a link" >&2
  exit 1
fi
rm -f "$DOTFILES/claude/.claude/lidskjalv.sh"

# 4. the cache
rm -rf "${HOME:?}/.cache/lidskjalv"
```

### 1.2 The SketchyBar plugins and the panel (by hand)

Lidskjalv's bar and panel live in the sketchybar dotfiles package. Remove, in `~/dotfiles`:

1. `sketchybar/.config/sketchybar/plugins/lidskjalv_*` (the plugins, `lidskjalv_aliases.conf` and the compiled
   `lidskjalv_panel`);
2. the `source "$PLUGIN_DIR/lidskjalv_items.sh"` line in `sketchybar/.config/sketchybar/sketchybarrc` and any
   other `lidskjalv` item it loads;
3. the Lidskjalv checkout (`~/projects/erik-romson/lidskjalv`) and its build outputs, if you no longer want it.

Then reload the bar yourself with `sketchybar --reload`. Fylgja does not draw the bar; until Lidskjalv's
successor exists, the bar simply loses the session chips.

## 2. Commit the result

```sh
git -C ~/dotfiles status --short
git -C ~/dotfiles commit -m "remove lidskjalv" -- claude/.claude/settings.json dotfiles.conf \
  claude/.claude/lidskjalv.sh sketchybar
```

## 3. Checks that Fylgja is unaffected

1. **Doctor stays green.** `fylgja doctor` exits 0 and prints the same results as before. Only the
   informational `lidskjalv-hooks` line disappears.

   ```sh
   fylgja doctor --json > /tmp/fylgja-doctor-after.json
   jq -S '[.checks[] | select(.name != "lidskjalv-hooks")]' /tmp/fylgja-doctor-before.json > /tmp/a.json
   jq -S '[.checks[] | select(.name != "lidskjalv-hooks")]' /tmp/fylgja-doctor-after.json > /tmp/b.json
   diff /tmp/a.json /tmp/b.json   # only the hook-commands count may differ: Lidskjalv's commands are gone
   ```

2. **`/now` is unchanged.** Fylgja never read Lidskjalv's data, so the picture is the same apart from time.

   ```sh
   fylgja now --json | jq 'del(.generated_at)' > /tmp/fylgja-now-after.json
   diff /tmp/fylgja-now-before.json /tmp/fylgja-now-after.json   # live state may have moved on; compare shapes
   ```

3. **Hook events keep arriving.** Use any Claude session (or wait for one), then count the inbox files and the
   `events` rows. Both keep growing, and the dropped counter in `fylgja doctor` stays at zero.

   ```sh
   ls ~/.local/share/fylgja/hook-inbox/claude | wc -l
   sqlite3 'file:'"$HOME"'/.local/share/fylgja/fylgja.db?mode=ro' \
     "SELECT count(*), max(observed_at) FROM events WHERE source = 'claude'"
   ```

   The inbox is drained by the daemon, so a count near zero with a rising `events` total is healthy.

If a check differs, restore the settings file and `dotfiles.conf` from the backup directory named above and
open a decision record: A21 says this must not happen.
