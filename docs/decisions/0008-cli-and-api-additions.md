# 0008: CLI and API additions

- Status: accepted
- Amendment: A8
- Amends: PLAN.md §6 CLI and API lists
- Answers: Q5
- Relates to: A7, A22
- Date: 2026-10-05

## Context

`PLAN.md` requires doctor to verify registrations, but nothing installs them. Phases 5, 6 and 8 need these commands, and the endpoints behind them, to meet their done criteria.

## Decision

**CLI and API additions.** CLI: `install config|hooks|statusline|skill|keybinding|launchd|secret [--apply]` (the first five write into `~/dotfiles` per A22), `history [--since] [--story]`, `next --feedback good|bad`, `story session <key>|ws <n>` and `todo` scoping flags. `--json` on every read command. API (§4.2): `POST /next/feedback`, and `GET`/`POST /summaries/{scope}/{ref}` for session, workspace and day summaries. Every one of these CLI commands goes through the daemon (A7), and model calls run only inside the daemon.

## Consequences

- Later WPs implement the install subcommands, history, feedback, story and summary commands and the endpoints behind them.
- Every one of these goes through the daemon, and model calls run only inside it.
- The five installers that write configuration target `~/dotfiles` according to A22.
