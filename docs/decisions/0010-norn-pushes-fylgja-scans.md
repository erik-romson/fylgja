# 0010: norn pushes; Fylgja scans as the fallback

- Status: accepted
- Amendment: A10
- Amends: PLAN.md §4.6
- Answers: Q6
- Relates to: A21
- Date: 2026-10-05

## Context

R3. The push gives exact liveness, the live stage, and norn's waiting-for-input signal for "waiting on me".

## Decision

**norn pushes; Fylgja scans as the fallback** (Q6). A norn-side plugin (WP3.3, in the norn repo) uses the hook envelope format to write into `hook-inbox/norn/`. It writes run lifecycle events, stage changes, `WaitingInput`, agent session ids, and a heartbeat with pid every 30 s. A run with a heartbeat no older than 90 s and a live pid is `running`. The scan of configured roots and known project cwds (depth-limited) for `*.history` and `*.runs/` stays for backfill and for runs started without the plugin. For those runs, liveness falls back to matching process argv and cwd against `run.json`, and an unfinished run with no match is `stale`. `sdk-py` Claude sessions whose ids appear in a run are nested under that run instead of being listed as independent sessions.

## Consequences

- WP3.3 adds a norn-side plugin (in the norn repo) that writes envelopes into `hook-inbox/norn/`, including a 30 s heartbeat.
- Liveness is exact for runs with a heartbeat no older than 90 s and a live pid.
- The scan of configured roots remains for backfill and for runs without the plugin, with argv and cwd matching and a `stale` state.
- `sdk-py` Claude sessions that appear in a run are nested under that run.
