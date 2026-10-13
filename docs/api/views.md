# Views: page-shaped answers under `/view/`

`/now` (`fylgja.now/1`) is the data contract that the CLI and Lidskjalv read, and it stays frozen. A
**view** is a separate, page-shaped contract. It carries what one tab of the navigator page shows: capped,
ordered and grouped, with every click target already decided. The page can then change without touching
`/now`.

## The rule: the daemon computes, the page renders

The page may:

- render what a route returns, in the order it returns it;
- format an instant as local time or date (`fmtTime`), and a number of seconds as a duration (`fmtDuration`:
  `3 h 20 m`, `45 m`, `30 s`);
- map a fixed id to a display label (`SOURCE_LABELS`);
- show and hide tabs, and keep UI choices (tab, ranker) in the hash or `localStorage`;
- debounce input and throttle reloads.

It may not sort, filter, group, cap, count across items, subtract times, score, or decide a target. The
daemon does those, so every client gets the same answer. `test_the_page_computes_nothing` checks the
script for `.sort(`, `.filter(`, `.reduce(`, `.find(`, `.some(`, `.every(`, `.slice(`, `.flatMap(`,
`const MAX_`, `Date.now() -` and `- new Date(`. A tab that needs more than formatting gets its own
`GET /view/<tab>`.

## `GET /view/overview`

The Overview tab in one call. It is a read: no authentication, and the `Host` header is checked as on
every route. The answer is `fylgja.view.overview/1`, under the `/now` serialization rule: a field
without a value is absent, never `null`. Lists are always present.

The view is built from the same three answers the page used to fetch itself:

- `/now`;
- the open and next todos, oldest first, as `GET /todos?status=open,next` lists them;
- `GET /next`, through the same function, so `next_list_id` is the `list_id` that `GET /next` gives
  for the same state. An unchanged ranking is the same list, so the call logs no extra suggestion
  list.

`?rank=<name>` names the ranker that orders `cards[]`: the cards come in the ranker's workspace order, and a
workspace it does not name follows in `/now` order. Without `rank`, the order follows `[items] default_ranker`
(`natural` when that ranker is not loaded). A `rank` that names no loaded ranker and is not `natural` is a
**422**. The page passes its stored ranker choice.

| Field | Type | Holds |
|---|---|---|
| `schema` | string | `fylgja.view.overview/1` |
| `generated_at` | instant | The `/now` it was built from |
| `focused_workspace` | string, optional | `focused.workspace`, else the first workspace flagged `focused` |
| `next[]` | `NextEntry` | The `GET /next` items, best first |
| `next_list_id` | string, optional | The `list_id` for `POST /next/feedback` |
| `cards[]` | `Card` | One per workspace with a window, a session or a todo, in the order of `?rank=` or `[items] default_ranker` |
| `todo_groups[]` | `TodoGroup` | The todo list, in display order |

**`FocusTarget`** is what a click focuses:

- `kind` is `session` (`POST /focus/session/{ref}`) or `window` (`POST /focus/window/{ref}`);
- `ref` is the session key or the AeroSpace window id.

**`NextEntry`** has `rank` (1 is the top), `kind`, `reason` and an optional `go`. `go` follows the item's
`target_ref`, trying each rule in turn:

1. a top-level session key focuses that session;
2. a project path whose project has a `workspace` focuses that workspace's window;
3. a window id focuses that window;
4. a workspace name focuses its window;
5. the id of an open todo of workspace scope focuses that workspace's window.

"A workspace's window" is its focused window, else its first. `go` is absent when no rule gives a
window.

**`Card`** has these fields:

- `name`, `label` (the effective label, else the inferred one), `focused`, `windows` (a count) and
  `one_liner`;
- `go`: the workspace's focused window, else its first; absent without a window;
- `projects[]` (`CardProject`): `name`, `branch`, `dirty`, `pr_number`, `pr_checks`, `build_state` and
  `build_failed`, which is true when `build_state` is `fail`;
- `sessions[]` (`CardSession`): `key`, `state` (`unknown` when none), `waiting`, `text` (the one-liner,
  else the title, name or kind) and `go` (the session). Waiting sessions (`waiting`,
  `waiting_permission`, `waiting_input`, `turn_done`) come first; otherwise the `/now` order holds;
- `context[]` (`CardContext`): `text` (the title, else the kind) and `unread`, flattened over the
  card's windows in window order;
- `todos[]` (`CardTodo`): `text` and `next`, in the `/now` order (`next` first, then oldest first).

The caps are `MAX_SESSIONS = 6`, `MAX_CONTEXT = 3` and `MAX_TODOS = 4`. `sessions_more`, `context_more`
and `todos_more` count what each cap leaves out.

**`TodoGroup`** has `title`, `kind`, an optional `empty` text and `todos[]` (`TodoRow`). The groups come
in this order:

1. `focused`: "Focused workspace `<name>`" with that workspace's todos, or `empty` "Nothing open here".
   It is present only while a workspace is focused;
2. `all`: "All workspaces". It has no todos of its own; `empty` is "Nothing else open" when no other
   todo exists;
3. `scope`: one group per workspace, project or session, holding the other todos in first-seen order.
   The titles are:
   - a session: its title, one-liner or name, then its project's name and its kind, joined by ` · `.
     A session `/now` does not show is "Session " and the ref, cut to its last 40 characters with a
     leading `…`;
   - a project: "Project " and the project's name, else the last part of its path;
   - a workspace: "Workspace " and the name, then its label;
   - a todo without a scope: "No scope".

**`TodoRow`** has `id`, `text`, `next`, `source` and `can_finish`. `can_finish` is true for a native todo
(`source` `fylgja` or absent). An imported todo is finished where it came from: the page names its source
with `SOURCE_LABELS` and shows no Done button.

### Example

```json
{
  "schema": "fylgja.view.overview/1",
  "generated_at": "2026-10-05T12:00:00Z",
  "focused_workspace": "2",
  "next": [{"rank": 1, "kind": "input", "reason": "waiting for input", "go": {"kind": "session", "ref": "claude:abc-123"}}],
  "next_list_id": "nl-0123456789abcdef",
  "cards": [{
    "name": "2", "label": "alpha", "focused": true, "windows": 1, "go": {"kind": "window", "ref": "100"},
    "projects": [{"name": "alpha/main", "branch": "CBS-1", "dirty": false, "build_failed": false}],
    "sessions": [{"key": "claude:abc-123", "state": "waiting_input", "waiting": true, "text": "waiting for input",
                  "go": {"kind": "session", "ref": "claude:abc-123"}}],
    "sessions_more": 0, "context": [], "context_more": 0,
    "todos": [{"text": "write the release notes", "next": true}], "todos_more": 0
  }],
  "todo_groups": [
    {"title": "Focused workspace 2", "kind": "focused",
     "todos": [{"id": "td-1", "text": "write the release notes", "next": true, "source": "fylgja", "can_finish": true}]},
    {"title": "All workspaces", "kind": "all", "empty": "Nothing else open", "todos": []}
  ]
}
```

Logic: `src/fylgja/api/views.py` (pure), `src/fylgja/api/view_models.py` (the models) and
`src/fylgja/api/routes/views.py`.
