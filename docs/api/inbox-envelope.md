# Hook inbox envelope: `fylgja.hook/1`

The hook inbox carries agent events from short-lived writers to the daemon. Each event is one JSON file, the
*envelope*. This page is the stable, versioned contract for that file. Several writers produce it:

| Writer | Inbox | Work package |
|---|---|---|
| `fylgja hook <Event>`, run by Claude Code as an async command hook | `hook-inbox/claude/` | WP2.1 |
| `fylgja hook --source <name> <Event>`, for agents with command hooks (Codex) | `hook-inbox/<name>/` | WP3.x |
| the OpenCode plugin (`fylgja.ts`, optional) | `hook-inbox/opencode/` | WP3.2 |
| the norn event subscriber (A10) | `hook-inbox/norn/` | WP3.3 |

The daemon drains every inbox (WP2.2). It deletes a file only after the event has been committed to SQLite, and it
uses `id` as the deduplication key, so a replay after a crash is harmless. It minimizes the payload before
persistence (A5); the raw payload lives only in the spool.

The golden example is [`tests/fixtures/envelope/fylgja.hook-1.json`](../../tests/fixtures/envelope/fylgja.hook-1.json).
`tests/unit/test_envelope_golden.py` checks that an envelope built by `fylgja.hook` has the golden's keys and
types, and that the field table below lists exactly those keys. External writers test their output against the
same golden.

## Fields

Every envelope is a JSON object with exactly these keys, in this order.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | Always `"fylgja.hook/1"`. |
| `id` | string | A random UUID (version 4), lowercase with hyphens. Unique per envelope; the drain's deduplication key. |
| `received_at_ns` | integer | When the writer received the event, in nanoseconds since the Unix epoch. `fylgja hook` takes it as soon as it starts. Events are ordered by this value. |
| `source` | string | The inbox name: `claude` by default. It matches `[A-Za-z0-9][A-Za-z0-9_-]*`, is at most 64 characters, and equals the name of the inbox directory. |
| `event_arg` | string or null | The event named on the command line (`fylgja hook Stop` gives `"Stop"`), at most 64 characters; `null` when none was given. The drain prefers the payload's own event name (`hook_event_name` for Claude) and falls back to this one. |
| `ppid` | integer | The parent pid of the writer process. For Claude this is usually the shell that ran the hook command. `env.CLAUDE_PID` names Claude itself. |
| `env` | object of string to string | The allowlisted environment variables (below) that were set and non-empty, each cut to 256 characters. It may be empty. |
| `payload_text` | string | The event payload as text, capped as described under "Payload size caps". The writer does not parse it below 256 KiB, so it may be invalid JSON. |
| `truncated` | boolean | `true` when `payload_text` is not the complete input: long strings were cut, the oversize marker replaced the payload, or stdin did not reach end-of-file within the 2 s deadline. |
| `fylgja_version` | string | The version of the software that wrote the envelope: the Fylgja package version for `fylgja hook`, the plugin's own version for external writers. Informational only. |

**Versioning.** Adding, removing or retyping a field, or changing what a field means, creates a new schema
version (`fylgja.hook/2`). During a transition the drain accepts both versions. It moves an envelope with an unknown
`schema` to `quarantine/`.

## Environment allowlist (A3)

An envelope copies only these variables, and only when they are set and non-empty:

- `CLAUDE_CODE_SESSION_ID`: the session id. It matches the payload's `session_id` and changes on `/clear`.
- `CLAUDE_PID`: the pid of the Claude Code process (Claude Code 2.1.214 and later).
- `CLAUDE_CODE_MESSAGING_SOCKET`: the session's inbox socket path (2.1.224 and later). It is a path, not a credential.
- `CMUX_WORKSPACE_ID`: the cmux workspace of the terminal.
- `CMUX_SURFACE_ID`: the cmux surface.
- `CMUX_TAB_ID`: the cmux tab.
- `CMUX_PANEL_ID`: the cmux panel.
- `ITERM_SESSION_ID`: the iTerm2 session.
- `TERM_SESSION_ID`: the Terminal or iTerm2 session id.
- `TERM_PROGRAM`: the terminal program.
- `CLAUDE_CODE_BRIDGE_SESSION_ID`: the Remote Control session id, present only while Remote Control is on.

Nothing else is ever copied. In particular `CLAUDE_CODE_MESSAGING_TOKEN` and `CMUX_SOCKET_CAPABILITY` never
enter an envelope (`tests/unit/test_no_token_leak.py`). The environment is never copied wholesale.

## Payload size caps

- **Up to 256 KiB** (262 144 bytes): kept verbatim. The bytes are decoded as UTF-8, and any invalid byte becomes
  U+FFFD. `truncated` is `false`, unless the stdin deadline cut the input.
- **Above 256 KiB, valid JSON:** the payload is parsed and written back as compact JSON. Every string value whose
  UTF-8 encoding exceeds 4 KiB (4 096 bytes) is replaced by `{"_truncated": <bytes>}`, where `<bytes>` is the
  original string's UTF-8 length. Object keys and non-string values are kept. `truncated` is `true`.
- **Above 256 KiB and not valid JSON, or more than 4 MiB read:** the payload becomes the marker
  `{"_malformed_oversize": <bytes>}`, where `<bytes>` counts every byte read from stdin. `truncated` is `true`.
  Input beyond 4 MiB is read and discarded, so the writer on the other end of the pipe never blocks.
- **Stdin deadline:** `fylgja hook` reads stdin for at most 2 s. When the deadline passes before end-of-file, what
  arrived is capped by the same rules and `truncated` is `true`.

## Inbox directory and atomic write

- **Directory:** `hook-inbox/<source>/` under the data directory. That is `$FYLGJA_DATA_DIR`, or
  `~/.local/share/fylgja` by default (A16), so the default inbox is `~/.local/share/fylgja/hook-inbox/<source>/`.
- **Directory checks:** a writer creates the directory, and any missing parent, with mode 0700. Before every write
  it checks the directory with `lstat`. It writes nothing when the path is a symlink, is not a directory, is owned
  by another user, or has any mode other than 0700. The refusal is silent.
- **File name:** `<received_at_ns>-<id>.json`, for example `1791231759255399000-6f1e2d3c-4b5a-4c6d-8e7f-0a1b2c3d4e5f.json`.
  Sorting by name gives receive order.
- **Write sequence:**
  1. Open `.tmp-<id>` in the inbox with `O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW` and mode 0600.
  2. Write the whole envelope as one line of compact JSON, followed by `\n`. `fylgja hook` escapes every non-ASCII
     character, so the file is plain ASCII.
  3. `fsync` the file, close it, then `rename` it to the final name.

  A reader therefore never sees a partial envelope under a final name.
- **What readers take:** only regular files whose names match `^[0-9]+-[0-9a-f-]{36}\.json$`, in name order. They
  ignore dot files (`.tmp-*`, `.backlog-stamp`), `dropped.count` and the `quarantine/` subdirectory. A `.tmp-*`
  file older than a minute is debris from a writer that was killed mid-write, and the daemon may delete it.

## Backlog limit

A writer checks the backlog at most once every 60 s.

- **The stamp:** `.backlog-stamp` in the inbox records the last check. Its mtime is the time of the last scan, and
  its content is that scan's verdict, `ok` or `full`. A stamp younger than 60 s is trusted. An older or missing
  stamp is refreshed first, so that concurrent writers keep the previous verdict, and then the writer scans.
- **The scan:** counts the envelope files (`*.json`, excluding dot files) and their total size. The verdict is
  `full` above 100 000 files or above 512 MiB.
- **While the verdict is `full`:** the event is dropped. The writer increments the decimal counter in
  `dropped.count` (one line, created with mode 0600), holding an exclusive `flock` while it does. `fylgja doctor`
  reports that counter.

External writers follow the same rules. As a minimum, they write nothing while `.backlog-stamp` says `full`.

## Statusline snapshots (A4)

`fylgja hook StatusLine` writes the same envelope, with `event_arg` `"StatusLine"`, but it never writes to the
inbox and it skips the backlog check:

- **Target:** `statusline/<session_id>.json` under the data directory, replaced atomically. The temporary file is
  `.tmp-<id>`, written, fsynced and then renamed over the target.
- **The session id:** taken from the payload's `session_id`, or from `CLAUDE_CODE_SESSION_ID` when the payload has
  none. It must match `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`; otherwise nothing is written.
- **Coalescing:** nothing is written while the existing snapshot is less than 2 s old (by its mtime).
- **Directory:** the same checks as the inbox (mode 0700, owned by the user, not a symlink).

The daemon reads a snapshot again whenever its mtime changes.

## The `fylgja hook` process

- **Command line:** `fylgja hook [--source <name>] [<Event>]`. The console script dispatches `hook` before it
  imports anything outside the stdlib. Unknown flags are ignored, and `-h` or `--help` writes nothing.
- **Silence first:** the hook points fds 1 and 2 at `/dev/null`, then arms a 3 s `signal.alarm` whose handler calls
  `os._exit(0)`.
- **Exit status:** always 0, and nothing is ever written to stdout or stderr. An event that cannot be written is
  simply lost.
- **Summarizer sessions:** when `FYLGJA_SUMMARY` or `LIDSKJALV_TRACK_SUMMARY` is set and non-empty, the hook exits
  at once without reading stdin. This keeps summarizer sessions out of the inbox.
- **What it never does:** model work, network or HTTP calls, database access, configuration loading, or reading any
  file other than its own stamp and counter.

## How the daemon drains the inbox (WP2.2)

The Claude collector watches `hook-inbox/claude/` (a kqueue watch on macOS, a 1 s poll otherwise), takes the files
named `<received_at_ns>-<id>.json` in name order, normalizes each envelope into a minimized `hook_event` record
(decision 0032), commits it, and only then deletes the file. The record's dedup key derives from the envelope
`id`, so a file that survives a crash is ingested again as a no-op. A file that is not a valid `fylgja.hook/1`
envelope for its inbox, names no session or event, or whose event the ingest gate rejects, moves to `quarantine/`,
where maintenance deletes it after 7 days. The daemon also deletes `.tmp-*` files older than a minute.

## The OpenCode writer (WP3.2)

`src/fylgja/collectors/opencode/fylgja.ts` is an optional OpenCode plugin (copied to
`~/.config/opencode/plugins/` by hand) that writes this envelope with `source` `opencode` for the OpenCode events
`session.status`, `permission.asked` and `todo.updated`. It follows the rules above (atomic rename from an `O_EXCL`
temporary file, mode 0600, an inbox with mode 0700 owned by the user, nothing while `.backlog-stamp` says `full`).

- `event_arg` is the OpenCode event name. `ppid` is the plugin host's parent pid, and `env` holds only the terminal
  identifiers of the allowlist (OpenCode sets none of the `CLAUDE_*` variables). `fylgja_version` is the plugin's own
  version.
- The payload is a JSON object with `session_id` (OpenCode's `ses_…` id), `hook_event_name` (the event name), `cwd`,
  and per event `reason` (the `session.status` type: `busy`, `idle` or `retry`) or `tool_name` (the permission of
  `permission.asked`). These are field names the drain's minimizer already keeps (A5); nothing else of an OpenCode
  event is sent.
- The OpenCode collector drains `hook-inbox/opencode/` as described above, with `kind` `opencode` (decision 0036).
  Each committed envelope also wakes its database poll.

The golden is [`tests/fixtures/envelope/opencode-session-status.json`](../../tests/fixtures/envelope/opencode-session-status.json),
generated by `scripts/gen_opencode_envelope.py` with `bun`. `tests/unit/test_opencode_envelope_compat.py` reads it with
the Python reader, compares its keys and types with `fylgja.hook-1.json`, and checks that every field name in
`fylgja.ts` is in this contract.
