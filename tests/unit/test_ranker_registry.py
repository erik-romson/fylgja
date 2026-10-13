"""The ranker registry: metadata discovery, the enable rule, settings and reported failures.

Entry points are injected, so every rule is checked without installing anything. The real
``fylgja-fake`` distribution is checked at the end and in ``test_registry_real_dist.py``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from fylgja import paths
from fylgja.collectors.registry import EntryPointLike
from fylgja.config import Config, parse_config
from fylgja.rankers.base import (
    FLOOR_NAME,
    NoSettings,
    Part,
    Ranked,
    RankerPlugin,
    RankInput,
    default_workspace_order,
)
from fylgja.rankers.frecency import FrecencySettings
from fylgja.rankers.registry import (
    MAX_ERROR,
    Origin,
    RegisteredRanker,
    discover_rankers,
    load_rankers,
)
from fylgja.testing.rankers import sample_inputs

BUILT_IN = "fylgja"
EXTERNAL = "vendor-rankers"


@dataclass(frozen=True)
class Dist:
    name: str
    version: str = "1.0.0"


@dataclass
class EntryPoint:
    """An entry point whose ``load`` is counted; ``target`` is returned, or raised if an
    exception."""

    name: str
    dist: Dist | None
    target: object
    value: str = ""
    loads: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.value = self.value or f"synthetic.{self.name}:plugin"

    def load(self) -> object:
        self.loads.append(self.name)
        if isinstance(self.target, BaseException):
            raise self.target
        return self.target


def _rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return [Ranked(item.key, 0.0, (Part("synthetic", 0.0),)) for item in data.items]


class WeightSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    weight: float = Field(default=1.0, gt=0)


def _plugin(name: str, **options: Any) -> RankerPlugin:
    return RankerPlugin(name=name, description=f"Synthetic {name}", rank=_rank, **options)


def _ep(name: str, target: object = None, *, builtin: bool = False) -> EntryPoint:
    dist = Dist(BUILT_IN if builtin else EXTERNAL)
    return EntryPoint(name, dist, _plugin(name) if target is None else target)


def _source(*eps: EntryPoint) -> Callable[[], list[EntryPointLike]]:
    return lambda: list(eps)


def _config(rankers: Mapping[str, Mapping[str, Any]] | None = None) -> Config:
    return parse_config({"rankers": dict(rankers or {})}, source="test-config.toml")


def _by_name(
    *eps: EntryPoint, rankers: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, RegisteredRanker]:
    return {r.name: r for r in load_rankers(_config(rankers), _source(*eps))}


class Bomb(Exception):
    """Raised by an entry point that must never be loaded."""


# --------------------------------------------------------------------------------------------
# The enable rule
# --------------------------------------------------------------------------------------------


def test_a_builtin_loads_by_default_with_its_default_settings() -> None:
    ep = _ep("alpha", builtin=True)
    registered = _by_name(ep)["alpha"]
    assert registered.origin is Origin.BUILT_IN
    assert (registered.enabled, registered.loaded, registered.error) == (True, True, None)
    assert registered.distribution == BUILT_IN
    assert registered.version == "1.0.0"
    assert isinstance(registered.settings, NoSettings)
    assert ep.loads == ["alpha"]


def test_a_builtin_with_enabled_false_is_not_loaded_and_is_reported_disabled() -> None:
    ep = _ep("alpha", Bomb("a disabled built-in was imported"), builtin=True)
    registered = _by_name(ep, rankers={"alpha": {"enabled": False}})["alpha"]
    assert registered.origin is Origin.BUILT_IN
    assert not registered.enabled
    assert not registered.loaded
    assert (registered.plugin, registered.settings, registered.error) == (None, None, None)
    assert ep.loads == []


def test_an_external_is_not_imported_unless_enabled() -> None:
    bomb = _ep("vendor_bomb", Bomb("a disabled external was imported"))
    for rankers in ({}, {"vendor_bomb": {"enabled": False}}, {"vendor_bomb": {"weight": 2}}):
        registered = _by_name(bomb, rankers=rankers)["vendor_bomb"]
        assert registered.origin is Origin.EXTERNAL
        assert not registered.enabled
        assert not registered.loaded
        assert registered.error is None
    assert bomb.loads == []


def test_an_enabled_external_loads_with_its_validated_settings() -> None:
    ep = _ep("vendor", _plugin("vendor", config_model=WeightSettings))
    registered = _by_name(ep, rankers={"vendor": {"enabled": True, "weight": 2.5}})["vendor"]
    assert registered.origin is Origin.EXTERNAL
    assert registered.loaded
    assert registered.settings == WeightSettings(weight=2.5)
    assert registered.distribution == EXTERNAL


def test_the_settings_never_see_the_enabled_key() -> None:
    ep = _ep("alpha", _plugin("alpha", config_model=WeightSettings), builtin=True)
    registered = _by_name(ep, rankers={"alpha": {"enabled": True}})["alpha"]
    assert registered.settings == WeightSettings()


# --------------------------------------------------------------------------------------------
# Duplicates
# --------------------------------------------------------------------------------------------


def test_a_duplicate_external_loses_to_a_builtin_and_is_never_imported() -> None:
    builtin = _ep("alpha", builtin=True)
    external = _ep("alpha", Bomb("the losing duplicate was imported"))
    registry = load_rankers(_config({"alpha": {"enabled": True}}), _source(builtin, external))
    winners = [r for r in registry if r.distribution == BUILT_IN]
    losers = [r for r in registry if r.distribution == EXTERNAL]
    assert [r.loaded for r in winners] == [True]
    assert len(losers) == 1
    assert not losers[0].enabled
    assert not losers[0].loaded
    assert losers[0].error is not None
    assert losers[0].error.startswith("duplicate: ")
    assert external.loads == []


def test_two_externals_with_one_name_are_both_rejected() -> None:
    first = EntryPoint("vendor", Dist("vendor-a"), Bomb("first"))
    second = EntryPoint("vendor", Dist("vendor-b"), Bomb("second"))
    registry = load_rankers(_config({"vendor": {"enabled": True}}), _source(first, second))
    assert [(r.distribution, r.loaded) for r in registry] == [
        ("vendor-a", False),
        ("vendor-b", False),
    ]
    assert all(r.error and r.error.startswith("duplicate: ") for r in registry)
    assert first.loads == second.loads == []


def test_the_same_entry_point_seen_twice_counts_once() -> None:
    ep = _ep("alpha", builtin=True)
    registry = load_rankers(_config(), _source(ep, ep))
    assert [(r.name, r.loaded) for r in registry] == [("alpha", True)]


# --------------------------------------------------------------------------------------------
# Reported failures
# --------------------------------------------------------------------------------------------


def _error(registered: RegisteredRanker) -> str:
    assert not registered.loaded
    assert registered.plugin is None
    assert registered.settings is None
    assert registered.error is not None
    return registered.error


def test_invalid_settings_give_an_error_and_the_others_still_load() -> None:
    bad = _ep("alpha", _plugin("alpha", config_model=WeightSettings), builtin=True)
    good = _ep("beta", builtin=True)
    for table in ({"weight": 0}, {"weight": "heavy"}, {"unknown": 1}):
        registry = _by_name(bad, good, rankers={"alpha": table})
        error = _error(registry["alpha"])
        assert error.startswith("config: [rankers.alpha].")
        assert registry["alpha"].enabled
        assert registry["beta"].loaded


def test_a_wrong_api_major_gives_an_error() -> None:
    ep = _ep("alpha", _plugin("alpha", api_major=2), builtin=True)
    assert _error(_by_name(ep)["alpha"]) == "api_major: plugin API major 2, core supports 1"


def test_the_name_natural_gives_an_error_and_is_never_imported() -> None:
    builtin = _ep(FLOOR_NAME, Bomb("the reserved name was imported"), builtin=True)
    external = _ep(FLOOR_NAME, Bomb("the reserved name was imported"))
    for ep in (builtin, external):
        registered = _by_name(ep, rankers={FLOOR_NAME: {"enabled": True}})[FLOOR_NAME]
        assert _error(registered).startswith("reserved: ")
    assert builtin.loads == external.loads == []


def test_a_plugin_named_natural_cannot_be_built() -> None:
    with pytest.raises(ValueError, match="reserved"):
        _plugin(FLOOR_NAME)


def test_an_import_that_fails_gives_an_error() -> None:
    ep = _ep("alpha", RuntimeError("boom"), builtin=True)
    assert _error(_by_name(ep)["alpha"]) == "import: RuntimeError: boom"


def test_a_long_error_is_cut() -> None:
    ep = _ep("alpha", RuntimeError("x" * 1000), builtin=True)
    assert len(_error(_by_name(ep)["alpha"])) == MAX_ERROR


def test_an_object_that_is_not_a_ranker_plugin_gives_an_error() -> None:
    ep = _ep("alpha", object(), builtin=True)
    assert _error(_by_name(ep)["alpha"]) == "entry_point: object is not a RankerPlugin"


def test_a_name_that_does_not_match_the_entry_point_gives_an_error() -> None:
    ep = _ep("alpha", _plugin("beta"), builtin=True)
    assert _error(_by_name(ep)["alpha"]).startswith("name: plugin name 'beta'")


def test_the_registry_is_sorted_by_name_then_distribution() -> None:
    registry = load_rankers(
        _config(), _source(_ep("zeta", builtin=True), _ep("alpha"), _ep("mid", builtin=True))
    )
    assert [r.name for r in registry] == ["alpha", "mid", "zeta"]


def test_discovery_reads_metadata_only() -> None:
    ep = _ep("alpha", Bomb("discovery imported a ranker"), builtin=True)
    discovery = discover_rankers(_source(ep))
    assert [(e.name, e.origin) for e in discovery.entries] == [("alpha", Origin.BUILT_IN)]
    assert ep.loads == []


# --------------------------------------------------------------------------------------------
# Running a registered ranker
# --------------------------------------------------------------------------------------------


def test_a_registered_ranker_ranks_with_its_settings() -> None:
    seen: list[BaseModel] = []

    def rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
        seen.append(settings)
        return _rank(data, settings)

    plugin = RankerPlugin("alpha", "Synthetic", rank, config_model=WeightSettings)
    ep = _ep("alpha", plugin, builtin=True)
    registered = _by_name(ep, rankers={"alpha": {"weight": 3}})["alpha"]
    data = sample_inputs()["small"]
    ranked = registered.rank(data)
    assert [r.key for r in ranked] == [item.key for item in data.items]
    assert seen == [WeightSettings(weight=3)]


def test_without_its_own_workspace_order_the_default_is_used() -> None:
    registered = _by_name(_ep("alpha", builtin=True))["alpha"]
    data = sample_inputs()["small"]
    ranked = registered.rank(data)
    assert registered.order_workspaces(data, ranked) == default_workspace_order(data, ranked)


def test_a_ranker_with_its_own_workspace_order_uses_it() -> None:
    def backwards(data: RankInput, ranked: object) -> list[str]:
        return list(reversed(data.workspaces))

    ep = _ep("alpha", _plugin("alpha", order_workspaces=backwards), builtin=True)
    registered = _by_name(ep)["alpha"]
    data = sample_inputs()["small"]
    assert registered.order_workspaces(data, []) == ["10", "2", "1"]


def test_a_ranker_that_is_not_loaded_cannot_run() -> None:
    registered = _by_name(_ep("alpha"))["alpha"]
    data = sample_inputs()["empty"]
    with pytest.raises(LookupError, match="not loaded"):
        registered.rank(data)
    with pytest.raises(LookupError, match="not loaded"):
        registered.order_workspaces(data, [])


# --------------------------------------------------------------------------------------------
# The real distributions
# --------------------------------------------------------------------------------------------


def test_the_builtins_and_the_example_are_discovered_from_metadata() -> None:
    entries = {e.name: e for e in discover_rankers().entries}
    assert entries["workspace"].origin is Origin.BUILT_IN
    assert entries["recent"].origin is Origin.BUILT_IN
    assert entries["frecency"].origin is Origin.BUILT_IN
    assert entries["fake_ranker"].origin is Origin.EXTERNAL
    assert entries["fake_ranker_bomb"].value == "fylgja_fake.ranker_bomb:plugin"


def test_by_default_the_builtins_load_and_the_externals_are_never_imported() -> None:
    registry = {r.name: r for r in load_rankers(_config())}
    assert registry["workspace"].loaded
    assert registry["recent"].loaded
    assert registry["frecency"].loaded
    for name in ("fake_ranker", "fake_ranker_bomb"):
        assert not registry[name].enabled
        assert not registry[name].loaded
        assert registry[name].error is None
    assert "fylgja_fake.ranker_bomb" not in sys.modules
    assert not (paths.state_dir() / "fake_ranker_bomb.sentinel").exists()


def test_every_builtin_can_be_disabled() -> None:
    names = ("workspace", "recent", "frecency")
    registry = {r.name: r for r in load_rankers(_config({n: {"enabled": False} for n in names}))}
    for name in names:
        assert not registry[name].enabled
        assert not registry[name].loaded


# --------------------------------------------------------------------------------------------
# frecency
# --------------------------------------------------------------------------------------------


def test_the_packaged_defaults_validate_against_the_frecency_settings() -> None:
    registry = {r.name: r for r in load_rankers(_config())}
    frecency = registry["frecency"]
    assert frecency.loaded
    assert frecency.settings == FrecencySettings(
        half_life_active_minutes=120, last_session_weight=1
    )


@pytest.mark.parametrize(
    "table",
    [
        {"half_life_active_minutes": 0},
        {"half_life_active_minutes": 10081},
        {"last_session_weight": -0.5},
        {"unknown_key": 1},
    ],
)
def test_a_bad_frecency_setting_gives_only_frecency_an_error(table: dict[str, Any]) -> None:
    registry = {r.name: r for r in load_rankers(_config({"frecency": table}))}
    assert _error(registry["frecency"]).startswith("config: [rankers.frecency].")
    assert registry["workspace"].loaded
    assert registry["recent"].loaded


def test_frecency_can_be_disabled_and_the_others_still_load() -> None:
    registry = {r.name: r for r in load_rankers(_config({"frecency": {"enabled": False}}))}
    assert not registry["frecency"].enabled
    assert not registry["frecency"].loaded
    assert registry["workspace"].loaded
