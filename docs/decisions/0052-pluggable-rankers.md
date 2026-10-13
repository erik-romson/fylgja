# 0052: Pluggable rankers

- Status: accepted
- Amendment: none
- Amends: —
- Answers: —
- Relates to: A6, A7
- Date: 2026-10-13

## Context

The recall view and the workspace cards need an order for the navigation items (decision 0051), and there is more than
one good order: workspace number, most recent, frecency on working time, and orders nobody has thought of yet. If the
daemon chose between them by name, every new order would be a change to Fylgja, and disabling the one the daemon
relied on would break it. Collectors already solved the same problem: an entry-point group, metadata-first discovery,
built-ins on unless disabled, externals opt-in, settings validated by the plugin and a public contract (`PLAN.md` §4.1,
§4.9). A ranker runs inside the daemon on every answer that is read without a secret (A6), and every client, the CLI
included (A7), must get the same order for the same question, so a ranker must be pure and deterministic.

## Decision

- **Entry-point group** `fylgja.rankers`. A package exports a `RankerPlugin` (`fylgja.rankers.base`) as `plugin`:
  `name` (the plugin-name pattern of collectors, equal to the entry-point name), `description` (one line, at most 120
  characters), `rank(data, settings)`, `config_model` (default `NoSettings`), an optional `order_workspaces(data,
  ranked)` and `api_major` (`RANKER_API_MAJOR` = 1).
- **Types.** `RankInput` holds `now` (floored to 5 s by the daemon), the `items`, the `activity` (working time,
  decision 0050) and the `workspaces` in natural order; nothing ranker-specific. `rank` returns a `Ranked` per item:
  `key`, `score` and `parts` (`Part(name, value)`). `Ranked.explanation` joins the parts: the first value as is, the
  others signed, whole numbers from 10 up, and a part with value 0 as a bare label. `natural_key`, the sort key of
  workspace names, lives in `fylgja.rankers.base`; `api/now.py` imports it.
- **Purity.** A ranker reads no clock, does no I/O and logs nothing. Ages are active working time
  (`activity.active_since`), so nothing changes while the person is away. The output is independent of the input order.
- **The enable rule is the collectors' rule.** A built-in (distribution `fylgja`) loads unless `[rankers.<name>]
  enabled = false`; an external loads only with `enabled = true`. Discovery reads metadata only and reuses the
  collector registry's discovery; a disabled ranker, built-in or external, is never imported, and a built-in wins a
  duplicate name. `Config.rankers` keeps the tables raw, with the same boolean `enabled` check as `collectors`
  (`ranker_enabled`, `ranker_settings`).
- **Per-ranker settings.** `[rankers.<name>]` minus `enabled` is validated against the ranker's `config_model` at load
  and passed to every `rank` call. Knobs never go into `RankInput` or `[items]`.
- **Failure is reported.** A failed import, an object that is not a `RankerPlugin`, a name that does not match its
  entry point, the name `natural`, another API major or invalid settings give a `RegisteredRanker` with an `error`
  (`<kind>: <detail>`, at most 300 characters) and no plugin. The daemon keeps running.
- **The `natural` floor** (`natural_order`) orders items by the natural order of their newest open place's workspace,
  then by key. It is not a plugin, cannot be disabled, and its name is reserved. The daemon falls back to it when
  `[items] default_ranker` is not loaded or a ranker raises, so nothing in the daemon depends on a named ranker.
- **The contract**, `fylgja.testing.rankers.RankerContract`, checks discovery, name, API and description; settings that
  build from defaults; every key exactly once; determinism under a `random.Random(0)` shuffle; at least one part,
  finite numbers and a private explanation of at most 200 characters; purity with the clocks, I/O, subprocesses and
  logging patched to raise; empty in, empty out; and a workspace order that is a permutation. `sample_inputs()` gives
  three fixed worlds (empty; 6 items over 3 workspaces with a waiting session; 40 items with 2 000 spans over 3 work
  sessions).
- **Built-ins.** `workspace` is the floor as a selectable ranker, with workspaces in natural order (today's cards).
  `recent` orders by active minutes since the last focus, never-focused items next (open ones first), then by key;
  its workspace order follows the first open item of each workspace.
- **An example external ranker**, `fake_ranker` ("most places first", one setting), and `fake_ranker_bomb`, which
  raises on import, ship in the test distribution `fylgja-fake`.
- **`frecency`** (the default ranker) scores focus in active working time. For an item's spans, `focus` is the sum of
  `span minutes × 0.5 ^ (active_since(span end) / (half_life_active_minutes × 60))`; a span under 120 s that ended under
  120 active s ago (a glance) is left out. `last session` is `(share of the previous work session's active time in the
  item + 0.25 if it held that session's last span) × 60 × last_session_weight × 0.5 ^ (active seconds of the current
  work session / 3600)`. State boosts reuse the Next-strip scores (`core/ranking.SCORES`): `waiting` 100 (permission or
  input), `turn done` 90, `build` 70 (build failed or review requested), `todo` 80 (`next` todo), and `unfinished` +10
  (dirty or open pull request). `forgotten` +40: at least 30 active minutes in the last 5 work sessions, none in the
  current or previous one, and unfinished. `while away` +1000: a waiting item not focused since the latest absence began,
  until 300 active seconds have passed since the return (an item carries no time for a waiting entry, so this is the
  closest reading of "changed while away"). `here now` is −0.5 × focus for the item holding the open span; `stale` is
  −0.75 × focus when no span is open and the last focus is over 16 active hours ago. The order is the score descending,
  then active time since the last focus ascending (never focused last), then the key. A span belongs to the item that
  holds its window as a place, else to the folder or app it was recorded in. The settings are
  `[rankers.frecency] half_life_active_minutes` (120) and `last_session_weight` (1.0). The constants sit at the top of
  `fylgja/rankers/frecency.py`.
- The Next strip (`core/ranking.py`) is unchanged.

## Consequences

- Adding, removing or disabling a ranker, built-ins included, needs no change to Fylgja's code: install and enable,
  uninstall, or `enabled = false`. `docs/rankers.md` explains how to write one.
- An installed external ranker runs no code until it is enabled.
- Every built-in has `tests/contract/test_<name>_ranker_contract.py`; a coverage test enforces it.
- Explanations hold reasons, never names, paths or titles, so they can be shown and logged.
- The routes (`GET /items`, `GET /rankers`), the memo and the fallback wiring come with the items routes; this record
  sets the plugin API they build on.
