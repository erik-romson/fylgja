<img src="docs/logo.png" alt="" width="128" align="right">

# Fylgja

## TL;DR

```sh
uv sync --all-groups
uv run fylgja serve          # daemon + API on http://127.0.0.1:7319, Ctrl-C to stop
uv run fylgja now            # in a second terminal
open http://127.0.0.1:7319/  # the navigator page
```

The page has seven tabs: Overview, Recall, Live data, Todos, Collectors, Data flow and Explain.
[`docs/api/inspect.md`](docs/api/inspect.md) describes the read routes behind Collectors, Data flow and Explain.

No configuration is needed to try it. Data lands in `~/.local/share/fylgja`; the log is
`~/.local/state/fylgja/fylgja.log`. Writes from the page and the CLI (todos, labels) need the API secret:
`uv run fylgja install secret`. If the daily daemon already runs, `serve` fails with "address already in use":
[try changes on a scratch daemon](#try-a-change-next-to-the-daily-daemon) instead.

## What it is

A local macOS daemon that knows where the work is:

- AeroSpace workspaces and windows, and which one has focus
- live agent sessions (Claude Code, Codex, OpenCode, norn), their state and a one-line summary
- app context, presence (idle, lock, mic, camera), project state, builds, tokens and quota
- todos per workspace, project or session, and a ranked "what next"

It is the backend for the `fylgja` CLI, the navigator page at `/` and Lidskjalv. Everything stays on the
machine: the API binds to `127.0.0.1` only.

## Install for daily use

Every `install` command prints a diff and changes nothing until you add `--apply`. Configuration goes into
`~/dotfiles` and is linked into `$HOME`. The installer never commits.

```sh
uv tool install .                     # puts fylgja in ~/.local/bin
fylgja install secret                 # API secret; without it every write is refused
fylgja install config --apply         # ~/.config/fylgja/config.toml
fylgja install hooks --apply          # async Claude Code hooks
fylgja install statusline --apply     # Claude statusline snapshot
fylgja install launchd --apply        # run `fylgja serve` at login
fylgja doctor                         # check the whole setup
```

Optional: `install skill` (the `fylgja-todo` Claude skill) and `install keybinding` (quick todo in AeroSpace).

### Run, update and stop the daily daemon

launchd starts `~/.local/bin/fylgja serve` at login and restarts it when it exits. `uv tool install` copies the
code, so a change in the checkout reaches the daemon only after a reinstall and a restart:

```sh
uv tool install --reinstall .                                       # copy the current checkout
launchctl kickstart -k gui/$(id -u)/io.github.erik-romson.fylgja    # restart the daemon
launchctl print gui/$(id -u)/io.github.erik-romson.fylgja | grep -E 'state|pid|last exit'
launchctl bootout gui/$(id -u)/io.github.erik-romson.fylgja         # stop it until the next login
fylgja install launchd --apply                                      # start it again
```

Its output goes to `~/.local/state/fylgja/launchd.{out,err}.log`, the daemon log to
`~/.local/state/fylgja/fylgja.log`. Exit code 3 in `last exit` with "address already in use" in the log means
another `fylgja serve` holds port 7319; stop that one and launchd takes over within 10 s. A new version may migrate
the store, after which an older version refuses it; copy `~/.local/share/fylgja` first if you may go back.

### Try a change next to the daily daemon

A scratch daemon on port 7329 runs the checkout with its own directories, so it never touches the daily store:

```sh
S=$PWD/target/manual-ui; mkdir -p $S/home $S/config $S/data $S/state
printf '[api]\nport = 7329\nallowed_origins = ["http://127.0.0.1:7329"]\n' > $S/config/config.toml
( export HOME=$S/home FYLGJA_CONFIG_DIR=$S/config FYLGJA_DATA_DIR=$S/data FYLGJA_STATE_DIR=$S/state
  uv run fylgja install secret   # creates the scratch secret once, keeps it after
  uv run fylgja serve )          # http://127.0.0.1:7329/, Ctrl-C to stop
```

It still reads the live machine (AeroSpace windows, titles, running agent processes), but finds nothing under the
empty scratch home: no Claude sessions, projects or JetBrains project paths. Delete `target/manual-ui` when done.

## Commands

| Command | Shows or does |
|---|---|
| `now` | Workspaces, sessions and anything degraded |
| `ws [N]` | All workspaces, or one with its windows and sessions |
| `sessions` | Every live agent session with state and one-liner |
| `next [--feedback good\|bad]` | What to do next, with the reason |
| `history [--since] [--story]` | Where your attention went |
| `todo`, `label` | Your todos; a manual name for a workspace |
| `session`, `story` | Act on one session; model-written summaries |
| `items [--rank] [--days] [--search]` | Folders and apps you worked in, ranked |
| `rankers` | Installed rankers: enabled, loaded, default |
| `collectors`, `doctor` | Plugin status; setup check |

Read commands take `--json`. The CLI talks to the daemon over HTTP; read commands exit with code 3
when the daemon is down.

## Configuration

`~/.config/fylgja/config.toml` overrides [`src/fylgja/defaults.toml`](src/fylgja/defaults.toml). Every
collector is on by default. These are off by default:

- `[summaries] enabled`: summaries written by a local `claude -p`
- `[actions.message] enabled`: messaging a Claude session
- `[actions.applescript] chrome` and `iterm2`: AppleScript reads of the Chrome tab URL and the iTerm2 path

`FYLGJA_CONFIG_DIR`, `FYLGJA_DATA_DIR` and `FYLGJA_STATE_DIR` move the three directories.

## Documentation

- [`docs/api/`](docs/api): HTTP API and the `/now` schema
- [`docs/collectors/`](docs/collectors): one page per collector
- [`docs/decisions/`](docs/decisions): decision records
- [`docs/runbooks/`](docs/runbooks): by-hand procedures
- [`docs/PLAN-final-plan.md`](docs/PLAN-final-plan.md): the implementation plan
- [`AGENTS.md`](AGENTS.md): how to work in this repository
