# 0060: A Go target needs a window

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: 0029, 0051
- Date: 2026-10-19

## Context

Decision 0051 makes `go` a waiting session first, then the open place focused most recently. A live session is an
open place whether or not the resolver found its window, but `POST /focus/session/{ref}` answers 409 "session has no
window" for a session without one. The Raycast client's live check found `go` and `go_agent` on such sessions: a
Claude session in a JetBrains terminal whose cwd is in no opened project, and an OpenCode session whose process chain
was missing. The first Go on those items always failed.

## Decision

- **A place is focusable or not.** `Place.focusable` is false for a session without a window (`host.window_id`), and
  true for every other place.
- **Go picks among the open, focusable places**, by the rule of 0051. `go_ide`, `go_agent` and `go_terminal` follow
  the same rule per role. An item whose open places are all windowless sessions has no `go`.
- **The place stays open.** A windowless session is still listed as an open place, and `open=true` on `GET /items`
  still keeps its item: "open" means running, not focusable.

## Consequences

- `go` and `open` are no longer the same set: an item can be open and have no `go`.
- `ItemPlace` is unchanged; a client that offers focus per place still gets 409 for a windowless session.
- `Place.focusable` defaults to true, so rankers and fakes that build places keep working.
