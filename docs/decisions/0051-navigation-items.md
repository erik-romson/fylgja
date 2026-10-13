# 0051: Navigation items: one per folder and one per app

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A5, A15
- Date: 2026-10-13

## Context

The rankers of the recall view (decision 0052) order one kind of thing: a place you can go back to. The `/now` picture
has workspaces, windows, sessions and projects, but no single unit that joins an IDE window, a terminal and an agent
session that all work in the same folder, or that stands for an app such as a browser. The unit has to be built from
`/now`, so that every string is redacted already (A5, A15), it must not name a collector, and it must be the same for
any order of its inputs.

## Decision

- **Two kinds.** A `folder` item is a git-root folder: key `folder:<absolute path>`. An `app` item is an application with
  no folder: key `app:<bundle id>`, or `app:<app name>` when there is no bundle id.
- **Grouping.** A live session with a `project_path` is a place of its folder. An open window with a folder (the git root
  of the path its IDE or terminal context names) is a place of that folder; one without is a place of its app. A span of a
  window that is no longer open makes a closed place of `folder:<span.project_path>`, else `app:<span.app_bundle_id>`; a
  span of an open window joins that window's place. An item is listed when it has an open place or focus in the last 14
  days.
- **Worktrees** are separate keys and are never merged. Grouping by ticket is not done.
- **Naming.** A folder is named `<parent>/<folder>`, the last two path components. An app is named by its app name.
- **Tickets.** Keys of the form `<PREFIX>-<digits>` for the prefixes of `[items] ticket_prefixes`, matched on word
  boundaries and case-insensitively, upper-cased, deduplicated and in first-seen order. They are read from the item name,
  the project branch, the session branches and the place titles, in that order. No prefix means no ticket.
- **Roles.** A session place is an `agent`. A window is an `ide` or a `terminal` when a context item of kind
  `ide_project`, `cmux_tab` or `terminal_session` says so, else an `app`. Roles come from kinds, never from collector
  names.
- **Go.** `go` is a waiting session first, then the open place focused most recently, then any open place by ref; `None`
  when nothing is open, so there is no button. `go_by_role` holds the same choice per role (`ide`, `agent`, `terminal`).
- **Places** are ordered open first, then by last focus, then by ref, and capped at eight.
- **The summary** is deterministic: tickets, branch, `PR #n <state>`, `dirty`, the newest IDE file and the first session
  one-liner, in that order, joined with `·`. A part with nothing to say is left out.
- **The id** is the first 16 hex digits of `sha256(key)`: stable, and neither URLs nor logs carry a path.
- **`seconds_today`** is the focus time since local midnight, for display only.
- **Where it lives.** The plain types and helpers are in `core/items.py`, which rankers import. The builder
  `build_items` is in `api/item_inputs.py`, as it reads `api.now_models`; it is pure and sorts its output by key.

## Consequences

- Rankers order items and never read `/now`. A ranker needs no collector name, and a new collector with an IDE or terminal
  context kind appears in the items without a change here.
- An IDE or terminal folder outside a git repository has no folder, so its window is an app item.
- The same folder seen through a worktree, and the main checkout, are two items.
- The builder reads every string from `/now`, so privacy filtering and collector availability apply to items as they do to
  the page.
