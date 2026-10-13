# `GET /inspect/*`

The inspect routes show how collection works right now. Each one is a read route: no authentication, Host-checked
like every route, `GET` only. Each answer has its own schema string, `fylgja.inspect.<name>/1`. The answers are read
from the daemon's memory: no route runs `fylgja doctor` or `osascript`, or reads SQLite. `fylgja.now/1` and
`GET /collectors` are unchanged. See decision 0048.

| Route | Schema | Shows |
|---|---|---|
| `GET /inspect/collectors` | `fylgja.inspect.collectors/1` | Each collector's state, redacted settings and accepted records per kind; the tables with no collector installed; the AppleScript switches |

A field without a value is absent, never `null`. **Every counter starts at zero when the daemon starts** (`started_at`);
nothing is persisted.

## `GET /inspect/collectors`

```json
{
  "schema": "fylgja.inspect.collectors/1",
  "generated_at": "2026-10-05T12:00:00Z",
  "started_at": "2026-10-05T08:00:00Z",
  "collectors": [
    {
      "name": "wm",
      "origin": "built-in",
      "distribution": "fylgja",
      "distribution_version": "0.1.0",
      "state": "enabled",
      "enabled": true,
      "loaded": true,
      "api_version": 1,
      "capabilities": ["windows"],
      "entity_kinds": ["focus", "window", "workspace"],
      "health": {"state": "ok", "since": "2026-10-05T08:00:01Z", "last_ok_at": "2026-10-05T11:59:40Z", "restarts": 0},
      "settings": {"snapshot_interval_seconds": 30, "api_token": "[redacted]", "socket": "~/.wm/socket"},
      "records": {
        "accepted": 1520,
        "dropped": 3,
        "lost": 0,
        "rejected": {"schema": 0, "source": 0, "entity_kind": 0, "timestamp": 1},
        "kinds": [
          {"entity_kind": "focus", "record_type": "event", "accepted": 610, "last_at": "2026-10-05T11:59:58Z"},
          {"entity_kind": "window", "record_type": "snapshot", "accepted": 480, "last_at": "2026-10-05T11:59:30Z"},
          {"entity_kind": "workspace", "record_type": "snapshot", "accepted": 430, "last_at": "2026-10-05T11:59:30Z"}
        ],
        "last_at": "2026-10-05T11:59:58Z"
      }
    },
    {
      "name": "vendor",
      "origin": "external",
      "distribution": "fylgja-vendor",
      "distribution_version": "2.1.0",
      "state": "disabled",
      "enabled": false,
      "loaded": false,
      "health": {"state": "disabled", "since": "2026-10-05T08:00:00Z", "restarts": 0}
    }
  ],
  "not_installed": ["gone"],
  "applescript": [
    {"name": "chrome", "configured": true, "doctor_ok": false, "doctor_at": "2026-10-05T07:55:00Z", "doctor_reason": "timeout"},
    {"name": "iterm2", "configured": false}
  ]
}
```

- `collectors` holds every collector the registry knows, in registry order (by name, then distribution). A name that
  more than one distribution claims shows up once per claimant; the rejected ones have `state = "rejected"`.
- `loaded` is false for a collector whose code was not imported: a disabled external plugin, a rejected duplicate,
  or one whose import failed. Such a collector shows its metadata and health only (no `api_version`, `capabilities`,
  `entity_kinds` or `settings`). A loaded collector whose settings failed validation has no `settings` either; its
  `health` says why.
- `entity_kinds` are the kinds the ingest gate lets the collector emit (from its capabilities).
- `health` is what the daemon holds: the supervisor's own `doctor()` probe of the collector (every 30 s), merged
  with the ingest gate's degraded state (more than 20 rejected records in a minute). `retry_in_seconds` is set while
  the collector waits in backoff. Run `fylgja doctor` for the full setup check.
- `records` is absent for a collector that does not run (disabled, not loaded, or failed to load). The counts are the
  ingest gate's:
  - `accepted`: records that passed the gate; `kinds` splits them by entity kind and record type (`event` or
    `snapshot`), sorted by kind, then type, with the time of the last one;
  - `dropped`: records the privacy filter dropped (an ignored app or title);
  - `lost`: accepted records that could not be queued because the writer was stopped;
  - `rejected`: per gate reason (`schema`, `source`, `entity_kind`, `timestamp`), every reason listed;
  - `last_at`: when the collector's last record was accepted.

  They count records as they arrive, before the writer: a snapshot of a projected kind (windows, workspaces,
  sessions) is counted although it has no `events` row.
- `not_installed` lists the `[collectors.<name>]` tables (the packaged defaults and the user's `config.toml`) with no
  entry point behind them, sorted.
- `applescript` lists the `[actions.applescript]` switches in the order doctor checks them: `configured` is the
  switch's value, and `doctor_ok`, `doctor_at` and `doctor_reason` are the outcome `fylgja doctor` last recorded in
  its state file (`applescript-switches.json` in the state directory, read up to 16 KB). They are absent when doctor
  has not checked that switch. The route never runs a switch.

### Redaction

`settings` are the collector's validated `[collectors.<name>]` settings (without `enabled`), dumped as JSON, then:

1. the value of every key that names a secret is replaced by `"[redacted]"`. The rule is the regular expression
   `(?i)(^|_)(secret|token|password|passwd|key|auth|credential|cookie)s?($|_)`, so `api_token`, `password`, `keys` and
   `auth_header` match, while `keyboard` and `authority` do not;
2. every string goes through the `[privacy] redact_patterns` filter, so a token under a neutral key is redacted too;
3. a string that starts with the home directory has that prefix replaced by `~` (`~/projects`);
4. a setting without a value is left out.

Nested tables and lists take the same rules; numbers and booleans are shown as they are. A health `detail` and a
`doctor_reason` take rules 2 and 3.
