# 0059: Focus spans keep the app name; AeroSpace's placeholder bundle id is no bundle id

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: 0050, 0051
- Date: 2026-10-19

## Context

`focus_spans` kept the bundle id of the focused window but not the app's name. A window that is no longer open
gets its place from its spans (0051), so its label was the bundle id, and an app item with no open window was
named by it too: on the daily store most closed app places read like `com.example.editor`, and a search for the
app's name did not find them. AeroSpace reports `NULL-APP-BUNDLE-ID` for a window whose app has no bundle id;
Fylgja stored it as a bundle id, so every such window, whatever its app, fell into one made-up app item.

## Decision

- **The parser reads the placeholder as no bundle id** (`collectors/aerospace/parse.py`, `NO_BUNDLE_ID`). Such a
  window falls back to its app name, as 0051 already says for a window without one.
- **The focus event carries the app name** (`FocusPayload.app_name`), and a span stores it in the new column
  `focus_spans.app_name`.
- **Closed places and app items use it.** A closed window's place is labelled by the span's app name, else its bundle
  id. An app item without an open window is named by its spans' app name. A span without a bundle id belongs to
  `app:<app name>`, in the item builder and in `rankers.base.spans_by_item` alike.
- **Migration 0004** adds the column, clears the placeholder in `focus_spans` and `windows_snapshot`, and fills the
  name of stored spans from `windows_snapshot`: the newest name of the span's bundle id, else of its window.

## Consequences

- A stored span whose app no remaining snapshot names keeps `app_name` NULL and is still labelled by its bundle id.
- `/history` is unchanged: its spans and app totals stay keyed by bundle id.
- There is no downgrade after migration 0004; an older Fylgja refuses a newer database.
