# 0061: A span focused before the window list showed its window takes the app from the list

- Status: accepted
- Amendment: none
- Amends: 0059
- Answers: —
- Relates to: 0050, 0051, 0059
- Date: 2026-10-19

## Context

The AeroSpace collector names the app of a focus event from the windows it last listed. A window focused before the
next list (a window just opened) gives a focus event with no bundle id and no name, so its span was stored with
neither. Migration 0004 filled only the name of such spans, from `windows_snapshot` by window id. A span with a name and
no bundle id belongs to `app:<name>` (0059), so on the daily store 17 apps showed twice in `GET /items`: once as
`app:<bundle id>` and once as `app:<name>`. Spans recorded after 0004 had no name either and belonged to no item.

## Decision

- **The span engine asks for the app.** `SpanTracker` takes `describe`, which gives the bundle id and the name of an
  open window (the daemon reads the live view). A span whose focus event named no app asks it when the span opens and
  once more when it closes, as `attribute` does for the project and the session. Closing the stored span writes the
  app it found and keeps any value the row has.
- **Migration 0005 repairs the stored spans.** A span without a bundle id takes the bundle id, and a missing name,
  from the newest `windows_snapshot` row of its window that overlaps the span. A span still without a bundle id but
  with a name then takes the newest bundle id recorded for that name.

## Consequences

- A window that closes before any list shows it still gives a span without an app; it belongs to no app item.
- An app with no bundle id at all (AeroSpace's placeholder) is still `app:<name>`, as 0059 says.
- Two apps with the same name and different bundle ids share the newer bundle id in the repaired spans.
- There is no downgrade after migration 0005.
