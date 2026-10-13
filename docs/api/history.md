# `GET /history` and `fylgja history`

`fylgja.history/1` answers "where did my attention go?" from the focus spans (`focus_spans`, WP5.2). It is a read
route: no authentication, Host-checked like every route. `fylgja.now/1` is unchanged.

```text
GET /history?since=<when>&until=<when>
```

| Parameter | Meaning |
|---|---|
| `since` | ISO 8601 instant (`2026-10-05T08:00:00+02:00`, `…Z`), a date (local midnight) or a local time without an offset. Default: the start of the local day of `until`. |
| `until` | The same forms. Default: now. |

A window that is not positive, one longer than a year or a time that cannot be read is **422** with a `detail`.

## Answer

```json
{
  "schema": "fylgja.history/1",
  "since": "2026-10-05T00:00:00Z",
  "until": "2026-10-05T12:00:00Z",
  "generated_at": "2026-10-05T12:00:00Z",
  "total_seconds": 9900.0,
  "workspaces": [{"workspace": "3", "seconds": 6600.0}],
  "projects": [{"project_path": "/work/alpha", "name": "alpha", "seconds": 6600.0}],
  "apps": [{"app_bundle_id": "com.example.app", "seconds": 6600.0}],
  "worked_on": [{"project_path": "/work/alpha", "name": "alpha", "markers": 2, "last_at": "2026-10-05T09:20:00Z"}],
  "spans": [
    {"window_id": "100", "workspace": "3", "app_bundle_id": "com.example.app", "title": "…",
     "project_path": "/work/alpha", "session_key": "claude:abc", "start": "2026-10-05T09:00:00Z",
     "end": "2026-10-05T09:30:00Z", "end_reason": "focus", "seconds": 1800.0, "open": false, "clipped": false}
  ],
  "spans_truncated": false
}
```

- Spans are ordered by start and **clipped** to the window (`clipped`). An open span runs to now (`open`, no
  `end_reason`). `end_reason` is `focus`, `idle`, `lock` or `shutdown`.
- Totals are ordered by seconds, largest first. An unknown workspace, project or app is an entry without its key,
  listed last. A field without a value is absent, never `null`.
- `spans` holds at most 5000 spans (`spans_truncated`); the totals cover every span.
- `worked_on` is the secondary signal: the projects Claude's prompt history marked in the window, with the number of
  markers (at most one per project per minute). It never changes a span or a total.
- Titles are redacted before storage, and a window of an ignored app is never a span (the time is counted for no
  window).

## `fylgja history`

```text
fylgja history [--since WHEN] [--until WHEN] [--spans/--no-spans] [--json]
```

`WHEN` is `today` (the default for `--since`), `yesterday`, a count (`90m`, `2h`, `3d`, `1w`) or an ISO 8601 date or
time. The command prints the window, three totals tables (workspace, project, app), the worked-on projects and, unless
`--no-spans`, the ordered spans in local time. `--json` prints the answer above. It exits 3 when the daemon is down and
2 for a time it cannot read. `--story [--date YYYY-MM-DD]` prints the story of one day instead (the daily narrative, see `docs/api/summaries.md`).

## How spans are made

See `src/fylgja/core/spans.py` and decision 0042. A span opens on a focus event for a window and closes on the next
focus change, on idle (back-dated to the start of the idle period), on lock and on shutdown; `idle` also covers a suspended machine, which the daemon detects as a wall-clock jump between
two ticks. When idle or the lock ends,
a span for the still-focused window starts again. A daemon that died leaves an open span; the next start closes it at
the last time the store observed anything.
