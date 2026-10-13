"""The ranker contract catches what it promises to catch.

Each broken ranker below breaks one rule. It is installed through a patched entry-point source
(an external, which the contract enables), and the matching check of :class:`RankerContract` must
fail on it, while a well-behaved ranker passes every check.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from fylgja.collectors.registry import EntryPointLike
from fylgja.rankers import registry
from fylgja.rankers.base import Part, Ranked, RankerPlugin, RankInput, natural_order
from fylgja.testing.rankers import (
    ImpureRankerError,
    RankerContract,
    forbid_side_effects,
    sample_inputs,
)

NAME = "probe"


@dataclass(frozen=True)
class Dist:
    name: str = "probe-rankers"
    version: str = "0.0.1"


@dataclass(frozen=True)
class EntryPoint:
    target: object
    name: str = NAME
    value: str = "probe:plugin"
    dist: Dist = Dist()

    def load(self) -> object:
        return self.target


RankFn = Callable[[RankInput, BaseModel], list[Ranked]]


def _install(monkeypatch: pytest.MonkeyPatch, plugin: RankerPlugin) -> None:
    def source() -> Iterable[EntryPointLike]:
        return [EntryPoint(plugin)]

    monkeypatch.setattr(registry, "_installed_entry_points", source)


def _plugin(rank: RankFn, **options: Any) -> RankerPlugin:
    return RankerPlugin(name=NAME, description="Probe", rank=rank, **options)


def _good(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return natural_order(data)


CHECKS: dict[str, Callable[[RankerContract], Callable[[str, dict[str, Any]], None]]] = {
    "1": lambda c: c.test_1_discoverable_with_a_valid_name_api_and_description,
    "2": lambda c: c.test_2_settings_model_builds_from_defaults_only,
    "3": lambda c: c.test_3_keeps_every_input_key_exactly_once,
    "4": lambda c: c.test_4_is_deterministic_and_independent_of_input_order,
    "5": lambda c: c.test_5_parts_numbers_and_explanation_are_valid_and_private,
    "6": lambda c: c.test_6_is_pure_no_clock_no_io_no_logging,
    "7": lambda c: c.test_7_empty_input_gives_empty_output,
    "8": lambda c: c.test_8_workspace_order_is_a_permutation,
}


def _run(check: str, settings: dict[str, Any] | None = None) -> None:
    CHECKS[check](RankerContract())(NAME, settings or {})


def test_a_well_behaved_ranker_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _plugin(_good))
    for check in CHECKS:
        _run(check)


# -- broken rankers ------------------------------------------------------------------------------


def _drops_a_key(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return natural_order(data)[1:]


def _repeats_a_key(data: RankInput, settings: BaseModel) -> list[Ranked]:
    ranked = natural_order(data)
    return ranked + ranked[:1]


def _input_order(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return [Ranked(item.key, 0.0, (Part("as given", 0.0),)) for item in data.items]


def _counter() -> RankFn:
    calls: list[int] = []

    def rank(data: RankInput, settings: BaseModel) -> list[Ranked]:
        calls.append(1)
        return [Ranked(r.key, float(len(calls)), r.parts) for r in natural_order(data)]

    return rank


def _leaks_names(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return [Ranked(r.key, 1.0, (Part(f"{r.key}", 1.0),)) for r in natural_order(data)]


def _leaks_titles(data: RankInput, settings: BaseModel) -> list[Ranked]:
    out = []
    for item in sorted(data.items, key=lambda i: i.key):
        title = next((p.title for p in item.places if p.title), "none")
        out.append(Ranked(item.key, 1.0, (Part(f"seen in {title}", 1.0),)))
    return out


def _infinite(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return [Ranked(r.key, math.inf, r.parts) for r in natural_order(data)]


def _nan_part(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return [Ranked(r.key, r.score, (Part("odd", math.nan),)) for r in natural_order(data)]


def _no_parts(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return [Ranked(r.key, r.score, ()) for r in natural_order(data)]


def _long_explanation(data: RankInput, settings: BaseModel) -> list[Ranked]:
    parts = tuple(Part(f"reason {n}", 1.0) for n in range(40))
    return [Ranked(r.key, r.score, parts) for r in natural_order(data)]


def _reads_the_clock(data: RankInput, settings: BaseModel) -> list[Ranked]:
    time.time()
    return natural_order(data)


def _reads_monotonic(data: RankInput, settings: BaseModel) -> list[Ranked]:
    time.monotonic()
    return natural_order(data)


def _opens_a_file(data: RankInput, settings: BaseModel) -> list[Ranked]:
    with open(Path(__file__)) as handle:
        handle.read(1)
    return natural_order(data)


def _logs(data: RankInput, settings: BaseModel) -> list[Ranked]:
    logging.getLogger("probe").debug("ranking %d items", len(data.items))
    return natural_order(data)


def _invents_items(data: RankInput, settings: BaseModel) -> list[Ranked]:
    return [*natural_order(data), Ranked("folder:/work/extra/main", 0.0, (Part("extra", 1.0),))]


@pytest.mark.parametrize(
    ("check", "rank"),
    [
        ("3", _drops_a_key),
        ("3", _repeats_a_key),
        ("3", _invents_items),
        ("4", _input_order),
        ("4", _counter()),
        ("5", _leaks_names),
        ("5", _leaks_titles),
        ("5", _infinite),
        ("5", _nan_part),
        ("5", _no_parts),
        ("5", _long_explanation),
        ("6", _reads_the_clock),
        ("6", _reads_monotonic),
        ("6", _opens_a_file),
        ("6", _logs),
        ("7", _invents_items),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_a_broken_ranker_fails_its_check(
    monkeypatch: pytest.MonkeyPatch, check: str, rank: RankFn
) -> None:
    _install(monkeypatch, _plugin(rank))
    with pytest.raises(AssertionError):
        _run(check)


def test_a_workspace_order_that_is_not_a_permutation_fails_check_8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def first_only(data: RankInput, ranked: object) -> list[str]:
        return list(data.workspaces[:1])

    _install(monkeypatch, _plugin(_good, order_workspaces=first_only))
    with pytest.raises(AssertionError):
        _run("8")


def test_a_workspace_order_that_depends_on_input_order_fails_check_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def by_first_item(data: RankInput, ranked: object) -> list[str]:
        first = next((p.workspace for i in data.items for p in i.places if p.workspace), None)
        rest = [w for w in data.workspaces if w != first]
        return ([first] if first in data.workspaces else []) + rest

    _install(monkeypatch, _plugin(_good, order_workspaces=by_first_item))
    with pytest.raises(AssertionError):
        _run("4")


class Required(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    weight: float


def test_settings_without_defaults_fail_check_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _plugin(_good, config_model=Required))
    with pytest.raises(pytest.fail.Exception):
        _run("2", {"weight": 1.0})


def test_a_ranker_that_does_not_load_fails_check_1(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _plugin(_good, api_major=99))
    with pytest.raises(AssertionError, match="failed to load"):
        _run("1")


def test_a_missing_entry_point_fails_check_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_installed_entry_points", list)
    with pytest.raises(AssertionError, match="no fylgja.rankers entry point"):
        _run("1")


# -- purity guard ------------------------------------------------------------------------------


def test_the_purity_guard_is_lifted_after_the_block() -> None:
    with forbid_side_effects(), pytest.raises(ImpureRankerError):
        time.time()
    assert time.time() > 0
    logging.getLogger("probe").debug("allowed again")


def test_the_sample_worlds_are_fixed() -> None:
    first, second = sample_inputs(), sample_inputs()
    assert list(first) == ["empty", "small", "large"]
    assert first == second
    assert [len(w.items) for w in first.values()] == [0, 6, 40]
    large = first["large"]
    assert len(large.activity.spans) == 2000
    assert len(large.activity.sessions) == 3
    assert large.activity.working
    small = first["small"]
    assert sorted({p.workspace for i in small.items for p in i.places if p.workspace}) == [
        "1",
        "10",
        "2",
    ]
    assert any(p.waiting for i in small.items for p in i.places)
