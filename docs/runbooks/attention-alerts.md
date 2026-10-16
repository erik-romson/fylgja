# Switch on attention alerts

Fylgja can tell you when an agent waits on you: one desktop banner after a grace period, one reminder, and a click
that focuses the waiting session (decision 0055). It is off by default. The automated tests post nothing, so these
steps are the by-hand part: a real banner, the permission prompt and the click test.

1. Install terminal-notifier. Use 3.1.0, or 3.0.0_1 at the least, because the first 3.0.0 bottle broke authorization:

   ```sh
   brew install terminal-notifier
   terminal-notifier -help | head -3
   ```

2. Post one banner by hand. This is the macOS 27 check of the tool and of the click action. Accept the permission
   prompt, and allow banners in System Settings → Notifications:

   ```sh
   /opt/homebrew/bin/terminal-notifier -title Fylgja -message test -group fylgja \
     -execute "$(command -v fylgja) session focus '' --else-open"
   ```

   Click the banner. The navigator page should open, because the empty key falls back to it.

   - If the click does nothing, the click action does not work on this macOS. Use the `-open {url}` fallback line of
     the comment in `src/fylgja/defaults.toml` in step 3.
   - If no banner shows at all, see the fallbacks below.

3. Enable `[notify]` in `~/dotfiles/<package>/…/config.toml`, the file `fylgja install config` linked into
   `~/.config/fylgja/config.toml`. Replace `/ABSOLUTE/PATH/fylgja` with the output of `command -v fylgja`:
   terminal-notifier runs click commands with a reduced `PATH`.

   ```toml
   [notify]
   enabled = true
   desk_command = [
     "/opt/homebrew/bin/terminal-notifier", "-title", "{title}", "-message", "{message}",
     "-group", "fylgja", "-execute", "/ABSOLUTE/PATH/fylgja session focus '{key}' --else-open",
   ]
   ```

   The click-does-nothing variant, which opens the page and does not focus a session:

   ```toml
   desk_command = [
     "/opt/homebrew/bin/terminal-notifier", "-title", "{title}", "-message", "{message}",
     "-open", "{url}", "-group", "fylgja",
   ]
   ```

   Element 0 of a command must be an absolute path, because launchd's `PATH` is minimal. The other keys keep their
   defaults: `kinds = ["permission", "input"]`, `grace_seconds = 20`, `reminder_seconds = 600`,
   `desk_detail = "project"`, `away_detail = "kind"` and `timeout_seconds = 5`.

4. Restart the daemon:

   ```sh
   launchctl kickstart -k gui/$(id -u)/io.github.erik-romson.fylgja
   ```

5. Check it. `fylgja doctor` runs the `notify:*` checks, and the "Alerts" section of the Collectors tab shows the
   last delivery failure as `notify: <kind>`; the Data flow tab shows the counters:

   ```sh
   fylgja doctor
   ```

6. Try it first on a scratch daemon (a scratch `HOME` and `FYLGJA_*_DIR`, another port), as the README's "Try a
   change next to the daily daemon" section describes. Put the `[notify]` section in the scratch `config.toml`.

   A session that asks for permission, or a norn run that stops in failure recovery, then gives one banner about
   20 s later, unless that session's window has focus. A click focuses the session's workspace, window and, when
   cmux tab focus is on, its tab. A banner that names several sessions opens the page.

## Phone (optional)

While you are idle (300 s) or the screen is locked, `away_command` replaces `desk_command`. This needs the presence
collector; without it you are never away and only the desk channel runs.

1. Make a topic. The topic is a password, so keep it out of the repository and out of shared screenshots:

   ```sh
   python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
   ```

2. Subscribe to that topic in the ntfy app on the phone, then add:

   ```toml
   [notify]
   away_command = ["/usr/bin/curl", "-s", "-m", "5", "-H", "Title: {title}", "--data-raw", "{message}", "https://ntfy.sh/<topic>"]
   away_detail = "kind"
   ```

   Keep `away_detail = "kind"`: the phone alert then carries only the kind and the source, never a folder or a
   reason. `fylgja doctor` warns when a topic is shorter than 20 characters. Pushover is the one-line alternative: a
   `curl` call to its messages endpoint with your token and user key as `--data-raw` arguments.

## Fallbacks, untested

Use these only when terminal-notifier fails on this macOS. Each is just another `desk_command` argv.

- alerter: `brew install vjeantet/tap/alerter`. Always pass `--timeout`, or it waits for a click and the send times out.
- growlrrr, from its tap.
- A small wrapper script around `shortcuts run` with a hand-made "Show Notification" shortcut. It has no click
  action and no replace group.

## Troubleshooting

- `command_timeout` on the first send: the permission prompt is waiting. Run step 2 by hand and accept it.
- `command_failed`: usually exit 2, a bad argument. terminal-notifier misparses a message that starts with `-`, `[`,
  `(`, `{` or a quote; Fylgja's messages start with a letter or a digit.
- `command_missing`: element 0 of the command does not exist.
- A click opens the page instead of the session: the session ended, has no window, or AeroSpace failed. Run
  `fylgja session focus <id>` in a terminal to see why.
- A click does nothing: either the path to `fylgja` in `-execute` is wrong, or click actions do not work on this
  macOS. Switch to the `-open {url}` variant.
- Nothing arrives while the screen is locked and `away_command` is unset: that is expected. The desk banner waits in
  Notification Center.
- No banner while you work in the session's window: alerts are held while that window has focus, other cmux tabs in
  it included. They go out when focus leaves the window, or when you go idle or lock the screen.
