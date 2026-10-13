"""Rankers are plugins: add, disable and remove one by configuration alone (``docs/rankers.md``).

The real installed ``fylgja-fake`` distribution ships an external ranker, ``fake_ranker`` ("most
places first", one setting), and ``fake_ranker_bomb``, which writes a sentinel and raises when it is
imported. The daemon reads the ``fylgja.rankers`` entry points of the environment (no seam), and
every change below is a ``[rankers.<name>]`` table of the user's configuration:

* **added:** ``fake_ranker`` is listed but neither enabled nor loaded until
  ``[rankers.fake_ranker] enabled = true``; then ``?rank=fake_ranker`` ranks with the settings of
  its table;
* **disabled:** ``[rankers.recent] enabled = false`` takes a built-in out of the choices, and
  ``?rank=recent`` is 422;
* **removed:** with ``[rankers.frecency] enabled = false`` the packaged ``default_ranker`` is not
  loaded, so the ``natural`` floor is the default and ``/items`` and ``/now`` still answer;
* ``fake_ranker_bomb`` is never imported.

No core module names a ranker (``tests/unit/test_no_ranker_dispatch.py``).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from api_support import (
    T0,
    Win,
    client,
    make_daemon,
    nulls,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja import paths
from fylgja.core.daemon import Daemon
from fylgja.core.items import item_id
from fylgja.testing.fakes import FakeFS

ALPHA = "/work/alpha/main"
BOMB_MODULE = "fylgja_fake.ranker_bomb"


def _sentinel() -> Path:
    return paths.state_dir() / "fake_ranker_bomb.sentinel"


def _bomb_untouched() -> None:
    assert not _sentinel().exists()
    assert BOMB_MODULE not in sys.modules


@asynccontextmanager
async def world(user_config: dict[str, Any] | None = None) -> AsyncIterator[Daemon]:
    """Workspace 1 holds a terminal with two sessions in ``/work/alpha/main``; workspace 2 a
    browser window."""
    fs = FakeFS()
    fs.add_dir(f"{ALPHA}/.git")
    daemon = make_daemon(windows_collector(), sessions_collector(), fs=fs, user_config=user_config)
    await daemon.start()
    try:
        wm, agents = daemon.sink("wm"), daemon.sink("agents")
        wm.emit(workspace_snapshot("wm", ["1", "2"], T0, focused="1"))
        wm.emit(
            window_snapshot(
                "wm",
                [
                    Win("100", "1", "Term", 500),
                    Win("200", "2", "Browser", 600, bundle="com.example.browser"),
                ],
                T0,
            )
        )
        agents.emit(
            session_snapshot(
                "agents",
                [
                    session("claude", "abc-123", pid=501, cwd=ALPHA, host_pid=500),
                    session("claude", "def-456", pid=601, cwd=ALPHA, host_pid=500),
                ],
                T0,
            )
        )
        await daemon.settle()
        yield daemon
    finally:
        await daemon.stop()


async def get(daemon: Daemon, path: str, status: int = 200) -> Any:
    async with client(daemon) as http:
        response = await http.get(path)
    assert response.status_code == status, response.text
    return response.json()


def row(body: dict[str, Any], name: str) -> dict[str, Any]:
    found: dict[str, Any] = next(r for r in body["rankers"] if r["name"] == name)
    return found


def choices(body: dict[str, Any]) -> list[str]:
    return [choice["name"] for choice in body["choices"]]


async def test_an_installed_external_ranker_is_listed_but_off_until_enabled() -> None:
    async with world() as daemon:
        body = await get(daemon, "/rankers")
        await get(daemon, "/items?rank=fake_ranker", status=422)
    fake = row(body, "fake_ranker")
    assert (fake["origin"], fake["distribution"], fake["version"]) == (
        "external",
        "fylgja-fake",
        "0.1.0",
    )
    assert (fake["enabled"], fake["loaded"]) == (False, False)
    assert "fake_ranker" not in choices(body)
    _bomb_untouched()


async def test_enabling_the_external_ranker_loads_it_with_its_settings() -> None:
    settings = {"rankers": {"fake_ranker": {"enabled": True, "place_weight": 3.0}}}
    async with world(settings) as daemon:
        body = await get(daemon, "/rankers")
        items = await get(daemon, "/items?rank=fake_ranker")
    fake = row(body, "fake_ranker")
    assert (fake["enabled"], fake["loaded"], fake["description"]) == (
        True,
        True,
        "Most places first",
    )
    assert "fake_ranker" in choices(body)
    assert items["ranker"] == "fake_ranker"
    assert "ranker_fallback" not in items
    alpha, terminal, browser = items["items"]  # two places, then one each by key
    assert alpha["id"] == item_id(f"folder:{ALPHA}")
    assert (alpha["score"], alpha["explanation"]) == (6.0, "places 6")  # 2 places x 3.0
    assert [terminal["key"], browser["key"]] == ["app:com.example.app", "app:com.example.browser"]
    assert [terminal["score"], browser["score"]] == [3.0, 3.0]
    assert list(nulls(items)) == []
    _bomb_untouched()


async def test_disabling_a_built_in_takes_it_out_of_the_choices() -> None:
    async with world({"rankers": {"recent": {"enabled": False}}}) as daemon:
        body = await get(daemon, "/rankers")
        refused = await get(daemon, "/items?rank=recent", status=422)
        await get(daemon, "/items?rank=workspace")
    recent = row(body, "recent")
    assert (recent["origin"], recent["enabled"], recent["loaded"]) == ("built-in", False, False)
    assert "recent" not in choices(body)
    assert "recent" not in refused["detail"]
    assert body["default"] == "frecency"


async def test_without_the_default_ranker_the_natural_floor_is_the_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    async with world({"rankers": {"frecency": {"enabled": False}}}) as daemon:
        body = await get(daemon, "/rankers")
        items = await get(daemon, "/items")
        floor = await get(daemon, "/items?rank=workspace")
        now = await get(daemon, "/now")
    assert (body["default"], body["configured_default"]) == ("natural", "frecency")
    assert row(body, "frecency")["loaded"] is False
    assert choices(body) == ["recent", "workspace"]
    assert not any(choice["default"] for choice in body["choices"])
    assert (body["selected"], body["stale_choice"]) == ("natural", False)
    assert items["ranker"] == "natural"
    assert "ranker_fallback" not in items
    assert items["items"] == floor["items"]
    assert now["schema"] == "fylgja.now/1"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert (
        warnings.count("the default ranker frecency is not loaded; ranking with the natural floor")
        == 1
    )


async def test_with_every_ranker_disabled_the_floor_still_answers() -> None:
    off = {"enabled": False}
    settings = {"rankers": {"frecency": off, "recent": off, "workspace": off}}
    async with world(settings) as daemon:
        body = await get(daemon, "/rankers")
        items = await get(daemon, "/items")
        refused = await get(daemon, "/items?rank=workspace", status=422)
    assert (body["default"], body["choices"]) == ("natural", [])
    assert items["ranker"] == "natural"
    assert items["total"] == 3
    assert refused["detail"] == "rank must name a loaded ranker (none) or the natural floor"


async def test_the_ranker_bomb_is_never_imported() -> None:
    async with world({"rankers": {"fake_ranker": {"enabled": True}}}) as daemon:
        body = await get(daemon, "/rankers")
    bomb = row(body, "fake_ranker_bomb")
    assert (bomb["enabled"], bomb["loaded"]) == (False, False)
    assert "error" not in bomb
    _bomb_untouched()
