# 0048: Inspect read routes

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A5, A6, A7
- Date: 2026-10-13

## Context

The navigator page gains diagnostic tabs (Collectors, Data flow, Explain) that show how collection works: each
collector's state and settings, what it ingested per record kind and when, and the AppleScript switches. That data
lives in the daemon (the registry, the supervisor's health, the ingest gate). `GET /collectors` returns
`CollectorRow`, the shape `fylgja collectors` prints and `fylgja doctor`'s daemon check parses, so it cannot grow.
`/now` (`fylgja.now/1`) is the hot contract Lidskjalv reads, and diagnostics do not belong in it. A `GROUP BY` over
`events` would undercount: the writer stores snapshots of projected kinds (windows, workspaces, sessions, activity,
usage) without an `events` row.

## Decision

- **A route family under `/inspect/`.** `GET /inspect/collectors` first; the pipeline and the explain routes follow
  under the same prefix. Each route has its own schema string, `fylgja.inspect.<name>/1`, so one can change without
  the others. Models are `NowModel`s in `api/inspect_models.py`, built by pure functions in `api/inspect.py` (no
  FastAPI, no I/O) and served by thin routes in `api/routes/inspect.py`. A missing value is absent, never `null`.
- **GET only, no authentication**, Host-checked like every read route (A6). There is no mutating inspect route.
- **Memory only.** The routes read the registry, the collector states, the gate's counters and the configuration.
  They never run `fylgja doctor` (doctor stays a CLI process, A7) or `osascript`, and never read SQLite. The one
  file read is doctor's own AppleScript switch state file, at most 16 KB, off the event loop. The switches are
  reported, never run.
- **The ingest gate counts per kind.** `PluginStats` gains `kinds` (accepted records per entity kind and record type)
  and `last_at` (the gate's clock at the last one), counted on accept on the thread that already runs `check()`.
  Dropped, rejected and unknown records are not counted as kinds; a record accepted and then lost to a stopped
  writer is. The counters live in memory and start again at zero with the daemon.
- **Privacy.** Settings are dumped as JSON, then a value under a secret-named key becomes `[redacted]`, every string
  goes through the `[privacy] redact_patterns` filter (the same instance the gate uses, `Daemon.privacy`), the home
  prefix becomes `~`, and an unset value is left out. Health details and doctor reasons take the string rules too.
  No record content is shown: only counts, kinds and times (A5).
- **No collector is special.** The builders treat every plugin alike; no built-in collector name is a dispatch key.
- **Unchanged:** `/now`, `now.schema.json`, `GET /collectors` and `CollectorRow`.

## Consequences

- The Collectors tab renders `/inspect/collectors` as it comes; the page computes nothing (decision 0047).
- `docs/api/inspect.md` documents the answers and the redaction rules. The route sits in `READ_ROUTES` of the
  Host-guard test and in the token-leak sweep.
- Counters reset on restart, so a quiet collector shows zero after a restart until it emits again.
- A new settings key that holds a secret under a neutral name is caught only when its value matches a
  `[privacy] redact_patterns` entry; plugin authors should name secret settings by the key rule.
