"""The ranker contract (``docs/decisions/0052``, ``docs/rankers.md``).

A ranker's contract module subclasses :class:`RankerContract` and names the ranker::

    class TestMineRankerContract(RankerContract):
        @pytest.fixture
        def ranker_name(self) -> str:
            return "mine"

        @pytest.fixture
        def ranker_settings(self) -> Mapping[str, Any]:  # optional
            return {"place_weight": 2.0}

The contract enables the ranker with ``[rankers.<name>] enabled = true`` plus ``ranker_settings``,
loads it through the registry from the installed entry points, and runs it on the three worlds of
:func:`sample_inputs`. It checks that the ranker:

1. is discoverable, its name matches the entry point and is not ``natural``, its API major
   matches, and its description is one non-empty line of at most 120 characters;
2. has a ``config_model`` that is a pydantic model and builds from defaults only;
3. keeps every input key exactly once;
4. is deterministic: the same input twice gives equal output, and the items shuffled with
   ``random.Random(0)`` give the same order and scores (a total order, independent of input order);
5. gives every item at least one part, finite numbers, and an explanation of at most 200
   characters that holds no item name, path or place title from the input (privacy);
6. is pure: it runs with ``builtins.open``, ``io.open``, ``os.open``, ``socket.socket``,
   ``subprocess.Popen``, ``time.time``, ``time.time_ns``, ``time.monotonic``,
   ``time.monotonic_ns`` and logging patched to raise. ``datetime.now`` cannot be patched: read
   ``RankInput.now`` instead;
7. gives empty output for empty input;
8. orders the workspaces as a permutation of ``workspaces`` (its own ``order_workspaces`` or the
   default one).
"""

from __future__ import annotations

import importlib
import logging
import math
import random
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NoReturn

import pytest
from pydantic import BaseModel, ValidationError

from fylgja.config import Config, parse_config
from fylgja.core.activity import build_activity
from fylgja.core.items import (
    GoTarget,
    Item,
    Place,
    Role,
    choose_go,
    folder_name,
    item_id,
    order_places,
)
from fylgja.rankers.base import (
    FLOOR_NAME,
    MAX_DESCRIPTION,
    MAX_EXPLANATION,
    PLUGIN_NAME_PATTERN,
    RANKER_API_MAJOR,
    Ranked,
    RankerPlugin,
    RankInput,
)
from fylgja.rankers.registry import RegisteredRanker, discover_rankers, load_rankers
from fylgja.store.rows import SpanRow

__all__ = [
    "SAMPLE_NOW",
    "ImpureRankerError",
    "RankerContract",
    "forbid_side_effects",
    "sample_inputs",
]

SAMPLE_NOW: Final = datetime(2026, 10, 13, 15, 0, tzinfo=UTC)
"""The ``now`` of every sample world."""
IDLE_THRESHOLD: Final = timedelta(minutes=5)
SESSION_BREAK: Final = timedelta(minutes=30)
WORKSPACES: Final = ("1", "2", "10")
"""The workspaces of the sample worlds, in natural order."""

_FOLDER: Final = "folder:"
_APP: Final = "app:"
_GO_ROLES: Final = ("ide", "agent", "terminal")
_IDE_APP: Final = "com.example.ide"
_TERMINAL_APP: Final = "com.example.terminal"


# --------------------------------------------------------------------------------------------
# Sample worlds
# --------------------------------------------------------------------------------------------


def _window(
    ref: str, role: Role, label: str, title: str, workspace: str | None, *, open: bool = True
) -> Place:
    return Place(
        kind="window",
        ref=ref,
        role=role,
        label=label,
        title=title,
        workspace=workspace,
        open=open,
        waiting=False,
        last_focus_at=None,
    )


def _session(ref: str, title: str, workspace: str | None, *, waiting: bool = False) -> Place:
    return Place(
        kind="session",
        ref=ref,
        role="agent",
        label="agent",
        title=title,
        workspace=workspace,
        open=True,
        waiting=waiting,
        last_focus_at=None,
    )


def _span(
    window: str, workspace: str, app: str, path: str | None, start: datetime, end: datetime | None
) -> SpanRow:
    return SpanRow(
        window_id=window,
        workspace=workspace,
        app_bundle_id=app,
        title=None,
        project_path=path,
        session_key=None,
        started_at=start,
        ended_at=end,
        end_reason=None if end is None else "focus_change",
    )


@dataclass(frozen=True)
class _Focus:
    """What the spans say: per item key, the end of its newest span and its seconds since
    midnight (UTC); per window, the end of its newest span. An open span ends at ``now``."""

    items: Mapping[str, tuple[datetime, float]]
    windows: Mapping[str, datetime]


def _focus_of(spans: Sequence[SpanRow], now: datetime) -> _Focus:
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    items: dict[str, tuple[datetime, float]] = {}
    windows: dict[str, datetime] = {}
    for span in spans:
        end = min(span.ended_at or now, now)
        key = (
            _FOLDER + span.project_path if span.project_path else _APP + (span.app_bundle_id or "")
        )
        last, seconds = items.get(key, (end, 0.0))
        today = max(0.0, (end - max(span.started_at, midnight)).total_seconds())
        items[key] = (max(last, end), seconds + today)
        if span.window_id:
            windows[span.window_id] = max(windows.get(span.window_id, end), end)
    return _Focus(items, windows)


def _item(
    key: str,
    name: str,
    places: Sequence[Place],
    focus: _Focus,
    *,
    state: Iterable[str] = (),
    tickets: tuple[str, ...] = (),
) -> Item:
    """An item as ``build_items`` makes one; a window's last focus comes from the spans."""
    seen = [
        replace(p, last_focus_at=focus.windows.get(p.ref)) if p.kind == "window" else p
        for p in places
    ]
    ordered = order_places(seen)
    by_role: dict[str, GoTarget] = {}
    for role in _GO_ROLES:
        go = choose_go([place for place in ordered if place.role == role])
        if go is not None:
            by_role[role] = go
    last, seconds = focus.items.get(key, (None, 0.0))
    folder = key.startswith(_FOLDER)
    flags = frozenset(state)
    return Item(
        id=item_id(key),
        key=key,
        kind="folder" if folder else "app",
        name=name,
        path=key.removeprefix(_FOLDER) if folder else None,
        group=None,
        tickets=tickets,
        places=ordered,
        state=flags,
        last_focus_at=last,
        seconds_today=seconds,
        summary=" · ".join([*tickets, *sorted(flags)]),
        go=choose_go(ordered),
        go_by_role=by_role,
    )


def _world(
    now: datetime,
    items: Sequence[Item],
    spans: Sequence[SpanRow],
    workspaces: tuple[str, ...] = WORKSPACES,
) -> RankInput:
    activity = build_activity(
        spans,
        now=now,
        suspended=False,
        idle_threshold=IDLE_THRESHOLD,
        session_break=SESSION_BREAK,
    )
    return RankInput(now=now, items=tuple(items), activity=activity, workspaces=workspaces)


def _small_world(now: datetime) -> RankInput:
    """Six items over three workspaces: an IDE with a waiting agent session (in focus now), a
    terminal, a closed worktree, two apps (one never focused) and an agent with no workspace."""
    alpha, beta, worktree = "/work/alpha/main", "/work/beta/main", "/work/beta/CBS-1"
    browser, mail = "com.example.browser", "com.example.mail"
    at = now.replace(hour=13, minute=30)
    yesterday = now - timedelta(days=1)

    def minutes(n: int) -> datetime:
        return at + timedelta(minutes=n)

    spans = [
        _span("w-beta", "2", _TERMINAL_APP, beta, yesterday, yesterday + timedelta(hours=1)),
        _span("w-alpha", "1", _IDE_APP, alpha, at, minutes(20)),
        _span("w-beta", "2", _TERMINAL_APP, beta, minutes(20), minutes(40)),
        _span("w-worktree", "2", _IDE_APP, worktree, minutes(40), minutes(55)),
        _span("w-browser", "10", browser, None, minutes(55), minutes(70)),
        _span("w-alpha", "1", _IDE_APP, alpha, minutes(70), None),
    ]
    focus = _focus_of(spans, now)
    alpha_places = [
        _window("w-alpha", "ide", "Example IDE", "Alpha editor", "1"),
        _session("agent:alpha-1", "Alpha agent task", "1", waiting=True),
    ]
    items = [
        _item(_FOLDER + alpha, folder_name(alpha), alpha_places, focus, state=["turn_done"]),
        _item(
            _FOLDER + beta,
            folder_name(beta),
            [_window("w-beta", "terminal", "Example Terminal", "Beta shell", "2")],
            focus,
            state=["dirty"],
        ),
        _item(
            _FOLDER + worktree,
            folder_name(worktree),
            [_window("w-worktree", "ide", "Example IDE", "Worktree editor", "2", open=False)],
            focus,
            state=["pr_open"],
            tickets=("CBS-1",),
        ),
        _item(
            _APP + browser,
            "Example Browser",
            [_window("w-browser", "app", "Example Browser", "Browser page", "10")],
            focus,
        ),
        _item(
            _APP + mail,
            "Example Mail",
            [_window("w-mail", "app", "Example Mail", "Mail inbox", "10")],
            focus,
        ),
        _item(
            _FOLDER + "/work/gamma/main",
            folder_name("/work/gamma/main"),
            [_session("agent:gamma-1", "Gamma agent task", None)],
            focus,
            state=["todo_next"],
        ),
    ]
    return _world(now, items, spans)


_LARGE_ITEMS: Final = 40
_FOCUSED_ITEMS: Final = 38
"""Items ``38`` and ``39`` are open and never focused."""
_SPANS_PER_SESSION: Final = (667, 667, 666)
_ITEMS_PER_SESSION: Final = (38, 26, 14)
"""Session ``n`` focuses items ``0`` to ``_ITEMS_PER_SESSION[n] - 1``: items 26 to 37 were last
focused yesterday, 14 to 25 this morning and 0 to 13 this afternoon."""


def _large_key(index: int) -> tuple[str, str | None, str]:
    """``(key, folder path, app bundle id)`` of item ``index``: 30 folders, then 10 apps."""
    if index < 30:
        path = f"/work/project{index:02d}/main"
        app = _IDE_APP if index % 2 == 0 else _TERMINAL_APP
        return _FOLDER + path, path, app
    bundle = f"com.example.app{index:02d}"
    return _APP + bundle, None, bundle


def _large_spans(now: datetime) -> list[SpanRow]:
    """2 000 short spans over three work sessions (yesterday noon, this morning, this
    afternoon), each over fewer items than the one before; the last span is still open."""
    starts = (now - timedelta(hours=27), now - timedelta(hours=5), now - timedelta(minutes=105))
    spans: list[SpanRow] = []
    counter = 0
    plan = zip(starts, _SPANS_PER_SESSION, _ITEMS_PER_SESSION, strict=True)
    for session, (start, count, focused) in enumerate(plan):
        moment = start
        for _ in range(count):
            index = (counter * 11 + session * 3) % focused
            _, path, app = _large_key(index)
            length = timedelta(seconds=4 + (counter * 7) % 9)
            spans.append(
                _span(f"w{index:02d}", WORKSPACES[index % 3], app, path, moment, moment + length)
            )
            moment += length + timedelta(seconds=counter % 3)
            counter += 1
    spans[-1] = replace(spans[-1], ended_at=None, end_reason=None)
    return spans


def _large_state(index: int) -> set[str]:
    state = {"turn_done"} if index == 3 else set()
    if index % 6 == 0:
        state.add("dirty")
    if index % 9 == 1:
        state.add("todo_next")
    if index % 7 == 2:
        state.add("pr_open")
    return state


def _large_world(now: datetime) -> RankInput:
    """Forty items (30 folders and 10 apps) with 2 000 spans over three work sessions."""
    spans = _large_spans(now)
    focus = _focus_of(spans, now)
    items: list[Item] = []
    for index in range(_LARGE_ITEMS):
        key, path, app = _large_key(index)
        workspace = WORKSPACES[index % 3]
        is_open = index % 5 != 4 or index >= _FOCUSED_ITEMS
        role: Role = "app" if path is None else ("ide" if index % 2 == 0 else "terminal")
        places = [
            _window(f"w{index:02d}", role, app, f"Title {index:02d}", workspace, open=is_open)
        ]
        if index < 10:
            ref, title = f"agent:s{index:02d}", f"Task {index:02d}"
            places.append(_session(ref, title, workspace, waiting=index == 3))
        tickets = (f"CBS-{index}",) if index % 10 == 5 else ()
        name = folder_name(path) if path is not None else f"Example App {index:02d}"
        items.append(_item(key, name, places, focus, state=_large_state(index), tickets=tickets))
    return _world(now, items, spans)


def sample_inputs() -> dict[str, RankInput]:
    """Three fixed worlds, built afresh on every call:

    * ``empty``: no items, no spans and no workspaces;
    * ``small``: six items over three workspaces with a waiting session;
    * ``large``: forty items with 2 000 spans over three work sessions.

    Every world uses :data:`SAMPLE_NOW`, placeholder paths and names only, and no real clock.
    """
    now = SAMPLE_NOW
    return {
        "empty": _world(now, (), (), ()),
        "small": _small_world(now),
        "large": _large_world(now),
    }


# --------------------------------------------------------------------------------------------
# Purity
# --------------------------------------------------------------------------------------------


class ImpureRankerError(AssertionError):
    """A ranker read a clock, did I/O or logged while the contract forbade it."""


_FORBIDDEN: Final = (
    ("builtins", "open"),
    ("io", "open"),
    ("os", "open"),
    ("socket", "socket"),
    ("subprocess", "Popen"),
    ("time", "time"),
    ("time", "time_ns"),
    ("time", "monotonic"),
    ("time", "monotonic_ns"),
)


def _forbidden(what: str) -> Callable[..., NoReturn]:
    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise ImpureRankerError(f"a ranker must not call {what}")

    return refuse


@contextmanager
def forbid_side_effects() -> Iterator[None]:
    """Patch the clocks, file and socket I/O, subprocesses and logging to raise
    :class:`ImpureRankerError` while the block runs."""
    with pytest.MonkeyPatch.context() as patch:
        for module, name in _FORBIDDEN:
            patch.setattr(importlib.import_module(module), name, _forbidden(f"{module}.{name}"))
        patch.setattr(logging.Logger, "isEnabledFor", _forbidden("logging"))
        yield


# --------------------------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------------------------


def _keys(ranked: Sequence[Ranked]) -> list[str]:
    return [r.key for r in ranked]


def _private_texts(data: RankInput) -> set[str]:
    texts: set[str] = set()
    for item in data.items:
        texts.add(item.name)
        if item.path:
            texts.add(item.path)
        texts.update(place.title for place in item.places if place.title)
    return {text for text in texts if text}


class RankerContract:
    """Subclass in ``tests/contract/test_<name>_ranker_contract.py`` and override the
    ``ranker_name`` fixture (and ``ranker_settings`` when the defaults are not enough)."""

    # -- fixtures the subclass supplies -------------------------------------------------------

    @pytest.fixture
    def ranker_name(self) -> str:
        raise NotImplementedError("override the ranker_name fixture")

    @pytest.fixture
    def ranker_settings(self) -> Mapping[str, Any]:
        """``[rankers.<name>]`` values, ``enabled`` aside, applied to every check."""
        return {}

    # -- helpers --------------------------------------------------------------------------------

    @staticmethod
    def _config(name: str, settings: Mapping[str, Any]) -> Config:
        table = {**settings, "enabled": True}
        return parse_config({"rankers": {name: table}}, source="contract")

    def _registered(self, name: str, settings: Mapping[str, Any]) -> RegisteredRanker:
        matches = [r for r in load_rankers(self._config(name, settings)) if r.name == name]
        assert len(matches) == 1, f"expected one registration of {name!r}, got {len(matches)}"
        registered = matches[0]
        assert registered.error is None, f"{name} failed to load: {registered.error}"
        assert registered.loaded, f"{name} is not loaded"
        return registered

    @staticmethod
    def _shuffled(data: RankInput) -> RankInput:
        items = list(data.items)
        random.Random(0).shuffle(items)
        return replace(data, items=tuple(items))

    # -- check 1: discoverable --------------------------------------------------------------

    def test_1_discoverable_with_a_valid_name_api_and_description(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        assert PLUGIN_NAME_PATTERN.fullmatch(ranker_name), f"invalid ranker name {ranker_name!r}"
        assert ranker_name != FLOOR_NAME, f"{FLOOR_NAME!r} is reserved for the floor"
        discovery = discover_rankers()
        claimed = [e for e in (*discovery.entries, *discovery.duplicates) if e.name == ranker_name]
        assert claimed, f"no fylgja.rankers entry point named {ranker_name!r}"
        assert len(claimed) == 1, f"entry point name {ranker_name!r} is claimed more than once"

        registered = self._registered(ranker_name, ranker_settings)
        plugin = registered.plugin
        assert isinstance(plugin, RankerPlugin)
        assert plugin.name == ranker_name
        assert plugin.api_major == RANKER_API_MAJOR
        assert plugin.description.strip(), "the description is empty"
        assert "\n" not in plugin.description, "the description is more than one line"
        assert len(plugin.description) <= MAX_DESCRIPTION, "the description is too long"

    # -- check 2: settings ------------------------------------------------------------------

    def test_2_settings_model_builds_from_defaults_only(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        registered = self._registered(ranker_name, ranker_settings)
        plugin = registered.plugin
        assert plugin is not None
        model = plugin.config_model
        assert isinstance(model, type)
        assert issubclass(model, BaseModel)
        try:
            built = model()
        except ValidationError as exc:
            pytest.fail(
                f"config_model() must build from defaults only ({exc.error_count()} errors)"
            )
        assert isinstance(built, model)
        assert isinstance(registered.settings, model)

    # -- check 3: every key exactly once ----------------------------------------------------

    def test_3_keeps_every_input_key_exactly_once(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        registered = self._registered(ranker_name, ranker_settings)
        for label, data in sample_inputs().items():
            keys = _keys(registered.rank(data))
            assert sorted(keys) == sorted(item.key for item in data.items), label
            assert len(set(keys)) == len(keys), f"{label}: a key appears more than once"

    # -- check 4: deterministic -------------------------------------------------------------

    def test_4_is_deterministic_and_independent_of_input_order(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        registered = self._registered(ranker_name, ranker_settings)
        for label, data in sample_inputs().items():
            first = registered.rank(data)
            assert registered.rank(data) == first, f"{label}: two runs differ"
            shuffled = registered.rank(self._shuffled(data))
            assert _keys(shuffled) == _keys(first), f"{label}: the order depends on input order"
            for a, b in zip(first, shuffled, strict=True):
                assert math.isclose(a.score, b.score, rel_tol=1e-9, abs_tol=1e-9), label
            workspaces = registered.order_workspaces(data, first)
            assert registered.order_workspaces(data, first) == workspaces, label
            again = registered.order_workspaces(self._shuffled(data), shuffled)
            assert again == workspaces, f"{label}: the workspace order depends on input order"

    # -- check 5: parts, numbers and privacy ------------------------------------------------

    def test_5_parts_numbers_and_explanation_are_valid_and_private(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        registered = self._registered(ranker_name, ranker_settings)
        for label, data in sample_inputs().items():
            private = _private_texts(data)
            for ranked in registered.rank(data):
                where = f"{label}/{ranked.key}"
                assert isinstance(ranked, Ranked), f"{where}: not a Ranked"
                assert ranked.parts, f"{where}: no parts"
                assert math.isfinite(ranked.score), f"{where}: the score is not finite"
                for part in ranked.parts:
                    assert isinstance(part.name, str), where
                    assert part.name.strip(), f"{where}: a part has no name"
                    assert "\n" not in part.name, f"{where}: a part name has a line break"
                    assert math.isfinite(part.value), f"{where}: {part.name} is not finite"
                text = ranked.explanation
                assert len(text) <= MAX_EXPLANATION, f"{where}: the explanation is too long"
                leaked = sorted(t for t in private if t in text)
                assert not leaked, f"{where}: the explanation holds an item name or title"

    # -- check 6: pure ----------------------------------------------------------------------

    def test_6_is_pure_no_clock_no_io_no_logging(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        registered = self._registered(ranker_name, ranker_settings)
        worlds = sample_inputs()
        with forbid_side_effects():
            for data in worlds.values():
                ranked = registered.rank(data)
                registered.order_workspaces(data, ranked)

    # -- check 7: empty -----------------------------------------------------------------------

    def test_7_empty_input_gives_empty_output(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        registered = self._registered(ranker_name, ranker_settings)
        empty = sample_inputs()["empty"]
        assert registered.rank(empty) == [], "empty input must give empty output"
        assert registered.order_workspaces(empty, []) == [], "no workspaces must give none"

    # -- check 8: workspace order ---------------------------------------------------------------

    def test_8_workspace_order_is_a_permutation(
        self, ranker_name: str, ranker_settings: Mapping[str, Any]
    ) -> None:
        registered = self._registered(ranker_name, ranker_settings)
        for label, data in sample_inputs().items():
            ordered = registered.order_workspaces(data, registered.rank(data))
            assert sorted(ordered) == sorted(data.workspaces), f"{label}: not a permutation"
