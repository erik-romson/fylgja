# Journal: `fylgja.journal/1`

Fylgja keeps a **journal per thread of work**. A thread is one checkout plus its branch. At a natural boundary (a turn
that went quiet, a session or a run that ended, the person leaving the project, a branch switch) a small model writes
one short entry, and proposes changes to a ledger of open items: `next`, `remember` and `blocked`. Plain code merges the
ledger. Every item and every entry names its sources. The rules are in
[decision 0057](../decisions/0057-workspace-journal.md).

The journal is **off by default**:

```toml
[journal]
enabled = true
```

The routes below are registered either way. While the journal is off, `GET /journal/threads` answers 200 with
`enabled: false` and no threads, and every other journal route answers **409**. Stored rows are left as they are.

The reads need no authentication; every route is `Host` checked. The four writes (the person's actions) are mutating routes behind the usual guards: bearer secret, `Origin`, `Content-Type` and body size. The answers follow the `/now`
serialization rule: **a field without a value is absent, never `null`**. Lists are always present. The models are in
`src/fylgja/api/journal_models.py` and are not reachable from `/now`, which is unchanged.

## Models

**`JournalSource`**: `kind` (`session`, `run`, `commit`, `todo`, `file`, `thread` or `person`), `ref`, `label`, and for a
`file` source `path` and `section`. **`EntrySource`** is a `JournalSource` with `id`, the `S` id the entry cited it by.

**`JournalItem`**: `id`, `kind` (`next`, `remember`, `blocked`), `text`, `status` (`open`, `done`, `dropped`), `pinned`,
`shown`, `source`, `created_at`, `closed_at`, `closed_by` (`model` or `user`), `todo_id` and `has_how`. Open items come
in this order within a kind: pinned first, then the newest `created_at`, then the higher sequence number. `shown` is true
for the first three open items of a kind.

**`Where`**: `text`, `generated_at` and `state`:

| `state` | When |
|---|---|
| `pending` | An attempt for the thread is reserved or running. |
| `failed` | The last attempt failed (`error_kind`). Any earlier `text` is kept. |
| `fresh` | The thread has a "where you are" text. |
| `missing` | Otherwise. |

**`ThreadBrief`** (the list): `id`, `name`, `folder`, `branch`, `last_at`, `where`, `next[]`, `remember[]`, `blocked[]`
(shown items only), `open` (the number of all open items) and `error_kind`.

**`JournalThreads`**: `schema`, `enabled`, `threads[]` (`ThreadBrief`, newest `last_at` first).

**`JournalThread`**: `schema`, `enabled`, `id`, `name`, `folder`, `branch`, `repo_root`, `last_at`, `where`, `open`
(`OpenItems`: `next[]`, `remember[]`, `blocked[]`, every open item with `shown`), `done_recent[]`, `entries[]`,
`error_kind` and `error_at`.

**`DoneDay`** (one local day of "Done lately"): `day` (`YYYY-MM-DD`), `relative` (`today` or `yesterday`, absent for
other days), `headlines[]` (up to three entry headlines of the day, newest first) and `items[]` (the items closed as
`done` that day, newest first). The list covers the last seven local days, today included, newest first; a day with
neither headlines nor items is left out. The days follow the local time zone, daylight saving included.

**`JournalEntry`**: `trigger` (`turn_done`, `session_end`, `run_finished`, `branch_changed`, `focus_left` or `manual`),
`started_at`, `ended_at`, `headline`, `did[]` and `sources[]` (`EntrySource`). `entries` are newest first.

**`JournalHow`**: `item`, `state` (`fresh`, `stale`, `pending`, `failed`, `missing`), `steps[]`, `generated_at` and
`error_kind`. The routes that answer it come with the "How do I do this?" action.

## `GET /journal/threads`

`?days=` (1–90, default 14) lists the threads whose latest entry is within `days`. A thread with no entry for 90 days is
never listed, but it is kept and is still readable by id. Out-of-range `days` is **422**.

## `GET /journal/threads/{id}`

`?entries=` (1–100, default 20) is the number of newest entries in `entries[]`. `id` is the 16-hex thread id.

| Status | When |
|---|---|
| 200 | `JournalThread`. A thread that is the current one of a known checkout but has no row yet answers the empty journal (`where.state` `missing`). |
| 404 | A malformed id, or an id that is neither a thread with a row nor the current thread of a known checkout. |
| 409 | The journal is off. |
| 422 | `entries` out of range. |

## `GET /journal/focused` and `GET /journal/checkout`

`/journal/focused` is the journal of the checkout of the focused window (the last window that is not private).
`/journal/checkout?path=` is the journal of the checkout that holds the absolute `path`; the daemon finds the git root.
Both answer a `JournalThread`, with `entries` at the default of 20.

| Status | When |
|---|---|
| 200 | `JournalThread`; the empty journal for a checkout with a thread identity but no row yet. |
| 404 | `nothing is focused`; or `no journal for this checkout`: no git checkout, a detached HEAD, or no branch seen yet. |
| 409 | The journal is off. |
| 422 | `path` is not absolute. |

## `PATCH /journal/items/{id}`

Body `{status?, pinned?, text?}`: `status` is `open`, `done` or `dropped`; `pinned` is a bool; `text` is cleaned
(whitespace collapsed, redacted) and must then be 1–200 characters. At least one field is required, and an explicit
`null` is **422**.

- `done` or `dropped` sets `closed_at` to now and `closed_by` to `user`.
- `open` on a closed item clears `closed_at` and `closed_by`, and marks the item edited.
- A new `text` marks the item edited. An edited or pinned item is never closed by the model.
- A change that changes nothing answers 200 and writes nothing.

| Status | When |
|---|---|
| 200 | The `JournalItem` after the change. |
| 404 | No item has this id. |
| 409 | The journal is off. |
| 422 | A bad body: no field, a `null`, an unknown field or status, or a `text` of the wrong length. |

A write publishes `journal.changed`. The action does not make the thread dirty, so it starts no entry.

## `POST /journal/threads/{id}/items`

Body `{kind, text}`: `kind` is `next`, `remember` or `blocked`; `text` follows the rule above. It adds an open, edited
item with source `{kind: "person", ref: <thread id>, label: "you"}`.

| Status | When |
|---|---|
| 201 | The `JournalItem`. |
| 404 | The thread has no row. |
| 409 | The journal is off. |
| 422 | A bad body, an unknown `kind`, or a `text` of the wrong length. |

## `POST /journal/items/{id}/todo`

Body `{}`. Makes a Fylgja todo from the item's text, scoped `project` to the thread's checkout, and links it
(`todo_id`). The linked todo is not journal input.

| Status | When |
|---|---|
| 201 | The new `Todo` (the model `POST /todos` answers with). |
| 200 | The item already has a todo that exists: the same `Todo`. |
| 404 | No item has this id. |
| 409 | The journal is off. |

When the todo is **done**, an open item becomes `done` with `closed_by: user` and `closed_at` set to the todo's
`done_at`. When it is **deleted**, the item's `todo_id` is cleared and the item stays as it is. Reopening the todo
changes nothing. Both are also checked once at daemon start. Each change publishes `journal.changed`.

## `POST /journal/threads/{id}/refresh`

Body `{}`. Asks for an entry now, with trigger `manual`. A manual attempt ignores the minimum interval between entries
and the wait after a failure.

| Status | Answer |
|---|---|
| 202 | `{thread, state: "pending"}`: an attempt was started, or one already runs for the thread (joined, so no second call). The finished attempt publishes `journal.changed`. |
| 200 | `{thread, state: "nothing_new"}`: the window since the last entry holds nothing, so no call is made. |
| 404 | The id is neither a thread with a row nor the current thread of a known checkout. |
| 409 | The journal is off. |

The answer comes once the attempt knows whether there is something new, which can take as long as a free model slot.

## The `journal` field of `/items`

A folder item carries `journal` (`thread`, `where`, `next[]` of `{id, text}`, `next_more`) while the journal is on and
its path is a checkout with a thread row. See [`items.md`](items.md).

## Events

`journal.changed` `{thread}` on `GET /events` follows every journal write. It carries the 16-hex thread id and never
text; `/now` is not touched.

## CLI

`fylgja journal` reads `/journal/focused`; `--here` reads `/journal/checkout` with the shell's directory; a thread id
reads `/journal/threads/{id}`; `--list [--days N]` reads `/journal/threads`; `--json` prints the answer.

| Exit | When |
|---|---|
| 0 | Printed. |
| 1 | The daemon answered 404 (its detail is printed) or 409 (`the journal is off; set [journal] enabled = true`). |
| 2 | Bad usage, for example `--here` together with a thread id. |
| 3 | The daemon is not running. |

## Raycast and other clients

- Reads need no authentication.
- A write sends `Authorization: Bearer <secret>` and `Content-Type: application/json`, and no `Origin`.
- An absent field means "no value": clients must not expect `null`.
- `useFetch`-style helpers turn a 404 into an error: `/journal/focused` answers 404 when nothing is focused.
