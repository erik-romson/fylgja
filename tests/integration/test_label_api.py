"""WP4.2's done condition, through ASGI and through the CLI against an in-process daemon:

* ``/now`` ``workspaces[].label`` shows the manual label directly after ``PUT``;
* it keeps the label while the inferred label changes underneath (driven by fake window data);
* it shows the inferred label again directly after ``DELETE``.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import uvicorn
from api_support import (
    BASE_URL,
    T0,
    Win,
    make_daemon,
    window_snapshot,
    windows_collector,
    workspace_snapshot,
)
from typer.testing import CliRunner

from fylgja import paths
from fylgja.api.app import create_app, server_config
from fylgja.cli import client
from fylgja.cli.app import app as cli
from fylgja.core.daemon import Daemon

SECRET = "k7Qz-test-secret-9fA2xLmN0pRsTuVwXyZ_abcdefghij"
AUTH = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


async def seed(daemon: Daemon, title: str = "docs") -> None:
    """Workspace 2 holds one Safari window; its title is what inference names the workspace by."""
    wm = daemon.sink("wm")
    wm.emit(workspace_snapshot("wm", ["1", "2", "10"], T0, focused="2"))
    wm.emit(window_snapshot("wm", [Win("100", "2", "Safari", 500, title=title)], T0))
    await daemon.settle()


@pytest.fixture
async def daemon() -> AsyncIterator[Daemon]:
    instance = make_daemon(
        windows_collector(), user_config={"privacy": {"redact_patterns": ["sk-[A-Za-z0-9]{8,}"]}}
    )
    await instance.start()
    await seed(instance)
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
async def http(daemon: Daemon) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(daemon, secret=SECRET))
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as session:
        yield session


async def workspace(http: httpx.AsyncClient, name: str = "2") -> dict[str, Any]:
    now = (await http.get("/now")).json()
    return next(w for w in now["workspaces"] if w["name"] == name)


async def label_of(http: httpx.AsyncClient, name: str = "2") -> tuple[str, str, str | None]:
    found = await workspace(http, name)
    return found["label"], found["label_source"], found.get("inferred_label")


# --- the done condition ------------------------------------------------------------------------


async def test_put_shows_manual_keeps_it_while_inference_changes_and_delete_restores(
    daemon: Daemon, http: httpx.AsyncClient
) -> None:
    assert await label_of(http) == ("Safari docs", "inferred", "Safari docs")

    put = await http.put("/workspaces/2/label", headers=AUTH, json={"label": "billing rewrite"})
    assert put.status_code == 200
    assert (put.json()["label"], put.json()["label_source"]) == ("billing rewrite", "manual")
    # directly after the PUT, with no wait
    assert await label_of(http) == ("billing rewrite", "manual", "Safari docs")

    # the windows change, so the inferred label changes underneath
    await seed(daemon, title="mail")
    assert await label_of(http) == ("billing rewrite", "manual", "Safari mail")
    await seed(daemon, title="calendar")
    assert await label_of(http) == ("billing rewrite", "manual", "Safari calendar")
    assert (await workspace(http))["one_liner"].startswith("billing rewrite")

    deleted = await http.request("DELETE", "/workspaces/2/label", headers=AUTH, content=b"{}")
    assert deleted.status_code == 200
    assert (deleted.json()["label"], deleted.json()["label_source"]) == (
        "Safari calendar",
        "inferred",
    )
    # directly after the DELETE: the latest inferred label
    assert await label_of(http) == ("Safari calendar", "inferred", "Safari calendar")
    assert (await workspace(http))["one_liner"].startswith("Safari calendar")


async def test_a_workspace_with_nothing_to_infer_shows_the_manual_label_then_the_dash(
    http: httpx.AsyncClient,
) -> None:
    before = await workspace(http, "10")
    assert (before["label"], before["label_source"]) == ("—", "none")
    assert "inferred_label" not in before
    await http.put("/workspaces/10/label", headers=AUTH, json={"label": "parking"})
    assert await label_of(http, "10") == ("parking", "manual", None)
    await http.request("DELETE", "/workspaces/10/label", headers=AUTH, content=b"{}")
    assert await label_of(http, "10") == ("—", "none", None)


async def test_the_workspace_routes_show_the_manual_label_too(http: httpx.AsyncClient) -> None:
    await http.put("/workspaces/1/label", headers=AUTH, json={"label": "inbox"})
    one = (await http.get("/workspaces/1")).json()
    assert (one["label"], one["label_source"]) == ("inbox", "manual")
    listed = {w["name"]: w for w in (await http.get("/workspaces")).json()}
    assert listed["1"]["label"] == "inbox"
    assert listed["2"]["label_source"] == "inferred"


async def test_the_label_is_whitespace_collapsed_and_redacted(http: httpx.AsyncClient) -> None:
    put = await http.put(
        "/workspaces/2/label", headers=AUTH, json={"label": "  rotate \n sk-ABCDEFGH12345  "}
    )
    assert put.json()["label"] == "rotate [redacted]"
    assert "sk-ABCDEFGH12345" not in (await http.get("/now")).text


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"label": ""}',
        b'{"label": "   "}',
        b'{"label": 5}',
        b'{"label": null}',
        b'{"label": "x", "extra": 1}',
        b"[]",
        b"not json",
        b'{"label": "' + b"x" * 81 + b'"}',
    ],
)
async def test_an_invalid_body_is_a_422_that_changes_nothing(
    http: httpx.AsyncClient, body: bytes
) -> None:
    response = await http.put("/workspaces/2/label", headers=AUTH, content=body)
    assert response.status_code == 422
    assert set(response.json()) == {"detail"}
    assert "xxxxxxxx" not in response.text
    assert await label_of(http) == ("Safari docs", "inferred", "Safari docs")


async def test_the_longest_allowed_label_is_accepted(http: httpx.AsyncClient) -> None:
    text = "y" * 80
    response = await http.put("/workspaces/2/label", headers=AUTH, json={"label": text})
    assert response.json()["label"] == text


async def test_an_unknown_workspace_is_a_404_for_both_writes(http: httpx.AsyncClient) -> None:
    put = await http.put("/workspaces/nope/label", headers=AUTH, json={"label": "x"})
    assert put.status_code == 404
    delete = await http.request("DELETE", "/workspaces/nope/label", headers=AUTH, content=b"{}")
    assert delete.status_code == 404


async def test_the_writes_need_the_bearer_and_the_read_does_not(http: httpx.AsyncClient) -> None:
    body = {"label": "intruder"}
    assert (await http.put("/workspaces/2/label", json=body)).status_code == 401
    assert (await http.request("DELETE", "/workspaces/2/label", content=b"{}")).status_code == 401
    assert (await http.get("/workspaces/2")).status_code == 200
    assert await label_of(http) == ("Safari docs", "inferred", "Safari docs")


# --- the CLI against an in-process daemon ------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Served:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon
        self.runner = CliRunner()

    async def fylgja(self, *args: str, code: int = 0) -> str:
        result = await asyncio.to_thread(self.runner.invoke, cli, list(args))
        assert result.exit_code == code, (args, result.output)
        return str(result.output)

    async def workspace(self, name: str = "2") -> dict[str, Any]:
        now = await asyncio.to_thread(client.get_json, "/now")
        return next(w for w in now["workspaces"] if w["name"] == name)


@pytest.fixture
async def served() -> AsyncIterator[Served]:
    port = free_port()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(f"[api]\nport = {port}\n", encoding="utf-8")
    paths.api_secret_file().write_text(SECRET + "\n", encoding="utf-8")
    daemon = make_daemon(windows_collector(), user_config={"api": {"port": port}})
    await daemon.start()
    await seed(daemon)
    server = uvicorn.Server(server_config(create_app(daemon, secret=SECRET), daemon.config))
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - Uvicorn offers no readiness event
        await asyncio.sleep(0.01)
    try:
        yield Served(daemon)
    finally:
        server.should_exit = True
        await task
        await daemon.stop()


async def test_label_ws_sets_and_clears_through_the_daemon(served: Served) -> None:
    assert (await served.workspace())["label"] == "Safari docs"

    out = await served.fylgja("label", "ws", "2", "billing rewrite")
    assert "billing rewrite" in out
    assert "manual" in out
    now = await served.workspace()
    assert (now["label"], now["label_source"]) == ("billing rewrite", "manual")

    await seed(served.daemon, title="mail")
    now = await served.workspace()
    assert (now["label"], now["inferred_label"]) == ("billing rewrite", "Safari mail")

    out = await served.fylgja("label", "ws", "2", "--clear")
    assert "Safari mail" in out
    assert "inferred" in out
    now = await served.workspace()
    assert (now["label"], now["label_source"]) == ("Safari mail", "inferred")


async def test_label_ws_json_prints_the_workspace(served: Served) -> None:
    import json

    out = await served.fylgja("label", "ws", "2", "named", "--json")
    shown = json.loads(out)
    assert (shown["name"], shown["label"], shown["label_source"]) == ("2", "named", "manual")


async def test_label_ws_refuses_bad_usage_and_unknown_workspaces(served: Served) -> None:
    assert "give a label, or --clear" in await served.fylgja("label", "ws", "2", code=2)
    both = await served.fylgja("label", "ws", "2", "x", "--clear", code=2)
    assert "give a label, or --clear" in both
    missing = await served.fylgja("label", "ws", "nope", "x", code=1)
    assert "404" in missing
    too_long = await served.fylgja("label", "ws", "2", "z" * 81, code=1)
    assert "422" in too_long
    assert (await served.workspace())["label_source"] == "inferred"


async def test_label_ws_reports_a_daemon_that_is_down(served: Served) -> None:
    paths.config_file().write_text(f"[api]\nport = {free_port()}\n", encoding="utf-8")
    out = await served.fylgja("label", "ws", "2", "x", code=3)
    assert "daemon not running" in out
