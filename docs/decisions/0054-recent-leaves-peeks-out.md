# 0054: The recent ranker leaves peeks out

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A6, A7
- Date: 2026-10-14

## Context

The `recent` ranker (decision 0052) orders items by active minutes since their last focus. Switching to an AeroSpace
workspace focuses the window last focused there, so a quick look at a workspace focuses a window nobody chose. In a
browser check on a scratch daemon, a 12 s visit to a workspace to read a browser window left keyboard focus on a file
manager the whole time; `recent` then ranked the file manager second, ahead of work that had held focus for minutes.
Fylgja sees focus, not what is read, so the fix has to come from the shape of the spans.

## Decision

- **A peek is not a focus for `recent`.** A peek is a closed span shorter than `[rankers.recent] min_peek_seconds`
  (default 20, 0 to 600) whose start is the end of a span on another workspace (it began on a workspace switch) and
  whose end is the start of the next span (it ended on a focus change, not on idle, a lock or sleep). The next span may
  be on any workspace: landing on one window and clicking another on the same workspace makes the landing a peek and
  the click a focus.
- **When an item's latest span is a peek,** its last focus is the end of its latest span that is not a peek; with none
  it counts as never focused. The ranker adds the label part `peek`, so the explanation says why.
- **The first span, the open span and spans without a workspace are never peeks.** `min_peek_seconds = 0` restores the
  previous order.
- **Only `recent` changes.** `frecency` already leaves out spans under 120 s for their first 120 active seconds (0052)
  and weights a short span by its minutes; `seconds_today`, `last_focus_at` on items and places, and the stored spans
  are unchanged.
- **The span-to-item mapping moves to the ranker API** as `fylgja.rankers.base.spans_by_item`, shared by `frecency`
  and `recent`; it is additive, so the ranker API stays at major 1.

## Consequences

- A place's `last_focus_at` on the page can be later than the age `recent` shows, when that focus was a peek; the
  `peek` part and its hover text explain the difference.
- A deliberate visit shorter than 20 s to another workspace, such as reading a chat and coming back, no longer moves
  that item up in `recent`. Raising or lowering `min_peek_seconds` tunes this; 0 turns it off.
- The rule needs workspace names on spans, which AeroSpace provides; spans recorded without one are always counted.
