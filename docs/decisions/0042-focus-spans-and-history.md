# 0042: Focus spans, `/history` and the project → workspace join: details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A5, A12, A17
- Date: 2026-10-06

## Context

WP5.2 derives focus spans from the AeroSpace focus events, closes them on idle, lock and shutdown, serves them through
`/history` and `fylgja history`, reads Claude's `history.jsonl` as a secondary signal and switches the project →
workspace join to focus time. The plan fixes the algebra's rules and leaves open where the idle threshold comes from,
what happens after an idle period or a lock ends, how a span survives a crash, who attributes it, what an ignored app
does to a span, and what the secondary signal looks like.

## Decision

- **Where the threshold comes from.** The presence collector stamps `idle.start` when its threshold was crossed, so the
  idle period began `threshold` seconds before the event's `observed_at`. The core must not read a collector's table by
  name, so the threshold is its own key, `[spans] idle_threshold_seconds` (default 300, equal to
  `[collectors.presence] idle_threshold_seconds`; a test pins the two defaults together).
- **A span resumes.** When idle or the lock ends and a window was focused, a new span starts for that window at the
  time of the first input (or the unlock). Nothing else would count the time of someone who comes back to the same
  window. A focus event during idle or lock updates the remembered window and starts nothing. After a shutdown no
  window is remembered, so only the next focus event opens a span.
- **Overlaps cannot happen.** The tracker keeps a floor, the latest span boundary, and no timestamp moves a boundary
  below it. A span that would end where it began (a focus flap, an idle period that began before the span did) is
  deleted, never stored.
- **Open spans are stored.** A span is inserted open (`ended_at` NULL) and closed by the writer. A daemon that dies
  leaves one open row; the next start closes it at the newest `observed_at` in `events`, never before its start and
  never after now, with `end_reason = shutdown`, or deletes it when that leaves no length. A graceful stop closes it
  at the daemon's now.
- **Attribution.** Each span gets its project and session from the live view's resolver placements: the live session
  placed in the focused window that was active most recently. It is asked when the span opens and, if the answer was
  empty (the resolver is a debounce behind), once more when the span closes. A window no session is placed in has no
  project.
- **An ignored app is "no window".** The ingest gate drops a focus event of an ignored bundle id or title (A5). It now
  also tells the span engine the time of that event and nothing else, so the span before it ends there and none
  starts. Without this the previous window would be credited with the time spent in the ignored app.
- **Project → workspace.** Over the last 24 hours the workspace with the most focus seconds of the project wins
  (`via` `focus-time`); equal seconds go to the workspace with more of the project's windows, then to the lower name,
  and a tie that survives both is `low`. A project with no span in that window keeps the window-count rule. If the
  store cannot be read the join falls back to the window count for that refresh.
- **`/history`.** `fylgja.history/1`: spans overlapping `[since, until)` clipped to it (`clipped`), an open span runs to
  now (`open`), totals per workspace, project and app, the span list (at most 5000, `spans_truncated`), and `worked_on`.
  `since` defaults to the start of the local day, `until` to now, a date is local midnight, a window longer than a
  year or not positive is 422. The model is not `/now`: `fylgja.now/1` is unchanged.
- **The secondary signal.** The Claude collector reads `history.jsonl` incrementally and emits a `context_item` event
  with `context_kind` `worked_on` and `data.project` per project per 60 s, stamped with the prompt's time. Only the
  project and the time are taken from a line; the prompt text is dropped at once. The collector therefore declares the
  `context` capability. A marker never changes a span or a total; `/history` lists the projects with their marker
  count. The first read emits only the last 24 h.

## Consequences

- A person who sits in one window all day is one span per idle or lock period, not one span.
- A crash costs the time since the last recorded event (the AeroSpace snapshot runs every 30 s), not the whole span.
- Attribution is only as good as the placement: a JetBrains window without a live agent session has no project until
  application context (WP5.3) lands.
- The span engine and the resolver both depend on the store being readable; neither fails the daemon when it is not.
