# 0022: All installed user configuration is persisted in ~/dotfiles

- Status: accepted
- Amendment: A22
- Amends: PLAN.md §4.3, §6, §8
- Answers: Q5, Q10
- Relates to: A8, A21
- Date: 2026-10-05

## Context

Erik's dotfiles repo is the source of truth for this configuration.

## Decision

**All user configuration Fylgja installs is persisted in `~/dotfiles`** (Q5), following its `dotfiles.conf` convention: each entry is `package:path`, the file lives in `~/dotfiles/<package>/<path>`, and it is symlinked into `$HOME`. This covers:

- hook entries in `claude/.claude/settings.json`
- the statusline block in `claude/.claude/statusline.sh`
- the todo skill in `claude/.claude/skills/fylgja-todo/`
- the AeroSpace keybinding in `aerospace/.aerospace.toml`
- Fylgja's own `config.toml`, as the new file entry `fylgja:.config/fylgja/config.toml`. It is linked as a file, not a directory, so the API secret beside it never enters dotfiles.

The installer refuses to write a regular file where dotfiles expects a link. Installed text blocks are delimited (`# >>> fylgja >>>` … `# <<< fylgja <<<`), and JSON hook entries are recognized by their `fylgja hook` command string, so reruns are idempotent. Dotfiles may be shared with machines that have no Fylgja, so every registered command is guarded to exit 0 silently when `~/.local/bin/fylgja` is missing. Backups go to `~/.local/state/fylgja/backups/`, never into the dotfiles tree. The installer never commits (Q10). After `--apply` it prints the diff and a suggested `git -C ~/dotfiles commit` command limited to the files it touched, and doctor warns while Fylgja's changes are uncommitted. The launchd plist and the API secret are machine-specific and stay out of dotfiles.

## Consequences

- Installers write into `~/dotfiles/<package>/<path>`, keep the symlink layout and mark text blocks with `# >>> fylgja >>>` and `# <<< fylgja <<<`.
- They refuse to replace an expected link with a regular file, back up to the state directory and never commit.
- Tests run installers against a copy of `tests/fixtures/dotfiles/` under `target/tests/`, never against the real dotfiles.
- Doctor warns while Fylgja's dotfiles changes are uncommitted; the launchd plist and API secret stay out of dotfiles.
