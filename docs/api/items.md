# `GET /items` and `GET /rankers`

`fylgja.items/1` answers "where was I, and where do I go back to?" with **navigation items**: one per git-root folder
and one per app (decision 0051), in the order of a **ranker** (decision 0052). `fylgja.rankers/1` lists the rankers. Both
are reads: no authentication, Host-checked like every route. `fylgja.now/1` is unchanged.

```text
GET /items?rank=<name>&days=<1..14>&q=<text>&limit=<1..500>
GET /rankers?rank=<name>
```

Both follow the `/now` serialization rule: a field without a value is absent, never `null`, and lists are always
present. Answers are not cached (`cache-control: no-store`).

## The rule: the daemon computes, clients render

Everything a client shows is in the answer: the order, the day groups, the Go targets, the durations in seconds, the
search result, the cap and the counts (`total`, `shown`), and for the rankers the choices to offer (`choices`,
`selected`, `stale_choice`). A client formats times and seconds and renders the rest as it comes. The navigator page
shows "`shown` of `total`" and fills its ranker list from `choices`; it never filters `rankers` and never counts
`items`. The page, `fylgja items` and any other client get the same answer for the same question.

## `GET /items`

| Parameter | Meaning |
|---|---|
| `rank` | A loaded ranker, or `natural` (the floor). Default: the default ranker in effect (see `GET /rankers`). An empty value is the default. |
| `days` | How far back an item without an open place may have had focus: 1 to 14. Default 14. |
| `q` | Search text, at most 200 characters. Case-insensitive; matched against the item name, its tickets, its path, its summary and its place titles. |
| `limit` | Keep the first `limit` items: 1 to 500. Default: all. |

**422** with a `detail`: a `rank` that names neither a loaded ranker nor `natural` (an unknown, disabled, uninstalled
or broken ranker; the detail lists the loaded ones), `days` outside 1–14, `limit` outside 1–500, or a `q` over 200
characters.

**Search and limit run on the server, after ranking.** `q` keeps the matching items in rank order and `limit` the first
ones after that. `total` is the number of items before both, `shown` the number after both (the length of `items`).
The day groups hold the shown items.

### Answer

```json
{
  "schema": "fylgja.items/1",
  "generated_at": "2026-10-05T12:00:00Z",
  "ranker": "frecency",
  "activity": {"working": true, "away_since": "2026-10-05T10:45:00Z", "away_until": "2026-10-05T11:20:00Z",
               "session_started_at": "2026-10-05T11:20:00Z", "active_seconds_session": 2400.0},
  "while_away": [
    {"item": "208d0396f4f10d7f", "name": "alpha/main", "kind": "input", "reason": "waiting for input: …",
     "since": "2026-10-05T11:02:00Z", "go": {"kind": "session", "ref": "claude:abc"}}
  ],
  "last_session": {"start": "2026-10-05T08:30:00Z", "end": "2026-10-05T10:45:00Z", "active_seconds": 7200.0,
                   "items": ["208d0396f4f10d7f", "cf461ead8bfab552"]},
  "items": [
    {"id": "208d0396f4f10d7f", "key": "folder:/work/alpha/main", "kind": "folder", "name": "alpha/main",
     "path": "/work/alpha/main", "tickets": [], "score": 1142.5,
     "explanation": "focus 30 · last session +12 · waiting +100 · while away +1000",
     "parts": [{"name": "focus", "value": 30.5}, {"name": "last session", "value": 12.0},
               {"name": "waiting", "value": 100.0}, {"name": "while away", "value": 1000.0}],
     "places": [
       {"kind": "session", "ref": "claude:abc", "role": "agent", "label": "claude", "title": "…",
        "workspace": "3", "open": true, "waiting": true, "last_focus_at": "2026-10-05T10:40:00Z"},
       {"kind": "window", "ref": "100", "role": "ide", "label": "PyCharm", "title": "…", "workspace": "3",
        "open": true, "waiting": false, "last_focus_at": "2026-10-05T10:45:00Z"}
     ],
     "state": ["input"], "last_focus_at": "2026-10-05T10:45:00Z", "seconds_today": 5400.0,
     "summary": "main · \"…\"",
     "go": {"kind": "session", "ref": "claude:abc"}, "go_ide": {"kind": "window", "ref": "100"},
     "go_agent": {"kind": "session", "ref": "claude:abc"}},
    {"id": "cf461ead8bfab552", "key": "folder:/work/alpha/CBS-1", "kind": "folder", "name": "alpha/CBS-1", "…": "…"},
    {"id": "0ec4894ed2b575c5", "key": "app:com.example.app", "kind": "app", "name": "Example", "…": "…"}
  ],
  "days": [{"day": "2026-10-05", "items": ["208d0396f4f10d7f", "cf461ead8bfab552", "0ec4894ed2b575c5"]}],
  "summaries_enabled": false,
  "total": 3,
  "shown": 3
}
```

| Field | Type | Holds |
|---|---|---|
| `schema` | string | `fylgja.items/1` |
| `generated_at` | instant | The time of the answer, floored to 5 s: the time the ranker got |
| `ranker` | string | The ranker asked for: `rank`, else the default in effect |
| `ranker_fallback` | string, optional | `natural` when that ranker failed and the floor answered instead |
| `activity` | `ActivityInfo` | Working time as of `generated_at` |
| `while_away[]` | `AwayChange` | What started waiting during the latest absence |
| `last_session` | `LastSession`, optional | The work session before the current one |
| `items[]` | `NavItem` | Rank order, after `q` and `limit` |
| `days[]` | `DayGroup` | The shown items by the local day of their last focus, newest day first |
| `summaries_enabled` | boolean | `[summaries] enabled` |
| `query` | string, optional | `q` as given |
| `total` | integer | Items before `q` and `limit` |
| `shown` | integer | Items in `items` |

**`NavItem`**:

- `id` is the first 16 hex digits of `sha256(key)`: stable, and no path in a URL or a log. `key` is
  `folder:<git root>` or `app:<bundle id>`; `kind` is `folder` or `app`.
- `name` is `<parent>/<folder>` for a folder and the app name for an app. `path` (folders only) and `group` (the
  draupnir group, shown only) are optional.
- `tickets` are the ticket keys of `[items] ticket_prefixes` found in the name, the branches and the titles.
- `score`, `explanation` and `parts[]` (`ScorePart`: `name`, `value`) come from the ranker. A part with value 0 is a label.
- `places[]` (`ItemPlace`): `kind` (`session` or `window`), `ref` (the session key or the window id), `role` (`agent`,
  `ide`, `terminal` or `app`), `label`, optional `title` and `workspace`, `open`, `waiting` and optional
  `last_focus_at`. Open places first, then the most recently focused; at most eight.
- `state` holds the waiting kinds, `todo_next`, `dirty` and `pr_open`, sorted.
- `last_focus_at` is optional; `seconds_today` is the focus time since local midnight.
- `summary` is deterministic: tickets, branch, `PR #n <state>`, `dirty`, the IDE file and the first session one-liner,
  joined with `·`.
- `go` is the Go button: a waiting session first, else the open place focused most recently. `go_ide`, `go_agent` and
  `go_terminal` are the same choice per role. A target (`GoTarget`: `kind`, `ref`) is focused with
  `POST /focus/session/{ref}` or `POST /focus/window/{ref}`. An item with nothing open has no `go`: nothing is started
  or reopened.

**`DayGroup`** has `day` (`YYYY-MM-DD`, local time) and `items` (item ids in rank order). An item that was never
focused is in today's group.

**`ActivityInfo`**: `working` (a focus span is open and neither idle nor a lock holds), `away_since` and `away_until`
(the latest not-working gap of at least the idle threshold; `away_until` is absent while still away),
`session_started_at` (the current work session; absent when there is none) and `active_seconds_session`.

**`AwayChange`**: a waiting entry of `/now` whose `since` falls in the latest absence, on an item not focused since the
absence began. It carries the item's `id` (`item`), `name` and `go`, and the entry's `kind`, `reason` and `since`, most
urgent first. Focusing the item takes it off the list.

**`LastSession`**: `start`, `end`, `active_seconds` and `items` (ids in order of first focus) of the previous work
session (decision 0050). Absent when there is none.

`while_away` and `last_session` come from the working time and the items, so they are the same for every ranker, every
`q` and every `limit`.

### Ranking

- The daemon ranks with `rank`, else `[items] default_ranker`. When that ranker is not loaded (disabled, uninstalled or
  broken), the default in effect is the **`natural` floor**, and the daemon logs one warning at start. The floor orders
  items by the natural order (`2` before `10`) of their newest open place's workspace, then by key; it is not a
  plugin and cannot be disabled. `rank=natural` is always accepted.
- A ranker that raises, or returns another key set, a number that is not finite or a workspace order that is not a
  permutation, is logged by its name only, and the floor answers instead with `ranker_fallback: "natural"`.
- **Rankers read no clock; ages are active working time.** Nothing a ranker computes changes while the person is
  away. The one wall-time event is the work-session break, which makes the session just left the last one.
- The answer is memoized per ranker on (the live view's version, the span window's version, `days` and the time floored
  to 5 s), and the floored time is what the ranker gets. So equal keys mean equal inputs, two requests within one 5 s
  step get the same body, and a burst of `now.changed` costs one ranking.
- `docs/rankers.md` explains how to write, enable, disable and remove a ranker.

## `GET /rankers`

| Parameter | Meaning |
|---|---|
| `rank` | Optional: a client's stored choice, checked against `choices`. A choice that names no loaded ranker is not an error here (it is the input being checked); only one over 200 characters is **422**. |

```json
{
  "schema": "fylgja.rankers/1",
  "default": "frecency",
  "configured_default": "frecency",
  "rankers": [
    {"name": "frecency", "description": "Focus on working time, decayed; …", "origin": "built-in",
     "distribution": "fylgja", "version": "0.1.0", "enabled": true, "loaded": true},
    {"name": "mine", "origin": "external", "distribution": "my-rankers", "version": "1.0.0",
     "enabled": false, "loaded": false},
    {"name": "recent", "description": "Most recently focused first, in active working time", "origin": "built-in",
     "distribution": "fylgja", "version": "0.1.0", "enabled": true, "loaded": true},
    {"name": "workspace", "description": "Workspace number order, as today's workspace cards", "origin": "built-in",
     "distribution": "fylgja", "version": "0.1.0", "enabled": true, "loaded": true}
  ],
  "choices": [
    {"name": "frecency", "description": "Focus on working time, decayed; …", "default": true},
    {"name": "recent", "description": "Most recently focused first, in active working time", "default": false},
    {"name": "workspace", "description": "Workspace number order, as today's workspace cards", "default": false}
  ],
  "selected": "frecency",
  "stale_choice": false
}
```

| Field | Type | Holds |
|---|---|---|
| `schema` | string | `fylgja.rankers/1` |
| `default` | string | The default in effect: `[items] default_ranker` when it is loaded, else `natural` |
| `configured_default` | string | `[items] default_ranker` |
| `rankers[]` | `RankerInfo` | Every discovered ranker by name: `name`, optional `description` (a ranker that is not loaded was never imported, so it has none), `origin` (`built-in` or `external`), optional `distribution` and `version`, `enabled`, `loaded` and an optional `error` (`<kind>: <detail>`) |
| `choices[]` | `RankerChoice` | The loaded rankers only, in `rankers` order: `name`, optional `description`, and `default` (true for the default in effect) |
| `selected` | string | `rank` when it names a choice, else `default` |
| `stale_choice` | boolean | `rank` was given and names no choice: the client drops its stored choice, so the default applies |

`rankers` is the diagnostic list that `fylgja rankers` prints. A client that offers a choice renders `choices`,
marks the `default` one, shows `selected`, and clears its stored choice when `stale_choice` is true. When the default
in effect is the `natural` floor, no choice is the default and `selected` is `natural`.

## Privacy

Items are built from `/now` and from the stored focus spans, both redacted at ingest, so every title is one that
`/now` and `/history` already show. A window of an ignored app is never a span or a place. URLs carry item ids, never
paths, and the daemon logs counts and ranker names only: never a title, path, ticket or summary text.
