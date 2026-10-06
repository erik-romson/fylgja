# 0045: Tokens, usage and quota: details

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A20, A21, A24
- Date: 2026-10-06

## Context

WP5.4 part 2 fills the rest of §4.7: `sessions[].tokens` from Lidskjalv's `_COSTS_JQ`, `sessions[].cost_usd` and
`context_pct` from the statusline, and `usage` from the statusline's `rate_limits` and the CodexBar CLI. The plan fixes the
shapes, the storage (`session_snapshots` and `usage_snapshots` as change intervals) and the freshness rule. It leaves open
how a reading that changes with every message is stored without opening an interval each time, where a usage section gets
its `observed_at`, `error_kind` and poll interval when a change-interval row has no column for them, how provider
failures are told apart from "no data", and what to do about the token sums being wrong. `docs/revalidation/
2026-10-06-usage.md` found that `_COSTS_JQ` counts every transcript line, that CodexBar exits 1 and still prints JSON, and
that CodexBar cannot answer for Copilot here.

## Decision

- **Readings are updated in place.** Context percentage, cost and the four token sums change with every message. They
  are not state, so a change never opens an interval (A20). `SessionActivity` carries them (`tokens`, `context_pct`,
  `cost_usd`), and the writer updates the open `session_snapshots` row in place; a new interval (a state change) carries
  them over, as the schema already provides for. The Claude collector puts them on the activity it emits: the sums from
  the transcript tail, the cost and percentage from the statusline reader's newest reading per session, so a statusline
  snapshot read before the session appears in the registry is not lost.
- **The token sums are an exact port, overcount included.** `_COSTS_JQ` sums every assistant line with a usage object.
  Claude writes one line per content block, each repeating the message's usage, so a real transcript is counted about
  three times over (246 lines for 78 messages in the sample). The port, and the goldens it is checked against, keep that
  behaviour, because Lidskjalv's switch to Fylgja (WP5.5, A24) must not change the numbers it shows. A sum with one line
  per message id is the refinement, to be added as its own field, not by changing this one.
- **Observation metadata is remembered in memory.** A change-interval row says when its reading *changed*. `UsageMemory`
  (`core/usage_view.py`), fed after the ingest gate like the context items, remembers for each row when it was last
  observed, the poll interval its source named (`UsagePayload.poll_interval_s`) and for each provider and kind (`usage` or
  `cost`) the short code of the last failed attempt (`UsagePayload.error_kind`). `/now` joins that with the stored rows.
  No schema change was needed. After a restart a row falls back to its stored start time and a 5 minute interval.
- **A failed attempt is an item that carries no reading.** It is emitted in the same complete snapshot as the readings; the
  writer stores nothing for it and keeps that provider's rows open, so `/now` shows the last reading as `unavailable` with
  the `error_kind`. A provider that has only ever failed is listed with the `error_kind` and no reading. The section as a
  whole is as good as its best part, so one provider failing for good (Copilot) does not make the section `unavailable`.
- **CodexBar failures never degrade the collector**, as gh problems do not. They show as `unavailable`, and the collector's
  health detail carries a `usage:` clause, which is also where "no data yet" and "disabled" are explained when `usage` is
  absent.
- **Which source covers which provider.** Claude's quota is the statusline's; Codex's is CodexBar's; Copilot's is not
  covered on this machine, so the mapping says Lidskjalv's quota switch keeps its own source for it. Spend is CodexBar's
  local scan for `codex` and `claude` (30 days, list price, summed). The windows are named by their length (`five_hour`,
  `seven_day`) so Claude's and Codex's agree.
- **CodexBar's source is left to CodexBar.** The collector passes `--provider` only. The default source can read browser
  cookies or start an agent CLI, which could not be tried in this step; the revalidation log records it as `unknown` with
  the command to run by hand.

## Consequences

- `/now` stays `fylgja.now/1`: `sessions[].tokens`, `cost_usd`, `context_pct` and `usage` were already in the models.
- `SessionActivity` gains three optional fields and `UsagePayload` two, so the collector API grows compatibly.
- `[collectors.project]` gains the `usage_*`, `cost_*` and `codexbar_*` settings; a daemon that has no CodexBar shows
  `usage` as `unavailable` with `codexbar_missing` until the setting is turned off.
- The token numbers will look high next to what a transcript really spent, until the deduplicated field exists.
- The live coverage (`tokens` for every live Claude session, `usage` filled or absent with its reason) and the
  CodexBar default source are checked by hand.
