# 0050: Working time and work sessions

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A12, A17
- Date: 2026-10-13

## Context

Rankers order places by how recently and how long the person worked in them. Wall-clock age is the wrong measure: a
place used before lunch, before the night or before a week away would decay while nobody works, and everything would
reorder overnight. Agent activity is not the person's work either.

Focus spans already hold exactly the person's attention: a span ends on a focus change, idle (A17), a lock, a sleeping
machine (docs/decisions/0049) and shutdown, and spans never overlap. But the daemon read them from SQLite only on
demand and only for one day, and it had no notion of active time or of a work session.

`/now` is fetched on every `now.changed`, several times a second while windows move, and has a 50 ms p95 budget, so a
ranker cannot read two weeks of spans per request.

## Decision

- **Active time is the union of the focus spans.** `core/activity.py` is pure (no clock, no I/O).
  `Activity.active_since(t)` is the active seconds in `(t, now]`: a bisect over a cumulative array built once, then a
  subtraction. An open span runs to now. A stored overlap is counted once.
- **Every age is in active seconds, so nothing moves while away.** With no open span, `active_since(t)` cannot grow,
  so no score changes and nothing reorders until the person works again.
- **Work sessions.** A not-working gap of at least `[items] work_session_break_minutes` (30 by default) starts a new
  work session. `current` is the last session, or none once the person has been away for the break; `previous` is the
  session before `current` (the last one while `current` is none). Wall time only splits sessions.
- **Away.** `away` is the latest gap of at least `[spans] idle_threshold_seconds` between two spans, or the open gap
  after the last span while idle or a lock holds or no span runs. `working` means a span is open and neither idle nor
  a lock holds.
- **The span window.** `core/span_window.py` holds the spans of the last 14 days in memory, plus the whole last
  complete work session (one followed by a break) even when it is older, so the session before the current one is
  known after a long absence. It holds at most 50 000 spans; past that the oldest go, and only the count is logged.
- **Loaded once, then fed by the span engine.** The daemon reads the window at start, after recovering stale spans,
  in a thread. Only when those 14 days hold no complete work session does it read further back
  (`queries.load_spans_before`, newest first, a page at a time). From then on `SpanEngine` hands every span action to
  its listeners right after queuing the write, and the window applies it: constant work per focus change, nothing
  read per request. The window only reads the store; the writer stays the only writer.
- **The load race.** Actions that arrive before the load has finished are buffered and applied after the loaded rows,
  keyed by `(started_at, window_id)`. A row that is both loaded and re-sent comes out as one row; the action wins.
- **Touches on presence transitions.** Going away and coming back changes working time, which no store projection
  shows. So `SpanEngine.on_away` is called when idle or a lock starts or ends, and when the machine wakes from sleep,
  and the daemon touches the live view, so clients refresh what depends on it ("away since", "while you were away").
  It is called only on a transition, never for a repeated state, so there is no event storm.
- **`Daemon.activity()`** builds an `Activity` from the window, the daemon's clock, the tracker's idle and lock state,
  `[spans] idle_threshold_seconds` and `[items] work_session_break_minutes`.

## Consequences

- Rankers age everything in active seconds and read no clock; at a half-life
  of two active hours, 14 days of about 6 active hours a day are about 42 half-lives, so older spans would weigh less
  than 1e-12.
- The daemon holds up to 14 days of spans in memory: about 25 MB at the 50 000-span cap, far less in practice.
- Spans are read from SQLite once per daemon start for the window. `focus_time` and `/history` still read the store.
- The window holds only what the store holds: spans are retained archives (A12) and are deleted a year after they
  ended, so after a longer absence there is no previous work session.
- The engine hook is called `on_away`, not `on_presence`: `core/daemon.py` must not name a built-in collector, and
  `presence` is one.
- Nothing user-visible changes yet; the navigation items and rankers consume `Activity`.
