"""``GET /view/attention`` over the fixture daemon (``docs/api/views.md``).

The count is the number of ``/now`` ``waiting[]`` entries of rank 0 (``permission`` and ``input``);
the title and the icon follow from it. The signal does not depend on ``[notify]``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
from api_support import (
    BASE_URL,
    T0,
    Win,
    activity_snapshot,
    make_daemon,
    session,
    session_snapshot,
    sessions_collector,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)

from fylgja.api.app import create_app
from fylgja.core.daemon import Daemon
from fylgja.models import SessionActivity

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"


@asynccontextmanager
async def world(states: Mapping[str, SessionActivity]) -> AsyncIterator[Daemon]:
    """A daemon with one Claude session (``claude:c-1``) and one norn run (``norn:n-1``) in
    workspace 1, in the activity states ``states`` names."""
    daemon = make_daemon(windows_collector(), sessions_collector())
    await daemon.start()
    try:
        daemon.sink("wm").emit(workspace_snapshot("wm", ["1"], T0, focused="1"))
        daemon.sink("wm").emit(window_snapshot("wm", [Win("100", "1", "Term", 500)], T0))
        daemon.sink("agents").emit(
            session_snapshot(
                "agents",
                [
                    session("claude", "c-1", pid=501, cwd="/work/a", host_pid=500),
                    session("norn", "n-1", pid=511, cwd="/work/b", host_pid=500),
                ],
                T0,
            )
        )
        daemon.sink("agents").emit(activity_snapshot("agents", states, T0))
        await daemon.settle()
        yield daemon
    finally:
        await daemon.stop()


async def get_attention(daemon: Daemon, **headers: str) -> httpx.Response:
    app = create_app(daemon, secret=SECRET)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http:
        return await http.get("/view/attention", headers=headers)


async def answer(states: Mapping[str, SessionActivity]) -> dict[str, Any]:
    async with world(states) as daemon:
        response = await get_attention(daemon)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    assert body["schema"] == "fylgja.view.attention/1"
    return body


async def test_an_idle_picture_keeps_the_plain_title_and_icon() -> None:
    body = await answer({"claude:c-1": SessionActivity(state="idle")})
    assert {k: body[k] for k in ("waiting", "title", "icon")} == {
        "waiting": 0,
        "title": "Fylgja",
        "icon": "/favicon.png",
    }


async def test_one_permission_wait_counts() -> None:
    body = await answer({"claude:c-1": SessionActivity(state="waiting_permission")})
    assert body["waiting"] == 1
    assert body["title"] == "(1) Fylgja"
    assert body["icon"] == "/favicon-attention.png"


async def test_a_permission_and_a_norn_input_wait_count_twice() -> None:
    body = await answer(
        {
            "claude:c-1": SessionActivity(state="waiting_permission"),
            "norn:n-1": SessionActivity(state="waiting_input", waiting_reason="a question"),
        }
    )
    assert body["waiting"] == 2
    assert body["title"] == "(2) Fylgja"
    assert body["icon"] == "/favicon-attention.png"


async def test_a_finished_turn_alone_does_not_count() -> None:
    body = await answer({"claude:c-1": SessionActivity(state="turn_done")})
    assert (body["waiting"], body["title"], body["icon"]) == (0, "Fylgja", "/favicon.png")


async def test_a_foreign_host_is_refused() -> None:
    async with world({"claude:c-1": SessionActivity(state="waiting_permission")}) as daemon:
        response = await get_attention(daemon, Host="evil.example")
    assert response.status_code == 403
    assert "waiting" not in response.text
