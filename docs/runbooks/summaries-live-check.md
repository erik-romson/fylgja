# Live check of the summary service (WP8.1 done condition)

The automated tests use a fake model. This check is the by-hand part of WP8.1's done condition: with summaries
enabled, three live sessions get ARC and NOW and two workspaces get a paragraph, and a second request is served from
the cache without a model call. Each model call is one `claude -p --model haiku` run (about 7 s; it uses your Claude
plan), so the check makes five calls.

1. Enable summaries in `~/dotfiles/<package>/…/config.toml` (the file `fylgja install config` linked into
   `~/.config/fylgja/config.toml`), then restart the daemon:

   ```toml
   [summaries]
   enabled = true
   ```

   ```sh
   launchctl kickstart -k gui/$(id -u)/io.github.erik-romson.fylgja
   ```

2. Pick three live sessions and two workspaces:

   ```sh
   fylgja sessions
   fylgja ws
   ```

3. Ask for each one. Every command waits for `summary.ready` and prints the summary with `[fresh, as of …]`:

   ```sh
   fylgja story session <key-1>
   fylgja story session <key-2>
   fylgja story session <key-3>
   fylgja story ws <name-1>
   fylgja story ws <name-2>
   ```

4. Ask again, at once. Each answer prints immediately, from the cache, with the same `as of` time:

   ```sh
   fylgja story session <key-1> --json | jq '{state, generated_at}'
   fylgja story ws <name-1> --json | jq '{state, generated_at}'
   ```

   `state` is `fresh` (or `stale`, when the session moved on inside the TTL). No new `claude` process starts: check
   with `ps -A -o pid=,args= | grep -c 'claude -p --model haiku'` while asking.

5. Check that no summarizer session shows up: `fylgja sessions` lists no session whose cwd is
   `~/.local/state/fylgja/summarizer`, and `curl -s http://127.0.0.1:7319/now | jq '[.sessions[].story | select(.)] |
   length'` counts the stories.

6. Record the outcome (counts and states only, no summary text) in the WP8.1 acceptance note. To switch summaries
   off again, set `enabled = false` and restart: `fylgja story` then exits 1 with "summaries are disabled".

If a story says `failed`, `fylgja story … --json | jq .error_kind` names the reason: `not_installed` (no `claude` at
`~/.local/bin/claude` or on the daemon's `PATH`), `nonzero_exit` (for example not signed in), `timeout` (60 s), or
`empty_output`.
