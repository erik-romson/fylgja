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
- The Next strip (`core/ranking.py`) is unchanged.

## Consequences

- Adding, removing or disabling a ranker, built-ins included, needs no change to Fylgja's code: install and enable,
  uninstall, or `enabled = false`. `docs/rankers.md` explains how to write one.
- An installed external ranker runs no code until it is enabled.
- Every built-in has `tests/contract/test_<name>_ranker_contract.py`; a coverage test enforces it.
- Explanations hold reasons, never names, paths or titles, so they can be shown and logged.
- The routes (`GET /items`, `GET /rankers`), the memo and the fallback wiring come with the items routes; this record
  sets the plugin API they build on.
