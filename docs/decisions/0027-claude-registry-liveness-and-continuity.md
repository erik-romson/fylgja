# 0027: Claude registry liveness compares instants; continuity fields ride on the session record

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A9, A11, A13
- Date: 2026-10-05

## Context

WP1.7 says a Claude row is live when its pid is alive and the process start time equals `procStart`. The
revalidation (`docs/revalidation/2026-10-05-claude-registry.md`) found that `procStart` is wall-clock text in
**UTC**, while `ps -o lstart=` prints local time, so comparing the two strings never matches away from UTC. One
live row also has no `procStart`. A11 asks for `process_key` and a predecessor link, and A9 for the per-session
`version` and `peerProtocol`, but `SessionPayload` (step 05) carried none of them, and the store's upsert did not
write the existing `process_key` and `predecessor_key` columns.

## Decision

- **Liveness compares instants.** `procStart` is read as UTC and the `ps` text as local time (daylight saving
  included), and the two match within one second. A row without `procStart` is live unless its process started
  more than five seconds after the row's `startedAt`, which a reused pid must have. One `ps -o pid=,lstart= -p
  <pids>` call per poll answers for every row; exit status 0 and 1 are answers.
- **`process_key` is `<pid>-<start epoch seconds>`**, taken from `ps`, so it is the same for every row of the same
  process whether or not `procStart` is present.
- **Continuity** lives in the collector's memory: when a live process shows a new `sessionId`, the new session's
  `predecessor_key` is `claude:<old id>`, set once and never looped. After a daemon restart a rotation that
  happened while it was down is not linked by the registry alone; the hook path (WP2.3) links it.
- **Session record.** `SessionPayload` gains the optional `process_key`, `predecessor_key`, `entrypoint`, `version`
  and `peer_protocol`. The writer's session upsert now writes `process_key` and `predecessor_key` (columns that
  already exist, merged with `coalesce` like the other fields). `entrypoint`, `version` and `peer_protocol` have no
  column yet: they travel on the record, and the messaging step (WP3.x) reads a target's `version` and
  `peerProtocol` from its registry row at send time. No migration is added here.
- **State** maps registry `status`: `idle` to `idle`, `busy` to `working`, `waiting` to `waiting`, with
  `waitingFor` as `waiting_reason`. Hook states replace them from WP2.3.

## Consequences

- The poll cost is one `stat` per row and one `ps` per 2 s.
- Tests build `ps` output from an instant in local time and `procStart` in UTC, so they pass in any time zone.
- A later step that wants `entrypoint` or the version in `/now` adds columns in a new migration; the record
  already carries the values.
