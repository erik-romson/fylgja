# 0058: Hardening after the journal and attention security review

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: 0046, 0055, 0057
- Date: 2026-10-19

## Context

A security review of the attention and workspace-journal work found no high-severity issue but two medium ones.
The model call runs `claude -p` with the user's settings, and nothing turned its tools off, while the journal now
puts repository text, commit messages and run reports into the prompt. The norn report patterns took quadratic
time on a long blank run, on the daemon's event loop. Three low ones touched the same paths: a FIFO in place of a
report or source file blocked the read forever, `fylgja journal` printed control characters from model text and
folder names to the terminal, and `GET /journal/checkout` probed the filesystem for any path a web page sent.

## Decision

- **Model calls have no tools and store no transcript.** `CLAUDE_ARGS` adds `--tools ""` and
  `--no-session-persistence`, for summaries and the journal alike (refines 0046's runner).
- **Report patterns run in linear time.** `_H2` and `_OPEN_ITEM` use a greedy group and strip it after the match.
- **Only regular files are read.** The real filesystems open without blocking and check the open descriptor; a FIFO
  or device raises `OSError`. The norn reader stats and reads the path it checked.
- **The CLI strips control characters** from the journal renderings, newlines aside.
- **`/journal/checkout` matches known checkouts.** The nearest ancestor of `path` that the daemon tracks decides; the
  route does no filesystem lookup, so a cross-site GET can neither stall the loop nor learn which folders exist.

## Consequences

- A checkout the daemon has not seen yet answers 404 on `/journal/checkout` even when it is a git repository.
- Model calls no longer leave transcripts under `~/.claude/projects/`.
- Still open, as follow-ups: the model may close or add items on injected text (J11 allows it), `GET
  /journal/items/{id}/how` runs `git show` for a commit source, and `ReportSection.path` skips the privacy filter.
