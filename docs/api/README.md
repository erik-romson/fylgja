# Fylgja HTTP API

The daemon serves the API on `127.0.0.1:7319` (`[api] port` in `config.toml`). Every answer is JSON unless noted.
The daemon does not serve OpenAPI (`/docs`, `/redoc` and `/openapi.json` are off in `api/app.py`): this page and the
pages it links are the reference. The response models live in `src/fylgja/api/*_models.py`; the routes live in
`src/fylgja/api/routes/` and `api/sse.py`. `PLAN-final-plan.md` §4.2 holds the original route list.

## Guards

Every route goes through the guards in `api/security.py` (A6) before any route code runs.

* **Host.** Only `127.0.0.1:<port>` and `localhost:<port>` are accepted: a foreign `Host` is **403**, a missing or
  repeated one **400**. No answer carries a CORS header.
* **Mutating methods** (`POST`, `PUT`, `PATCH`, `DELETE`), whatever the route, also need, in this order:
  1. `Authorization: Bearer <secret>` (**401**; without a secret file every write is refused). `fylgja install
     secret` creates the secret;
  2. an `Origin` from `[api] allowed_origins`, or no `Origin` together with the bearer, as the CLI sends (**403**);
  3. `Content-Type: application/json` (**415**);
  4. a body of at most 16 KiB, 64 KiB for `/sessions/{key}/message` (**413**).

Reads need no authentication. Errors are FastAPI's `{"detail": "..."}`; a detail never repeats a submitted text,
label or secret.

## Read routes

| Route | Query | Answer | Doc |
|---|---|---|---|
| `GET /now` | `rank` | `fylgja.now/1`: the whole picture in one call. `rank` changes only the order of `workspaces[]` | [`now.schema.json`](now.schema.json), [`items.md`](items.md#get-nowrank-and-get-viewoverviewrank), [`waiting.md`](waiting.md) |
| `GET /workspaces` | | `Workspace[]`, the `workspaces[]` of `/now`; empty while no window collector is available | |
| `GET /workspaces/{name}` | | `Workspace`; **404** when it is not open | |
| `GET /sessions` | | `Session[]`, the `sessions[]` of `/now`; empty while no session collector is available | |
| `GET /sessions/{key}` | | `Session` (`key` is `kind:native_id`; nested `children[]` are found too); **404** | |
| `GET /projects` | | `Project[]`, the `projects[]` of `/now`; empty while the section is absent | |
| `GET /todos` | `ws` \| `project` \| `session`, `status`, `order`, `limit` | `Todo[]`, see [Todos](#todos) | |
| `GET /next` | | `{list_id, items[]}`: "what next", best first | [`next.md`](next.md) |
| `GET /history` | `since`, `until` | `fylgja.history/1`: focus spans with per-workspace, project and app totals | [`history.md`](history.md) |
| `GET /items` | `rank`, `days`, `q`, `limit` | `fylgja.items/1`: one navigation item per git-root folder and per app, ranked | [`items.md`](items.md) |
| `GET /rankers` | `rank` | `fylgja.rankers/1`: the discovered rankers and the selected one | [`items.md`](items.md#get-rankers) |
| `GET /journal/threads` | `days` (1–90, default 14) | `fylgja.journal/1`: the threads with an entry lately, only each thread's shown items; `enabled: false` and no threads while `[journal] enabled = false` | [`journal.md`](journal.md#get-journalthreads) |
| `GET /journal/threads/{id}` | `entries` (1–100, default 20) | `fylgja.journal/1`: one thread's journal; **404** for an unknown or malformed id; **409** while the journal is off | [`journal.md`](journal.md#get-journalthreadsid) |
| `GET /journal/focused` | | `fylgja.journal/1`: the journal of the focused window's checkout; **404**; **409** while the journal is off | [`journal.md`](journal.md#get-journalfocused-and-get-journalcheckout) |
| `GET /journal/checkout` | `path` (absolute) | `fylgja.journal/1`: the journal of the checkout that holds `path`; **404**; **409** while the journal is off; **422** for a relative path | [`journal.md`](journal.md#get-journalfocused-and-get-journalcheckout) |
| `GET /view/overview` | `rank` | `fylgja.view.overview/1`: the page's Overview tab in one call | [`views.md`](views.md) |
| `GET /view/attention` | | `fylgja.view.attention/1`: the tab title and icon while an agent waits | [`views.md`](views.md#get-viewattention) |
| `GET /summaries/{scope}/{ref}` | | `SummaryResponse`, cached only; never starts a job | [`summaries.md`](summaries.md) |
| `GET /collectors` | | `CollectorRow[]`: `{name, origin, distribution, distribution_version, state, enabled, api_version, capabilities, health}`; a collector that is not loaded has `not_loaded` in the last three | |
| `GET /inspect/collectors` | | `fylgja.inspect.collectors/1`, with a `notify` block | [`inspect.md`](inspect.md) |
| `GET /inspect/pipeline` | | `fylgja.inspect.pipeline/1`, with a `notify` block | [`inspect.md`](inspect.md) |
| `GET /inspect/explain` | | `fylgja.inspect.explain/1` | [`inspect.md`](inspect.md) |
| `GET /health` | | `{status, version, now_schema, started_at, live_version, degraded[]}`; `status` is `ok`, `starting` or `stopping` | |
| `GET /events` | | Server-Sent Events, see [Events](#events) | |
| `GET /` | | The navigator page (HTML, the secret in a `<meta>` tag, never cached) | |
| `GET /favicon.png` | | The page's tab icon (PNG) | |
| `GET /favicon-attention.png` | | The tab icon with the attention dot (PNG) | |

A `rank` that names no loaded ranker (and is not `natural`) is **422** on `/now`, `/items` and `/view/overview`.

## Mutating routes

| Route | Body | Answers | Doc |
|---|---|---|---|
| `POST /todos` | `{text, ws? \| project? \| session?, status?: "open" \| "next", link?}` | **201** `Todo`; **409** no scope given and no workspace focused | [Todos](#todos) |
| `PATCH /todos/{id}` | `{text?, status?: "open" \| "next" \| "done"}`, at least one | **200** `Todo`; **404**; **409** for an imported todo | [Todos](#todos) |
| `DELETE /todos/{id}` | | **204** (soft delete); **404**; **409** for an imported todo | [Todos](#todos) |
| `PUT /workspaces/{name}/label` | `{label}`, 1 to 80 characters after whitespace is collapsed | **200** `Workspace`; **404** when it is not open | |
| `DELETE /workspaces/{name}/label` | | **200** `Workspace`, the inferred label shows again; **404** | |
| `POST /focus/window/{id}` | | **200** `{focused: "window" \| "tab", via}`; **404**; **502** naming the failed AeroSpace step | |
| `POST /focus/session/{key}` | | as above; **409** when the session has no window | |
| `POST /sessions/{key}/message` | exactly `{text}` | **200** receipt `{message_id, session_key, state, created_at, state_at, bytes, error_kind, detail}`; **409** while `[actions.message] enabled = false` (the default); **400** for a socket path or address in the body | |
| `POST /next/feedback` | `{list_id, rating: "good" \| "bad", suggestion_id?}` | **200** `{list_id, suggestion_id, rank, rating, feedback_at}`; **404** | [`next.md`](next.md#post-nextfeedback) |
| `POST /summaries/{scope}/{ref}` | ignored | **200** fresh; **202** `pending`; **409** `disabled`; **404** | [`summaries.md`](summaries.md) |

A body that is not an object with exactly the listed fields is **422**.

The message receipt's `state` is `submitted`, `observed-enqueued`, `unsupported`, `unreachable`,
`refused-locally`, `timed-out` or `unknown`. `submitted` means the whole line was written to the session's socket,
not that it was delivered.

## Todos

* `GET /todos` takes at most one scope filter: `ws`, `project` or `session`. `status` is a comma-separated list of
  `open`, `next` and `done` (default: every status). `order` is `created` (the default, oldest first) or
  `done_desc`. `limit` is 1 to 1000 (default: no cap). Deleted todos are never returned.
* Without a scope, `POST /todos` adds the todo to the focused workspace.
* An imported todo (a `TODO.md` line, an agent's task) is read-only: `PATCH` and `DELETE` answer **409** and name
  its source.

## Events

`GET /events` is a Server-Sent Events stream. Payloads carry ids, states and versions, never prompt text or titles.

| Event | Data | When |
|---|---|---|
| `now.changed` | `{version}`, also the SSE `id` | After every effective change of the live view; fetch `/now` to see what changed |
| `session.state` | `{session_key, state, version}` | A live session's state changed; `state` is `null` when it ended. Comes before the `now.changed` of the same update |
| `focus.changed` | `{window_id, workspace, version}` | Focus moved. Comes before the `now.changed` of the same update |
| `summary.ready` | `{scope, ref, state}` | A summary job finished ([`summaries.md`](summaries.md)) |
| `journal.changed` | `{thread}` | A journal entry, item or answer changed; only while `[journal] enabled = true` |

A `: hb` comment arrives every 15 s. At most 16 clients can connect; the next one gets **503**. A slow client loses
its oldest frames; since every event is a hint to fetch `/now`, a later event repairs the loss.

## Other contracts

| Doc | Contract |
|---|---|
| [`now.schema.json`](now.schema.json) | The JSON Schema of `fylgja.now/1`, generated with `scripts/gen_now_schema.py`; a test guards it against changes without a version bump |
| [`inbox-envelope.md`](inbox-envelope.md) | `fylgja.hook/1`, the file hooks drop into the inbox (not HTTP) |
| [`lidskjalv-mapping.md`](lidskjalv-mapping.md) | Which `/now` path replaces each field Lidskjalv reads from its own cache |
