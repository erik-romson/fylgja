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
`~/.local/state/fylgja/fylgja.log`.

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
