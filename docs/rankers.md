# Writing a ranker

A **ranker** orders the navigation items: one item per git-root folder and one per app (decision 0051). Rankers are
plugins, exactly like collectors (decision 0052): adding, removing or disabling one needs no change to Fylgja's code.

| To | Do |
|---|---|
| add one | install a package with a `fylgja.rankers` entry point, then set `[rankers.<name>] enabled = true` |
| disable one, built-ins included | `[rankers.<name>] enabled = false` |
| remove one | uninstall its package (a built-in: disable it) |
| configure one | its own `[rankers.<name>]` table, validated by the ranker's `config_model` |
| check the result | `fylgja rankers` or `GET /rankers`: enabled, loaded, load error, default in effect |

The built-in rankers:

| Name | Order | Parts | Settings |
|---|---|---|---|
| `workspace` | the `natural` floor: items by the natural order (`2` before `10`) of their newest open place's workspace, then key; workspace cards in natural order | `workspace <n>`, `no workspace` | none |
| `recent` | active minutes since the last focus, ascending; never-focused items next (open ones first); then key. Workspace cards follow their first open item | `active min since focus <m>`, `latest focus`, `never focused` | none |
| `frecency` | score descending: decayed focus on working time, the last work session, waiting and unfinished work; then active time since the last focus, then key. The default (`[items] default_ranker`) | `focus`, `last session`, `waiting`, `turn done`, `build`, `todo`, `unfinished`, `forgotten`, `while away`, `here now`, `stale` | `half_life_active_minutes` (120, above 0, at most 10080), `last_session_weight` (1.0, 0 to 10) |

### The `frecency` settings

```toml
[rankers.frecency]
half_life_active_minutes = 120  # focus loses half its weight after this many active minutes
last_session_weight = 1.0       # 0 turns the last-session part off
```

A bad value leaves only `frecency` unloaded, with a `config:` error; `[items] default_ranker` then falls back. The work
session break is `[items] work_session_break_minutes`.

## The entry point and the plugin

A ranker package names a module-level `RankerPlugin` in the `fylgja.rankers` entry-point group:

```toml
[project.entry-points."fylgja.rankers"]
mine = "my_rankers:plugin"
```

`RankerPlugin` (`fylgja.rankers.base`) has these fields:

| Field | Meaning |
|---|---|
| `name` | `[a-z0-9_-]+`, equal to the entry-point name. `natural` is reserved for the floor and refused |
| `description` | one non-empty line of at most 120 characters |
| `rank` | `rank(data: RankInput, settings) -> Sequence[Ranked]`: every input item exactly once, in rank order |
| `config_model` | a pydantic model for `[rankers.<name>]` minus `enabled`; every field needs a default. Default `NoSettings` |
| `order_workspaces` | optional `order_workspaces(data, ranked) -> Sequence[str]`, a permutation of `data.workspaces`. Without it, workspaces are ordered by the summed score of the items with an open place in them, ties in natural order |
| `api_major` | `RANKER_API_MAJOR` (1); any other value is refused at load |

## What a ranker gets

`RankInput` holds only what every ranker gets. A ranker's own knobs come from its settings, so a new ranker never
changes `RankInput` or `[items]`.

| Field | Meaning |
|---|---|
| `now` | the time of the answer, floored to 5 s by the daemon. Read this, never a clock |
| `items` | the `Item`s to order, each with a unique `key`; their order means nothing |
| `activity` | `Activity`: the focus spans, work sessions, `current`, `previous`, `away` and `working` |
| `workspaces` | the workspace names in natural order |

An `Item` carries `key` (`folder:/work/alpha/main` or `app:com.example.app`), `kind`, `name`, `path`, `tickets`,
`places` (each `Place` has `kind`, `ref`, `role`, `open`, `waiting`, `workspace` and `last_focus_at`), `state`
(waiting kinds, `todo_next`, `dirty`, `pr_open`), `last_focus_at`, `seconds_today`, `summary` and the Go targets.

**Ages are active working time.** Use `data.activity.active_since(moment)`, the seconds of focus after `moment`, not
`data.now - moment`. Time away from the keyboard (idle, locked, asleep) is never in a focus span, so nothing a ranker
computes changes while the person is away. Work sessions split at a not-working gap of `[items]
work_session_break_minutes`.

## What a ranker returns

A `Ranked` per item: `key`, `score` and `parts`, a tuple of `Part(name, value)`. The parts explain the score;
`Ranked.explanation` joins them into one line, `focus 300 · last session +80 · here now −68`. The first value is shown
as is and the others with a sign; values from 10 up are whole numbers, smaller ones keep one decimal. A part with
value 0 is a label and shows its name alone (`latest focus`). Higher scores go first in the default workspace order.

## Settings

`[rankers.<name>]` minus `enabled` is validated against `config_model` when the ranker loads, and the validated
instance is passed to every `rank` call. A table that fails validation leaves the ranker unloaded with an error such as
`config: [rankers.mine].place_weight: Input should be greater than 0`; the daemon keeps running and the other rankers
still load.

```toml
[rankers.mine]
enabled = true
place_weight = 2.0
```

## Enabling, disabling and removing

The enable rule is the collectors' rule:

| Origin | Loads when | Disabled by |
|---|---|---|
| built-in (distribution `fylgja`) | always, unless disabled | `[rankers.<name>] enabled = false` |
| external | only with `[rankers.<name>] enabled = true` | leaving it out, `enabled = false`, or uninstalling |

- Discovery reads entry-point metadata only. A disabled ranker, built-in or external, is never imported.
- A name claimed by two distributions is a duplicate: a built-in keeps it, external claimants are rejected.
- A ranker that fails to import, is not a `RankerPlugin`, names itself differently from its entry point, takes the name
  `natural`, has another API major or has invalid settings is reported with an error and not loaded.
- When `[items] default_ranker` is not loaded (disabled, uninstalled or broken), the daemon uses the `natural` floor:
  the `workspace` order, which is not a plugin and cannot be disabled. A ranker that raises at run time, or returns
  another key set, a number that is not finite or a workspace order that is not a permutation, is replaced by the floor
  for that answer (`ranker_fallback: "natural"`), and the log names it.

`fylgja rankers` and `GET /rankers` list every discovered ranker with its origin, distribution, version, `enabled`,
`loaded`, its error and the default in effect; `GET /rankers` also gives the loaded ones as `choices`.
`GET /items?rank=<name>` ranks with one of them. Both routes are described in `docs/api/items.md`.

## Purity and privacy

A ranker runs inside the daemon on every answer, so it must be:

- **pure**: no clock (use `data.now` and `data.activity`), no file, socket or subprocess, no logging;
- **deterministic**: the same input gives the same output, and the order of `data.items` does not matter. Break every
  tie, for example by `key`;
- **private**: part names describe reasons (`focus`, `waiting`). Never put an item's name, path or a window title in
  one: the explanation is shown and may be logged.

## The contract

Every ranker passes `fylgja.testing.rankers.RankerContract`. Subclass it in your package's tests:

```python
import pytest

from fylgja.testing.rankers import RankerContract


class TestMineRankerContract(RankerContract):
    @pytest.fixture
    def ranker_name(self) -> str:
        return "mine"

    @pytest.fixture
    def ranker_settings(self) -> dict[str, float]:
        return {"place_weight": 2.0}
```

The contract enables the ranker, loads it through the registry from the installed entry points and runs it on three
fixed worlds (`sample_inputs()`: empty; six items over three workspaces with a waiting session; forty items with 2 000
spans over three work sessions). It checks that the ranker:

1. is discoverable, its name matches the entry point and is not `natural`, its API major matches, and its description
   is one line of at most 120 characters;
2. has a `config_model` that builds from defaults only;
3. keeps every input key exactly once;
4. is deterministic, also with the items shuffled;
5. gives every item at least one part, finite numbers, and an explanation of at most 200 characters without an item
   name, path or title;
6. runs with the clocks, file and socket I/O, subprocesses and logging patched to raise (`datetime.now` cannot be
   patched; read `data.now`);
7. gives empty output for empty input;
8. orders the workspaces as a permutation of `data.workspaces`.

Fylgja's own built-ins each have `tests/contract/test_<name>_ranker_contract.py`.

## Worked example

The test distribution `tests/fake_plugins/fylgja_fake` ships an external ranker, `fake_ranker`: the items with the
most places first. Its entry point:

```toml
[project.entry-points."fylgja.rankers"]
fake_ranker = "fylgja_fake.ranker:plugin"
```

Its module, `fylgja_fake/ranker.py`:

```python
from pydantic import BaseModel, ConfigDict, Field

from fylgja.rankers.base import Part, Ranked, RankerPlugin, RankInput


class FakeRankerSettings(BaseModel):
    """``[rankers.fake_ranker]`` minus ``enabled``. Every field has a default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    place_weight: float = Field(default=1.0, gt=0, le=100)
    """Points per place."""


def rank(data: RankInput, settings: FakeRankerSettings) -> list[Ranked]:
    """Most places first, then by key: a total order, whatever the input order."""
    scored = []
    for item in data.items:
        value = len(item.places) * settings.place_weight
        scored.append(Ranked(item.key, value, (Part("places", value),)))
    return sorted(scored, key=lambda r: (-r.score, r.key))


plugin = RankerPlugin(
    name="fake_ranker",
    description="Most places first",
    rank=rank,
    config_model=FakeRankerSettings,
)
```

It is off until enabled:

```toml
[rankers.fake_ranker]
enabled = true
place_weight = 2.0
```

Its contract run is `tests/contract/test_fake_ranker_contract.py`. The same distribution also has `fake_ranker_bomb`,
a module that writes a sentinel and raises when imported; the registry tests prove it is never imported unless
enabled.
